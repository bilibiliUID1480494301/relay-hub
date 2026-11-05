"""模型指纹：同清单必须同哈希，字段变化必须被 diff 抓到。"""

from __future__ import annotations

from relayhub import fingerprint as fp


def test_fingerprint_is_order_independent():
    a = [{"id": "m1", "context_window": 1000}, {"id": "m2"}]
    b = [{"id": "m2"}, {"id": "m1", "context_window": 1000}]
    assert fp.catalog_fingerprint(a) == fp.catalog_fingerprint(b)


def test_fingerprint_changes_when_capability_changes():
    a = [{"id": "m1", "context_window": 1000}]
    b = [{"id": "m1", "context_window": 2000}]
    assert fp.catalog_fingerprint(a) != fp.catalog_fingerprint(b)


def test_diff_reports_added_removed_changed():
    old = [{"id": "m1", "ratio": 1.0}, {"id": "gone"}]
    new = [{"id": "m1", "ratio": 2.0}, {"id": "fresh"}]
    out = fp.diff(old, new)
    assert out["added"] == ["fresh"]
    assert out["removed"] == ["gone"]
    assert out["changed"] == ["m1"]


def test_store_roundtrip(tmp_path):
    store = fp.Store(tmp_path / "fp.json")
    entry = store.record("k1", [{"id": "m1"}])
    store.save()
    again = fp.Store(tmp_path / "fp.json")
    assert again.get("k1")["fingerprint"] == entry["fingerprint"]


def test_admin_fingerprint_endpoints(tmp_path):
    import json, threading, urllib.request
    from relayhub.gateway.admin import AdminServer
    from relayhub.gateway.pool import KeyPool, UpstreamKey

    pool_path = tmp_path / "pool.json"
    KeyPool([UpstreamKey(key_id="k1", label="l", base_url="https://example.invalid", api_key="x")]).save(pool_path)
    srv = AdminServer(("127.0.0.1", 0), pool_path, "t",
                      audit_path=tmp_path / "audit.jsonl", tokens_path=tmp_path / "tokens.json",
                      requests_log_path=tmp_path / "req.jsonl")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        req = urllib.request.Request(base + "/api/fingerprint")
        req.add_header("X-Admin-Token", "t")
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.loads(r.read()) == {}
    finally:
        srv.shutdown(); srv.server_close()
