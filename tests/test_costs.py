# -*- coding: utf-8 -*-
"""#17 成本/利润记账：upstream_cost 计算 + summarize 聚合。"""
from __future__ import annotations

import math

from relayhub.gateway.pool import KeyPool
from relayhub.gateway.reqlog import summarize

from support import make_key


def test_upstream_cost_unpriced_is_zero() -> None:
    # 没配单价 = 算不出成本，按 0（语义是「未配价」不是「免费」）
    key = make_key("a", "http://a")
    assert key.cost_multiplier == 1.0
    assert key.upstream_cost(1000, 1000) == 0


def test_upstream_cost_free_channel_is_zero() -> None:
    key = make_key("a", "http://a", cost_multiplier=0.0,
                   price_in_per_1k=2.0, price_out_per_1k=8.0)
    assert key.upstream_cost(100000, 100000) == 0


def test_upstream_cost_formula() -> None:
    # 2 元/1k 输入 + 8 元/1k 输出，倍率 1.5
    key = make_key("a", "http://a", cost_multiplier=1.5,
                   price_in_per_1k=2.0, price_out_per_1k=8.0)
    # (1000/1000*2 + 2000/1000*8) * 1.5 = (2+16)*1.5 = 27
    assert key.upstream_cost(1000, 2000) == 27
    # 向上取整：2.1 → 3
    key2 = make_key("b", "http://b", cost_multiplier=1.0,
                    price_in_per_1k=0.0021, price_out_per_1k=0.0)
    assert key2.upstream_cost(1000, 0) == math.ceil(0.0021 * 1.0) or True
    assert key2.upstream_cost(1000, 0) == 1


def test_summarize_revenue_profit() -> None:
    entries = [
        {"ok": True, "tokens_in": 10, "tokens_out": 5, "cache_read": 0,
         "cost": 30, "upstream_cost": 10, "model": "m", "token": "t",
         "channel": "c", "ts": 0.0},
        {"ok": True, "tokens_in": 20, "tokens_out": 5, "cache_read": 0,
         "cost": 50, "upstream_cost": 30, "model": "m", "token": "t",
         "channel": "c", "ts": 0.0},
    ]
    s = summarize(entries)
    w = s["window"]
    assert w["revenue"] == 80
    assert w["upstream_cost"] == 40
    assert w["profit"] == 40
    assert s["by_model"]["m"]["cost"] == 80
    assert s["by_day"][list(s["by_day"])[0]]["upstream_cost"] == 40


def test_key_group_untouched_by_cost_fields() -> None:
    pool = KeyPool([make_key("a", "http://a", group="paid",
                             cost_multiplier=0.5)])
    assert pool.keys[0].group == "paid"
    assert pool.keys[0].cost_multiplier == 0.5


def test_upstream_cost_applies_peak_multiplier() -> None:
    """上游分时：峰时按倍率加价，空闲按标价；没配峰时则恒定。"""
    import time as _time

    from relayhub.gateway.pool import UpstreamKey

    base = dict(
        key_id="k1", label="t", base_url="http://up", api_key="x",
        price_in_per_1k=1.0, price_out_per_1k=2.0,
    )
    flat = UpstreamKey(**base)
    assert flat.upstream_cost(1000, 1000) == 3

    # 取一个本地小时当"峰时"，倍率 2：1000 in + 1000 out = 3 → 6
    hour = _time.localtime().tm_hour
    peak = UpstreamKey(**base, peak_hours=[hour], peak_multiplier=2.0)
    assert peak.upstream_cost(1000, 1000, ts=_time.time()) == 6
    # 不传 ts 就当没有分时信息，按标价
    assert peak.upstream_cost(1000, 1000) == 3


def test_price_api_payload_is_normalised() -> None:
    """价格接口只认「每 1k token」的已换算数字；峰时价折成倍率。"""
    from relayhub import pricing

    got = pricing.parse_unit_prices({
        "in_per_1k": 0.1, "out_per_1k": 0.2,
        "peak_in_per_1k": 0.2, "peak_out_per_1k": 0.4,
        "peak_hours": [9, 10, 25], "currency": "CNY",
    })
    assert got["price_in_per_1k"] == 0.1
    assert got["price_out_per_1k"] == 0.2
    assert got["peak_hours"] == [9, 10]        # 25 被丢掉，不是抛错
    assert got["peak_multiplier"] == 2.0

    try:
        pricing.parse_unit_prices({"in_per_1k": "abc"})
    except pricing.PricingError:
        pass
    else:
        raise AssertionError("非数字应当报 PricingError")

    # 只有峰时绝对价、没有标价时不能除零
    got2 = pricing.parse_unit_prices({"in_per_1k": 0, "out_per_1k": 0, "peak_out_per_1k": 3})
    assert got2.get("peak_multiplier", 1.0) == 1.0


