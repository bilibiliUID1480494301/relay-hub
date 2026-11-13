"""TOTP 恢复码：手机丢了/时钟坏了还能进管理面。

没有恢复码的二次验证是「一次性门闩」——设备一丢就只能改文件绕过，那等于没有。
恢复码一次性、只存哈希、可随时重新生成（旧的一律作废）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

SCHEMA = "relayhub-recovery/1"
DEFAULT_COUNT = 10


def _digest(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace("-", "").encode("utf-8")).hexdigest()


def make_codes(count: int = DEFAULT_COUNT) -> list[str]:
    """人类可抄写的 8 位码（两段四位），不含易混字符。"""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    codes = []
    for _ in range(max(1, min(count, 20))):
        raw = "".join(secrets.choice(alphabet) for _ in range(8))
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


class Store:
    """恢复码集合：只留哈希 + 是否已用。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.state: dict[str, Any] = {"schema": SCHEMA, "codes": []}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("schema") == SCHEMA:
                    self.state = loaded
            except (OSError, ValueError):
                pass

    def regenerate(self, count: int = DEFAULT_COUNT) -> list[str]:
        """生成新一批，**旧码全部作废**。返回明文（只此一次）。"""
        codes = make_codes(count)
        self.state["codes"] = [{"hash": _digest(c), "used": False} for c in codes]
        self.save()
        return codes

    def consume(self, code: str) -> bool:
        """核销一个恢复码。已用/不存在都返回 False。"""
        digest = _digest(code)
        for item in self.state.get("codes", []):
            if not item.get("used") and item.get("hash") == digest:
                item["used"] = True
                self.save()
                return True
        return False

    def remaining(self) -> int:
        return sum(1 for item in self.state.get("codes", []) if not item.get("used"))

    def save(self) -> None:
        from .atomicio import write_json_atomic
        from .secretbox import harden

        write_json_atomic(self.path, self.state)
        harden(self.path)
