"""站点级客户端识别策略（`identity.json`）。

定位：**让站长识别自己写的客户端**（harness、聊天 App），不是用来看别人的用户。

两条硬约束：

1. 这里只决定「读哪些头、怎么归类」；**具体值属于用户隐私**——管理面不得展示
   某个下游用户的客户端名，站长能看的最大粒度是「按用户」。
2. 没有身份头的请求一律归「直连」，不伪造客户端名。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

DEFAULT_HEADERS = {
    "user": ["X-Client-User"],
    "device_id": ["X-Device-Id"],
    "version": ["X-Client-Version"],
    "plugin": ["X-Client-Plugin"],
}

# 头名白名单：只允许字母数字和连字符，避免有人写进奇怪字符
_MAX_HEADERS = 6
_MAX_VALUE_LEN = 64


def _clean_headers(raw: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not isinstance(raw, Mapping):
        return {k: list(v) for k, v in DEFAULT_HEADERS.items()}
    for key, names in raw.items():
        if not isinstance(names, (list, tuple)):
            names = [names]
        cleaned = []
        for name in names[:_MAX_HEADERS]:
            text = str(name or "").strip()
            if not text or len(text) > 64:
                continue
            if not all(c.isalnum() or c in "-_" for c in text):
                continue
            cleaned.append(text)
        if cleaned:
            out[str(key)] = cleaned
    # 未配置的字段沿用默认头名——只配了 plugin 不该让 user/device 全失效
    for field, names in DEFAULT_HEADERS.items():
        out.setdefault(field, list(names))
    return out


def load(path: Path) -> dict[str, Any]:
    """读站点策略；文件缺失/损坏 → 默认策略（识别开着，头用默认名）。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    mode = str(raw.get("unidentified") or "direct").strip().lower()
    if mode not in ("direct", "reject", "ignore"):
        mode = "direct"
    return {
        "enabled": bool(raw.get("enabled", True)),
        "headers": _clean_headers(raw.get("headers")),
        "unidentified": mode,
        "per_device_concurrency": max(0, int(raw.get("per_device_concurrency") or 0)),
    }


def extract(policy: dict[str, Any], headers: Mapping[str, str]) -> dict[str, str]:
    """按策略从请求头取身份；取不到的字段留空（不猜、不填默认名）。"""
    if not policy.get("enabled", True):
        return {}
    lookup = {str(k).lower(): v for k, v in headers.items()}
    out: dict[str, str] = {}
    for field, names in (policy.get("headers") or {}).items():
        for name in names:
            value = lookup.get(name.lower())
            if value:
                out[field] = str(value).strip()[:_MAX_VALUE_LEN]
                break
    return out
