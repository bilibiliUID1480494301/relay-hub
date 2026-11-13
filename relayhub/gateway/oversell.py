"""超售/余额预警：按消耗速率预估「还能撑几天」。

三个输入：
  * **上游余额**：号池里各 Key 的 `credits`（`pool credit` 记的剩余额度；-1/None = 未知）
  * **下游额度**：所有用户 `quota` 之和（已售出但还没用掉的点数）
  * **消耗速率**：请求明细里最近窗口的消耗速度（点/天）

输出：每个口径的剩余天数 + 是否低于阈值。

设计约束：
  * **纯计算，不联网、不起线程**：由管理面按需调用，转发路径零开销
  * 数据不全时**明确说「未知」**，不拿 0 冒充（0 会显示成「马上就没了」，是假警报）
"""

from __future__ import annotations

import time
from typing import Any, Iterable

DEFAULT_WARN_DAYS = 3.0
WINDOW_DAYS = 7.0


def burn_rate(points: Iterable[tuple[float, float]], now: float | None = None,
              window_days: float = WINDOW_DAYS) -> dict[str, Any]:
    """points: [(时间戳, 消耗点数)]。返回窗口内的日均消耗。

    用「窗口内总量 / 实际跨度」而不是除以 window_days：新站只跑了两小时，
    除以 7 天会把速率算成实际值的 1/84，预警永远不会触发。
    """
    now = time.time() if now is None else now
    cutoff = now - window_days * 86400
    recent = [(ts, pts) for ts, pts in points if ts >= cutoff and pts > 0]
    if not recent:
        return {"per_day": 0.0, "samples": 0, "span_days": 0.0}
    total = sum(pts for _, pts in recent)
    oldest = min(ts for ts, _ in recent)
    span_days = max((now - oldest) / 86400.0, 1 / 24)      # 至少按 1 小时算，避免除零放大
    return {
        "per_day": round(total / span_days, 2),
        "samples": len(recent),
        "span_days": round(span_days, 2),
    }


def days_left(balance: float | None, per_day: float) -> float | None:
    """余额 / 日耗。余额未知或日耗为 0 时返回 None（= 无法预估，不是 0 天）。"""
    if balance is None or per_day <= 0:
        return None
    return round(balance / per_day, 1)


def assess(
    *,
    upstream_balance: float | None,
    downstream_quota: float | None,
    per_day: float,
    warn_days: float = DEFAULT_WARN_DAYS,
) -> dict[str, Any]:
    """给出上游/下游两个口径的剩余天数与告警级别。"""
    up_days = days_left(upstream_balance, per_day)
    down_days = days_left(downstream_quota, per_day)

    def level(days: float | None) -> str:
        if days is None:
            return "unknown"
        if days <= warn_days:
            return "critical" if days <= 1 else "warning"
        return "ok"

    return {
        "per_day": per_day,
        "warn_days": warn_days,
        "upstream": {"balance": upstream_balance, "days": up_days, "level": level(up_days)},
        "downstream": {"balance": downstream_quota, "days": down_days, "level": level(down_days)},
        # 超售口径：卖出去的额度比上游余额还多，等于迟早兑不出来
        "oversold": (
            None
            if upstream_balance is None or downstream_quota is None
            else round(downstream_quota - upstream_balance, 2)
        ),
    }


def summarize(
    *,
    key_credits: list[float | None],
    user_quotas: list[float],
    points: Iterable[tuple[float, float]],
    warn_days: float = DEFAULT_WARN_DAYS,
    now: float | None = None,
) -> dict[str, Any]:
    """把号池余额、用户额度、消耗流水汇成一个视图。"""
    known = [c for c in key_credits if c is not None and c >= 0]
    upstream = round(sum(known), 2) if known else None
    downstream = round(sum(q for q in user_quotas if q > 0), 2)
    rate = burn_rate(points, now=now)
    result = assess(
        upstream_balance=upstream,
        downstream_quota=downstream,
        per_day=rate["per_day"],
        warn_days=warn_days,
    )
    result.update(
        {
            "upstream_keys_known": len(known),
            "upstream_keys_unknown": len(key_credits) - len(known),
            "samples": rate["samples"],
            "span_days": rate["span_days"],
        }
    )
    return result
