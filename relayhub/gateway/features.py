"""站点功能开关（目前只有 billing_enabled）。

存 `<home>/features.json`。管理面可开可关，数据面**每请求读取**这个小文件，
所以开关改完下一请求即生效，不用重启网关。

默认全关：对外多开一个接口就是多一个暴露面，要显式打开。
"""

from __future__ import annotations

import json
from pathlib import Path

from .atomicio import write_json_atomic

# 所有已知开关及其默认值。新增开关时在这里登记，load() 自动补齐默认。
DEFAULTS: dict[str, bool] = {
    # 对外提供 /v1/dashboard/billing/*（对标 new-api 的余额/用量查询）
    "billing_enabled": False,
}

_SCHEMA = 1


def load(path: Path) -> dict[str, bool]:
    """读开关文件；缺失/损坏 → 全默认（不因为一个坏文件把功能全锁死或全放开）。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULTS)
    if not isinstance(raw, dict):
        return dict(DEFAULTS)
    out = dict(DEFAULTS)
    for key in out:
        if key in raw:
            out[key] = bool(raw[key])
    return out


def save(path: Path, values: dict) -> dict[str, bool]:
    """合并写入（只认已知键，未知键忽略），返回写入后的完整开关表。"""
    current = load(path)
    for key in current:
        if key in values and values[key] is not None:
            current[key] = bool(values[key])
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(target, {"schemaVersion": _SCHEMA, **current})
    return current


def billing_enabled(path: Path) -> bool:
    return bool(load(path).get("billing_enabled", False))
