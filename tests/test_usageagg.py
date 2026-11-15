"""用量聚合：直连不进客户端维度，但钱照算。"""

from __future__ import annotations

from relayhub.gateway import usageagg


def _e(src="", cost=1, ok=True, tin=10, tout=5, ts=1000.0):
    d = {"ts": ts, "cost": cost, "ok": ok, "tokens_in": tin, "tokens_out": tout}
    if src:
        d["ident_src"] = src
    return d


def test_clients_exclude_direct_but_total_includes_it():
    rows = [_e("deepseekharness", cost=5), _e("deepseekharness", cost=5), _e("", cost=3)]
    out = usageagg.aggregate(rows)
    assert out["total"]["requests"] == 3 and out["total"]["cost"] == 13
    assert [r["client"] for r in out["by_client"]] == ["deepseekharness"]
    assert out["by_client"][0]["cost"] == 10
    assert out["direct"]["requests"] == 1 and out["direct"]["cost"] == 3


def test_direct_bucket_is_always_reported():
    out = usageagg.aggregate([_e("x", cost=2)])
    assert out["direct"]["requests"] == 0        # 没有直连也要给出来，别让人去猜


def test_failed_requests_are_counted_separately():
    out = usageagg.aggregate([_e("a", ok=False), _e("a", ok=True)])
    assert out["by_client"][0]["failed"] == 1 and out["by_client"][0]["ok"] == 1


def test_since_filters_by_time():
    out = usageagg.aggregate([_e("a", ts=100.0), _e("a", ts=200.0)], since=150.0)
    assert out["total"]["requests"] == 1


def test_client_falls_back_across_identity_fields():
    entry = {"ts": 1, "ok": True, "ident_dev": "laptop"}
    assert usageagg.client_of(entry) == "laptop"
    assert usageagg.client_of({"ts": 1, "ok": True}) == ""