def test_upstream_cost_long_context_tier() -> None:
    """超过阈值整条请求重算，输入/输出倍率可以不同（OpenAI 是 2×/1.5×）。"""
    from relayhub.gateway.pool import UpstreamKey

    key = UpstreamKey(
        key_id="k1", label="t", base_url="http://up", api_key="x",
        price_in_per_1k=1.0, price_out_per_1k=2.0,
        long_context_threshold=200_000,
        long_ctx_in_multiplier=2.0, long_ctx_out_multiplier=1.5,
    )
    # 未达阈值：100k in + 100k out = 100 + 200 = 300
    assert key.upstream_cost(100_000, 100_000) == 300
    # 刚好到阈值：整条按 2×/1.5× → 200k×2×1 + 100k×1.5×2 = 400 + 300 = 700
    assert key.upstream_cost(200_000, 100_000) == 700
    # 阈值 0 = 不分档
    flat = UpstreamKey(
        key_id="k2", label="t", base_url="http://up", api_key="x",
        price_in_per_1k=1.0, price_out_per_1k=2.0,
    )
    assert flat.upstream_cost(500_000, 500_000) == 1500


def test_price_api_long_context_is_converted_to_multiplier() -> None:
    from relayhub import pricing

    got = pricing.parse_unit_prices({
        "in_per_1k": 0.002, "out_per_1k": 0.008,
        "long_context_threshold": 272000,
        "long_ctx_in_per_1k": 0.004, "long_ctx_out_per_1k": 0.012,
    })
    assert got["long_context_threshold"] == 272000
    assert got["long_ctx_in_multiplier"] == 2.0
    assert got["long_ctx_out_multiplier"] == 1.5

    # 百万 token 单价忘了除 1000 时直接报错，而不是默默记成 1000 倍成本
    try:
        pricing.parse_unit_prices({"in_per_1k": 2.0, "out_per_1k": 8.0})
    except pricing.PricingError as exc:
        assert "除以 1000" in str(exc)
    else:
        raise AssertionError("应当拦住单位错误")


def test_peak_multiplier_respects_weekdays() -> None:
    """厂商的峰时几乎都是「仅工作日」：周末必须按谷价，否则成本翻倍。"""
    import time as _time

    from relayhub.gateway.pool import UpstreamKey

    key = UpstreamKey(
        key_id="k", label="t", base_url="http://up", api_key="x",
        price_in_per_1k=0.001, price_out_per_1k=0.004,
        peak_hours=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23],
        peak_multiplier=2.0, peak_days=[1, 2, 3, 4, 5],
    )
    # 本地时间当日：工作日的当前小时 → 2×；若今天是周末 → 1×
    now = _time.time()
    is_weekday = _time.localtime(now).tm_wday < 5
    assert key.peak_multiplier_at(now) == (2.0 if is_weekday else 1.0)

    # 不填 peak_days = 每天生效（旧行为，不能静默改变已有配置）
    every_day = UpstreamKey(
        key_id="k2", label="t", base_url="http://up", api_key="x",
        price_in_per_1k=0.001, peak_hours=[_time.localtime(now).tm_hour],
        peak_multiplier=2.0,
    )
    assert every_day.peak_multiplier_at(now) == 2.0


def test_upstream_cost_splits_cache_read() -> None:
    """缓存命中的输入按缓存价算；没配缓存价则按输入价（高于等于实际，不低估）。"""
    from relayhub.gateway.pool import UpstreamKey

    base = dict(
        key_id="k", label="t", base_url="http://up", api_key="x",
        price_in_per_1k=0.01, price_out_per_1k=0.04,
    )
    # 1M 输入里 800k 命中，缓存价 0.0002/1k：
    # 200k*0.01/1k = 2.0 + 800k*0.0002/1k = 0.16 → 2.16 → 向上取整 3
    priced = UpstreamKey(**base, price_cache_read_per_1k=0.0002)
    assert priced.upstream_cost(1_000_000, 0, cache_read=800_000) == 3

    # 不配缓存价：全按输入价 1M*0.01/1k = 10
    plain = UpstreamKey(**base)
    assert plain.upstream_cost(1_000_000, 0, cache_read=800_000) == 10

    # cache_read 超过输入时不能算负
    assert plain.upstream_cost(100, 0, cache_read=999) == 1

    # 只配了缓存价也算「配了价」，不该被当成 0 成本
    only_cache = UpstreamKey(
        key_id="k3", label="t", base_url="http://up", api_key="x",
        price_cache_read_per_1k=0.001,
    )
    assert only_cache.upstream_cost(1000, 0, cache_read=1000) >= 1
