"""超售预警：余额未知不许伪装成 0；新站不能被窗口稀释成永不告警。"""

from __future__ import annotations

import time

from relayhub.gateway import oversell

DAY = 86400.0


def test_burn_rate_uses_actual_span_not_full_window():
    """只跑了两小时的新站：除以 7 天会把速率压成 1/84，预警永远不触发。"""
    now = time.time()
    points = [(now - 2 * 3600, 100.0)]
    rate = oversell.burn_rate(points, now=now)
    assert rate["per_day"] >= 1000        # 两小时烧 100 → 日耗至少 1200 量级
    assert rate["span_days"] < 0.2


def test_burn_rate_ignores_old_and_zero_points():
    now = time.time()
    points = [(now - 30 * DAY, 9999.0), (now - 3600, 0.0), (now - 3600, 50.0)]
    rate = oversell.burn_rate(points, now=now)
    assert rate["samples"] == 1 and rate["per_day"] > 0


def test_unknown_balance_is_not_zero_days():
    result = oversell.assess(upstream_balance=None, downstream_quota=100, per_day=10)
    assert result["upstream"]["level"] == "unknown"
    assert result["upstream"]["days"] is None      # 不是 0，也不是「马上没了」


def test_zero_burn_gives_no_forecast():
    result = oversell.assess(upstream_balance=1000, downstream_quota=500, per_day=0)
    assert result["upstream"]["days"] is None


def test_levels_scale_with_warn_days():
    result = oversell.assess(upstream_balance=100, downstream_quota=100, per_day=40, warn_days=3)
    assert result["upstream"]["days"] == 2.5
    assert result["upstream"]["level"] == "warning"      # ≤3 天预警
    critical = oversell.assess(upstream_balance=30, downstream_quota=30, per_day=40, warn_days=3)
    assert critical["upstream"]["level"] == "critical"   # ≤1 天
    healthy = oversell.assess(upstream_balance=1000, downstream_quota=10, per_day=10, warn_days=3)
    assert healthy["upstream"]["level"] == "ok"


def test_oversold_is_downstream_minus_upstream():
    result = oversell.assess(upstream_balance=100, downstream_quota=250, per_day=10)
    assert result["oversold"] == 150       # 卖出去 250，上游只有 100 → 超售 150


def test_summarize_marks_unknown_keys():
    now = time.time()
    result = oversell.summarize(
        key_credits=[500.0, None, -1],
        user_quotas=[100, 50],
        points=[(now - DAY, 100.0)],
        now=now,
    )
    assert result["upstream"]["balance"] == 500.0
    assert result["upstream_keys_known"] == 1
    assert result["upstream_keys_unknown"] == 2
