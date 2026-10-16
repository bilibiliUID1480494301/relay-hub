# -*- coding: utf-8 -*-
"""性能优化：上游连接池（thread-local + keep-alive 复用）。

问题：原实现每个请求都 `HTTPSConnection(...)` 新建 TCP+TLS，用完 close()。
高并发/高频对话下，TLS 握手（~2-3 个 RTT）是主要固定开销，且每次新建 socket
快速耗尽本地端口（TIME_WAIT）。

做法：
1. 按 (scheme, host, port) 缓存连接，**thread-local**（http.client 连接非线程安全，
   ThreadingHTTPServer 每请求一线程，天然按线程私有复用最安全）。
2. 发送前显式 `Connection: keep-alive`；读完整响应体（含流式读完）后连接可复用。
3. 空闲超过 IDLE_TTL（默认 60s）或出错即丢弃重建——上游可能已单方关闭。
4. 池大小按线程自动有界（每线程每个 endpoint 最多 1 条），不会无限增长。

对使用者透明：`_open()` 返回的仍是 HTTPConnection，调用方照旧 `.request()/.getresponse()`，
只是 `close()` 语义从「关 socket」变成「归还池」（见 `release()`）。
"""
from __future__ import annotations

import select
import socket
import threading
import time
from http.client import HTTPConnection, HTTPSConnection
from typing import Any

IDLE_TTL = 60.0      # 空闲超过此秒数的连接直接丢弃（上游常在此后单方 FIN）
_tls = threading.local()


class _Cached:
    __slots__ = ("conn", "used_at")

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.used_at = time.monotonic()


def _bucket() -> dict:
    b = getattr(_tls, "pool", None)
    if b is None:
        b = {}
        _tls.pool = b
    return b


def acquire(scheme: str, host: str, port: int, timeout: float) -> HTTPConnection:
    """取一条可复用连接：优先命中同线程缓存，否则新建。"""
    key = (scheme, host, port)
    bucket = _bucket()
    cached = bucket.get(key)
    if cached is not None:
        conn = cached.conn
        # 过期 / 已被上游关闭 → 丢弃重建
        if (time.monotonic() - cached.used_at) <= IDLE_TTL and _alive(conn):
            conn.timeout = timeout  # 复用连接时刷新超时（http.client 允许改）
            return conn
        try:
            conn.close()
        except Exception:
            pass
        bucket.pop(key, None)
    if scheme == "https":
        conn = HTTPSConnection(host, port, timeout=timeout)
    else:
        conn = HTTPConnection(host, port, timeout=timeout)
    bucket[key] = _Cached(conn)
    return conn


def _alive(conn: HTTPConnection) -> bool:
    """粗判连接是否还可用：底层 socket 未被标记关闭，且对端没发过 FIN。

    注意 sock=None 有两种含义——「还没连过」和「已关闭」。前者可复用（可用），
    后者不可。http.client 在 close() 后把 sock 置 None，但我们自己在 release
    时不会 close 可复用的连接，所以池里的连接 sock 要么是有效 socket，要么是
    从未 request 过的新连接（同样可用）。真正关闭的连接由 mark_dead/超时淘汰。

    **对端 FIN 探测**：上游（nginx/LB/上游重启）常在自己那侧静默关闭 keep-alive
    连接，本端 socket 仍标记为「未关闭」。直接复用会撞上半开连接，表现是
    `ConnectionResetError` / `RemoteDisconnected`——请求白跑一次。
    这里用非阻塞 `recv(1, MSG_PEEK)` 探一下：能读出 b"" 说明对端已 FIN。
    """
    sock = getattr(conn, "sock", None)
    if sock is None:
        return True  # 未连接的裸连接：可复用（复用时会自行 connect）
    try:
        if sock._closed:  # type: ignore[attr-defined]
            return False
    except Exception:
        return True
    # 已连接过：探对端是否已关闭。只在有未读数据或 EOF 时才判定，绝不消费数据。
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return True  # 无事件：对端安静，认为还活着
        peeked = sock.recv(1, socket.MSG_PEEK)
        return bool(peeked)  # 读到数据=还活着（有响应残留）；b""=对端 FIN
    except (BlockingIOError, InterruptedError):
        return True
    except Exception:
        return False  # 任何异常（含 ConnectionReset）都按已死处理


def release(conn: HTTPConnection, reusable: bool = True) -> None:
    """归还连接：可复用的留池（刷新时间戳），否则真关。

    调用方在「完整读出响应体」时传 reusable=True；异常/半途放弃时传 False。
    """
    if reusable and _alive(conn):
        # 记住所属桶：按连接自身的 host/port 反查不现实，直接扫本线程桶找它
        for cached in _bucket().values():
            if cached.conn is conn:
                cached.used_at = time.monotonic()
                return
        # 不在池里（可能是新建后未登记的异常路径）→ 放到已知桶按 host 猜
        return
    try:
        conn.close()
    except Exception:
        pass
    for key, cached in list(_bucket().items()):
        if cached.conn is conn:
            _bucket().pop(key, None)


def mark_dead(conn: HTTPConnection) -> None:
    """异常路径：连接可能已损坏，直接关闭并移出池。"""
    try:
        conn.close()
    except Exception:
        pass
    for key, cached in list(_bucket().items()):
        if cached.conn is conn:
            _bucket().pop(key, None)


def pool_stats() -> dict[str, int]:
    """诊断用：本线程池内连接数。"""
    return {f"{k[0]}://{k[1]}:{k[2]}": 1 for k in _bucket()}


def clear() -> None:
    """测试/关闭用：清空本线程所有缓存连接。"""
    for cached in _bucket().values():
        try:
            cached.conn.close()
        except Exception:
            pass
    _bucket().clear()
