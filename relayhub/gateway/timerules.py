"""分时计费规则表。

为什么不是「峰时小时集合」：`peak_hours=[10..21]` 只能表达「每天的这些小时加价」，
而真实需求常带**星期**维度（周末全天闲时、周五整体上浮），旧结构表达不了。

规则形状（按顺序**第一条命中**生效，所以特例要写在前面）：

    [
      {"days": [5, 6], "hours": "*", "multiplier": 1.0},       # 周六日闲时
      {"days": [4],    "hours": "*", "multiplier": 1.2},       # 周五上浮
      {"days": "*",    "hours": [[9, 21]], "multiplier": 1.5}, # 工作日 9-21 峰时
      {"dates": ["2026-10-01"], "hours": "*", "multiplier": 2.0}  # 节假日
    ]

约定：
  * `days`：`"*"` 或 0-6 列表（0=周一）
  * `hours`：`"*"`、整点列表 `[9,10]`，或区间 `[[9,21]]`（含头含尾）、跨零点 `[[22,2]]`
  * `dates`：`"YYYY-MM-DD"` 列表，命中时只看日期、忽略 days
  * 全部规则都不命中 → 1.0
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

try:  # py3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

MAX_MULTIPLIER = 100.0


def _norm_days(raw: Any) -> set[int] | None:
    """None 表示「不限」。"""
    if raw in (None, "", "*"):
        return None
    if isinstance(raw, int):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return None
    days = {int(d) for d in raw if isinstance(d, (int, float)) and 0 <= int(d) <= 6}
    return days or None


def _norm_hours(raw: Any) -> list[tuple[int, int]] | None:
    """返回闭区间列表；None 表示全天。"""
    if raw in (None, "", "*"):
        return None
    if not isinstance(raw, (list, tuple)):
        return None
    spans: list[tuple[int, int]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            try:
                start, end = int(item[0]) % 24, int(item[1]) % 24
            except (TypeError, ValueError):
                continue
            spans.append((start, end))
        elif isinstance(item, (int, float)) and 0 <= int(item) <= 23:
            spans.append((int(item), int(item)))
    return spans or None


def compile_rules(raw: Any) -> list[dict[str, Any]]:
    """把落盘配置编成可快速匹配的形式；坏条目直接丢掉，不让计费 500。"""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            multiplier = float(item.get("multiplier", 1.0))
        except (TypeError, ValueError):
            continue
        if not 0 < multiplier <= MAX_MULTIPLIER:
            continue
        dates = {str(d) for d in (item.get("dates") or []) if isinstance(d, str)}
        out.append(
            {
                "days": _norm_days(item.get("days")),
                "hours": _norm_hours(item.get("hours")),
                "dates": dates,
                "multiplier": multiplier,
            }
        )
    return out


def _hour_hit(spans: list[tuple[int, int]] | None, hour: int) -> bool:
    if spans is None:
        return True
    for start, end in spans:
        if start <= end:
            if start <= hour <= end:
                return True
        else:  # 跨零点：22-2 表示 22,23,0,1,2
            if hour >= start or hour <= end:
                return True
    return False


def multiplier_for(
    rules: list[dict[str, Any]], ts: float | None = None, tz: str = "Asia/Shanghai"
) -> float:
    """按顺序找第一条命中的规则；一条都不中按 1.0。"""
    if not rules:
        return 1.0
    moment = ts if ts is not None else time.time()
    try:
        stamp = (
            datetime.fromtimestamp(moment, ZoneInfo(tz))
            if ZoneInfo
            else datetime.fromtimestamp(moment)
        )
    except Exception:  # noqa: BLE001 - 时区名写错不该让计费失败
        stamp = datetime.fromtimestamp(moment)
    hour = stamp.hour
    weekday = stamp.weekday()
    today = stamp.strftime("%Y-%m-%d")
    for rule in rules:
        if rule["dates"]:
            if today not in rule["dates"]:
                continue
        else:
            days = rule["days"]
            if days is not None and weekday not in days:
                continue
        if not _hour_hit(rule["hours"], hour):
            continue
        return float(rule["multiplier"])
    return 1.0
