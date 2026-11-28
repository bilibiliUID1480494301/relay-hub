"""站间协作：配对 + 只读数据端点。

先说清楚这套东西**不是**什么：

* 不是"更安全的通道"。两站之间走 HTTPS，传输完整性由 TLS 负责；再加一层
  请求签名只是在锁好的门上再挂一把锁——信任已经建立，之后就只剩"是谁"。
* 不是转发协议。**转发就是把对方配成一条普通上游渠道**：`base_url` 指向对方
  站、`api_key` 用对方给的令牌，走现有 KeyPool（冷却、重试、记账、限流全部
  继承）。自造转发协议只在"需要不可否认的第三方对账"时才值得。

这里只保留两件真正有用的事：

1. **配对**：一次带外交换令牌，记下对方是谁。
2. **只读数据**：`info / balance / pricing`，让对方能程序化取到余额与价格，
   不必人工抄，也不必把 billing 接口开放给所有人。

所有数值都带 `advisory` 警告：那是**上游自报**的数字，上游可以改、可以延迟、
可以按渠道返回不同结果。**结算以本站自己的请求日志为准**，对端余额只用来
判断"还要不要继续给它派流量"。
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atomicio import write_json_atomic

CAPABILITIES = ("info", "balance", "pricing")

ADVISORY_WARNING = (
    "数据由上游自报，可能被修改或延迟更新；仅供展示与告警，"
    "不作为结算、对账或退款依据（结算以本站请求日志为准）"
)


class PeerError(RuntimeError):
    """配对或凭据不合法。message 可直接回 401/403。"""


@dataclass
class Peer:
    peer_id: str
    url: str = ""
    token: str = ""
    enabled: bool = True
    note: str = ""
    last_seen: float = 0.0

    def public(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id, "url": self.url, "enabled": self.enabled,
            "note": self.note, "last_seen": self.last_seen,
            "has_token": bool(self.token),
        }


@dataclass
class PeerStore:
    path: Path
    _lock: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"peers": {}}
        raw.setdefault("peers", {})
        return raw

    def list(self) -> list[Peer]:
        return [
            Peer(
                peer_id=pid,
                url=str(item.get("url") or ""),
                token=str(item.get("token") or ""),
                enabled=bool(item.get("enabled", True)),
                note=str(item.get("note") or ""),
                last_seen=float(item.get("last_seen") or 0.0),
            )
            for pid, item in (self._read().get("peers") or {}).items()
        ]

    def get(self, peer_id: str) -> Peer | None:
        return next((p for p in self.list() if p.peer_id == peer_id), None)

    def upsert(self, peer: Peer) -> None:
        with self._lock:
            data = self._read()
            data["peers"][peer.peer_id] = {
                "url": peer.url, "token": peer.token, "enabled": peer.enabled,
                "note": peer.note, "last_seen": peer.last_seen,
            }
            write_json_atomic(self.path, data)

    def remove(self, peer_id: str) -> bool:
        with self._lock:
            data = self._read()
            if peer_id not in (data.get("peers") or {}):
                return False
            data["peers"].pop(peer_id)
            write_json_atomic(self.path, data)
            return True

    def touch(self, peer_id: str) -> None:
        with self._lock:
            data = self._read()
            item = (data.get("peers") or {}).get(peer_id)
            if item:
                item["last_seen"] = time.time()
                write_json_atomic(self.path, data)


def new_token() -> str:
    """配对令牌：只用于认「你是哪个站」，不参与加密（传输层由 TLS 负责）。"""
    return "rhp_" + secrets.token_urlsafe(32)


def identify(store: PeerStore, headers: Any) -> Peer:
    """按 Bearer 令牌认出对端；不合法抛 PeerError。

    未配对、令牌错误、被停用三种情况给同一个回答——不告诉探测者
    「这个站存在但被停用了」。
    """
    raw = str(headers.get("Authorization") or "").strip()
    if not raw.lower().startswith("bearer "):
        raise PeerError("缺少配对令牌")
    supplied = raw[7:].strip()
    if not supplied:
        raise PeerError("缺少配对令牌")
    for item in store.list():
        if item.enabled and item.token and hmac.compare_digest(item.token, supplied):
            store.touch(item.peer_id)
            return item
    raise PeerError("未授权的站点")
