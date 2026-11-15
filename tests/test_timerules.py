"""分时规则表：星期、全天、跨零点、节假日，按顺序第一条命中。"""

from __future__ import annotations

from datetime import datetime, timezone

from relayhub.gateway import timerules
from relayhub.gateway.users import User, UserPool

TZ = "Asia/Shanghai"


def ts_of(y, m, d, hour, tz=timezone.utc):
    return datetime(y, m, d, hour, tzinfo=tz).timestamp()


def test_weekend_all_day_is_off_peak():
    """2026-11-15 是周日：全天按闲时。"""
    rules = timerules.compile_rules([
        {"days": [5, 6], "hours": "*", "multiplier": 1.0},
        {"days": "*", "hours": [[9, 21]], "multiplier": 1.5},
    ])
    # UTC 04:00 = 上海 12:00，本该是峰时，但周日命中第一条
    assert timerules.multiplier_for(rules, ts_of(2026, 11, 15, 4), TZ) == 1.0
    # 2026-11-16 是周一，同样的钟点是峰时
    assert timerules.multiplier_for(rules, ts_of(2026, 11, 16, 4), TZ) == 1.5


def test_order_matters_specific_before_general():
    rules = timerules.compile_rules([
        {"days": [4], "hours": "*", "multiplier": 1.2},          # 周五整体
        {"days": "*", "hours": [[9, 21]], "multiplier": 1.5},    # 其余日子峰时
    ])
    # 2026-11-13 是周五，UTC 04:00 = 上海 12:00
    assert timerules.multiplier_for(rules, ts_of(2026, 11, 13, 4), TZ) == 1.2


def test_cross_midnight_range():
    rules = timerules.compile_rules([{"hours": [[22, 2]], "multiplier": 0.8}])
    assert timerules.multiplier_for(rules, ts_of(2026, 11, 16, 15), TZ) == 0.8   # 上海 23:00
    assert timerules.multiplier_for(rules, ts_of(2026, 11, 16, 3), TZ) == 1.0    # 上海 11:00


def test_date_rule_wins_over_weekday():
    rules = timerules.compile_rules([
        {"dates": ["2026-11-16"], "hours": "*", "multiplier": 3.0},
        {"days": "*", "hours": "*", "multiplier": 1.0},
    ])
    assert timerules.multiplier_for(rules, ts_of(2026, 11, 16, 4), TZ) == 3.0
    assert timerules.multiplier_for(rules, ts_of(2026, 11, 17, 4), TZ) == 1.0


def test_bad_rules_are_dropped_not_fatal():
    rules = timerules.compile_rules([
        {"multiplier": "abc"}, {"multiplier": -1}, {"multiplier": 1e9},
        {"hours": [[9, 18]], "multiplier": 1.3},
    ])
    assert len(rules) == 1
    assert timerules.multiplier_for(rules, ts_of(2026, 11, 16, 4), TZ) == 1.3


def test_pool_uses_rules_over_legacy_peak_hours():
    pool = UserPool(peak_hours=[12], peak_multiplier=9.0, timezone=TZ,
                    time_rules=[{"days": "*", "hours": "*", "multiplier": 2.0}])
    assert pool.time_multiplier(ts_of(2026, 11, 16, 4)) == 2.0   # 规则优先，忽略旧字段
    assert Pool_legacy() == 9.0


def Pool_legacy() -> float:
    pool = UserPool(peak_hours=[12], peak_multiplier=9.0, timezone=TZ)
    return pool.time_multiplier(ts_of(2026, 11, 16, 4))
