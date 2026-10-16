# -*- coding: utf-8 -*-
"""性能优化：上游连接池（connpool）复用语义 + 服务端 backlog/NODELAY。

铁律：池按线程隔离（http.client 连接非线程安全）；
     复用前提是「响应体已读完整」；异常必须 mark_dead 而不是留池。
"""
from __future__ import annotations

import threading
import time

from relayhub.gateway import connpool


def _fresh() -> None:
    connpool.clear()


def test_acquire_same_endpoint_reuses_connection() -> None:
    _fresh()
    c1 = connpool.acquire("http", "127.0.0.1", 9, 5.0)
    connpool.release(c1, reusable=True)
    c2 = connpool.acquire("http", "127.0.0.1", 9, 5.0)
    assert c2 is c1, "同 endpoint 应复用同一条连接"
    _fresh()


def test_different_endpoint_not_shared() -> None:
    _fresh()
    a = connpool.acquire("http", "127.0.0.1", 9001, 5.0)
    b = connpool.acquire("http", "127.0.0.1", 9002, 5.0)
    assert a is not b
    _fresh()


def test_scheme_https_not_mixed_with_http() -> None:
    _fresh()
    a = connpool.acquire("http", "h", 80, 5.0)
    b = connpool.acquire("https", "h", 80, 5.0)
    assert a is not b
    _fresh()


def test_release_non_reusable_drops_from_pool() -> None:
    _fresh()
    c = connpool.acquire("http", "127.0.0.1", 9010, 5.0)
    connpool.release(c, reusable=False)
    c2 = connpool.acquire("http", "127.0.0.1", 9010, 5.0)
    assert c2 is not c, "不可复用释放后必须新建"
    _fresh()


def test_mark_dead_evicts() -> None:
    _fresh()
    c = connpool.acquire("http", "127.0.0.1", 9011, 5.0)
    connpool.mark_dead(c)
    c2 = connpool.acquire("http", "127.0.0.1", 9011, 5.0)
    assert c2 is not c
    _fresh()


def test_idle_ttl_evicts_stale() -> None:
    _fresh()
    c = connpool.acquire("http", "127.0.0.1", 9012, 5.0)
    # 直接把池内时间戳改老（比改全局 TTL 更贴近真实「连接躺了一会儿」）
    bucket = connpool._bucket()
    for cached in bucket.values():
        if cached.conn is c:
            cached.used_at = time.monotonic() - (connpool.IDLE_TTL + 10.0)
    c2 = connpool.acquire("http", "127.0.0.1", 9012, 5.0)
    assert c2 is not c, "超过 IDLE_TTL 应重建"
    _fresh()


def test_pool_is_thread_local() -> None:
    _fresh()
    main_conn = connpool.acquire("http", "127.0.0.1", 9020, 5.0)
    seen: list[object] = []

    def worker() -> None:
        seen.append(connpool.acquire("http", "127.0.0.1", 9020, 5.0))
        connpool.clear()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen and seen[0] is not main_conn, "不同线程必须各自持有连接"
    _fresh()


def test_pool_stats_reports_endpoints() -> None:
    _fresh()
    connpool.acquire("http", "127.0.0.1", 9030, 5.0)
    stats = connpool.pool_stats()
    assert any("9030" in k for k in stats), stats
    _fresh()


def test_release_unknown_conn_is_noop() -> None:
    _fresh()
    c = connpool.acquire("http", "127.0.0.1", 9040, 5.0)
    connpool.clear()  # 池已清空，连接成为「游离」
    connpool.release(c, reusable=True)  # 不应抛异常
    connpool.mark_dead(c)
    _fresh()


def test_peer_fin_evicts_half_open_connection() -> None:
    """对端静默关闭（FIN）后，池里的半开连接不得被复用。

    上游重启 / LB 摘后端时，本端 socket 仍标记「未关闭」，直接复用会撞
    ConnectionResetError。`_alive()` 的 MSG_PEEK 探测必须能识别出 EOF。

    这里起一个真 TCP 服务端：accept 后立刻 close（发 FIN），客户端连接
    进入「对端已 FIN」状态，再 acquire 必须拿到新连接。
    """
    import socket as _socket

    _fresh()
    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    conn = connpool.acquire("http", "127.0.0.1", port, 5.0)
    # 真连一次，让 socket 进入已连接状态（未连接时 _alive 直接 True，探不出 FIN）
    conn.connect()
    connpool.release(conn, reusable=True)

    # 服务端 accept 后立即 FIN
    peer, _ = srv.accept()
    peer.close()
    time.sleep(0.05)  # 给 FIN 抵达本端

    again = connpool.acquire("http", "127.0.0.1", port, 5.0)
    assert again is not conn, "对端已 FIN 的半开连接必须丢弃重建"
    srv.close()
    _fresh()
