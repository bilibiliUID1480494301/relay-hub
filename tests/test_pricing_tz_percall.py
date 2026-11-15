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


def test_channel_ratio_overrides_global():
    """混合渠道：OpenAI 渠道 0.5、DeepSeek 渠道 0.1，各按各的算。"""
    pool = UserPool(price_in_per_1k=10.0, price_out_per_1k=0.0, cache_read_ratio=0.1)
    user = _user()
    # 1000 输入全命中缓存
    assert pool.cost_of(user, 1000, 0, cache_read=1000) == 1                    # 全局 0.1
    assert pool.cost_of(user, 1000, 0, cache_read=1000, channel_ratio=0.5) == 5  # OpenAI 渠道
    assert pool.cost_of(user, 1000, 0, cache_read=1000, channel_ratio=0.0) == 0  # 渠道免费缓存


def test_channel_ratio_clamped():
    pool = UserPool(price_in_per_1k=10.0, price_out_per_1k=0.0)
    # 超过 1 按 1 算，不会出现「缓存比原价还贵」
    assert pool.cost_of(_user(), 1000, 0, cache_read=1000, channel_ratio=9.0) == 10


def test_absolute_cache_price_beats_ratio():
    """给了绝对价就不按比例折算——缓存价可以完全独立于输入价。"""
    pool = UserPool(price_in_per_1k=10.0, price_out_per_1k=0.0,
                    cache_read_ratio=0.1, cache_read_price_per_1k=3.0)
    # 1000 输入全命中：3 元/M × 1k = 3 点，而不是 10×0.1=1 点
    assert pool.cost_of(_user(), 1000, 0, cache_read=1000) == 3


def test_channel_absolute_price_overrides_station():
    pool = UserPool(price_in_per_1k=10.0, price_out_per_1k=0.0, cache_read_price_per_1k=3.0)
    assert pool.cost_of(_user(), 1000, 0, cache_read=1000, channel_cache_price=0.0) == 0
    assert pool.cost_of(_user(), 1000, 0, cache_read=1000, channel_cache_price=8.0) == 8


def test_ratio_still_used_when_no_absolute_price():
    pool = UserPool(price_in_per_1k=10.0, price_out_per_1k=0.0, cache_read_ratio=0.5)
    assert pool.cost_of(_user(), 1000, 0, cache_read=1000) == 5


def test_cache_write_is_billed_at_its_own_price():
    """Anthropic 的 cache_creation 单独计费，不并入输入价。"""
    pool = UserPool(price_in_per_1k=10.0, price_out_per_1k=0.0,
                    cache_write_price_per_1k=12.5)
    # 1000 输入 + 1000 写入：10 + 12.5 → 向上取整 23
    assert pool.cost_of(_user(), 1000, 0, cache_write=1000) == 23


def test_cache_write_defaults_to_free():
    """没配写入单价时不多收（老配置行为不变）。"""
    pool = UserPool(price_in_per_1k=10.0, price_out_per_1k=0.0)
    assert pool.cost_of(_user(), 1000, 0, cache_write=1000) == 10


def test_cache_write_channel_price_overrides():
    pool = UserPool(price_in_per_1k=10.0, price_out_per_1k=0.0, cache_write_price_per_1k=12.5)
    assert pool.cost_of(_user(), 1000, 0, cache_write=1000, channel_cache_write_price=0.0) == 10
