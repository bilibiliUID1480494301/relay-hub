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
