"""计费：时区必须显式，按次要能单独计价。"""

from __future__ import annotations

from datetime import datetime, timezone

from relayhub.gateway.users import User, UserPool


def _user(quota: int = 10000) -> User:
    return User(user_id="u1", username="u1", password_hash="x", quota=quota)


def _ts(utc_hour: int) -> float:
    return datetime(2026, 5, 1, utc_hour, 0, tzinfo=timezone.utc).timestamp()


def test_peak_hour_follows_configured_timezone():
    """UTC 02:00 = 上海 10:00：同一时刻在两个时区下峰谷判断相反。"""
    ts = _ts(2)
    shanghai = UserPool(peak_hours=[10], peak_multiplier=2.0, timezone="Asia/Shanghai")
    utc = UserPool(peak_hours=[10], peak_multiplier=2.0, timezone="UTC")
    assert shanghai.time_multiplier(ts) == 2.0
    assert utc.time_multiplier(ts) == 1.0


def test_bad_timezone_falls_back_instead_of_raising():
    pool = UserPool(peak_hours=[10], peak_multiplier=2.0, timezone="Mars/Olympus")
    assert pool.time_multiplier(_ts(2)) in (1.0, 2.0)      # 不抛异常即可


def test_per_call_is_charged_even_with_zero_tokens():
    pool = UserPool(price_in_per_1k=2.0, price_out_per_1k=6.0, per_call=5.0)
    assert pool.cost_of(_user(), 0, 0) == 5                # 没有 token 也按次收


def test_per_call_adds_on_top_of_token_price():
    pool = UserPool(price_in_per_1k=2.0, price_out_per_1k=6.0, per_call=5.0)
    # 1000 in + 1000 out = 2 + 6 = 8 点，再加按次 5 → 13
    assert pool.cost_of(_user(), 1000, 1000) == 13


def test_unlimited_quota_is_free():
    pool = UserPool(per_call=5.0)
    assert pool.cost_of(_user(quota=-1), 1000, 1000) == 0


def test_cache_hit_input_is_discounted():
    """命中上游缓存的输入按比例计价：上游收得少，用户不能按全价付。"""
    pool = UserPool(price_in_per_1k=10.0, price_out_per_1k=0.0, cache_read_ratio=0.1)
    user = _user()
    full = pool.cost_of(user, 1000, 0)                    # 10 点
    cached = pool.cost_of(user, 1000, 0, cache_read=1000)  # 1 点
    assert full == 10 and cached == 1


def test_partial_cache_hit_is_proportional():
    pool = UserPool(price_in_per_1k=10.0, price_out_per_1k=0.0, cache_read_ratio=0.1)
    # 一半命中：5 + 0.5 = 5.5 → 向上取整 6
    assert pool.cost_of(_user(), 1000, 0, cache_read=500) == 6


def test_cache_read_cannot_exceed_input():
    pool = UserPool(price_in_per_1k=10.0, price_out_per_1k=0.0, cache_read_ratio=0.1)
    assert pool.cost_of(_user(), 1000, 0, cache_read=99999) == 1   # 按全命中算，不会变负数
