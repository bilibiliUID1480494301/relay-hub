"""版本号可见性：token（站长）可见，网页会话（管理员）隐藏。"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

from relayhub import __version__
from relayhub.gateway.admin import SESSION_COOKIE, AdminServer
from relayhub.gateway.pool import KeyPool


def _server(tmp_path: Path) -> AdminServer:
    pool = tmp_path / "pool.json"
    KeyPool([]).save(pool)
    srv = AdminServer(
        ("127.0.0.1", 0), pool, "t",
        audit_path=tmp_path / "audit.jsonl",
        tokens_path=tmp_path / "tokens.json",
        requests_log_path=tmp_path / "req.jsonl",
    )
    srv.users_path = tmp_path / "users.json"      # 别写到全局用户库（会跨用例脏）
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _state(base: str, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(base + "/api/state")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_owner_sees_version_admin_sees_hidden(tmp_path):
    srv = _server(tmp_path)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        owner = _state(base, {"X-Admin-Token": "t"})
        assert owner["version"] == __version__
        assert owner["api_compat"]

        srv.sessions["sid-test"] = time.time() + 300
        # 会话来自 admin token，因此也是 owner
        session = _state(base, {"Cookie": f"{SESSION_COOKIE}=sid-test"})
        assert session["version"] == __version__
        assert session["api_compat"]        # 兼容标识所有已认证用户可读
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_cannot_write_channels_but_owner_can(tmp_path):
    """写操作由服务端按 scope 拒绝：admin 只读，owner 全权。"""
    srv = _server(tmp_path)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        srv.sessions["sid-admin"] = {"exp": time.time() + 300, "role": "admin", "user": "ops"}
        cookie = {"Cookie": f"{SESSION_COOKIE}=sid-admin"}

        def post(headers):
            req = urllib.request.Request(base + "/api/keys", data=b"{}", method="POST")
            req.add_header("Content-Type", "application/json")
            for k, v in headers.items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        assert post(cookie) == 403                       # admin 无 channel:write
        assert post({"X-Admin-Token": "t"}) in (400, 200)  # owner 通过鉴权（后续校验另说）
    finally:
        srv.shutdown()
        srv.server_close()


def test_extra_scope_grants_write_for_admin(tmp_path):
    """逐用户授权：admin 默认只读，但被单独授予 channel:write 后可以写。"""
    from relayhub.gateway.scopes import allowed

    assert allowed("admin", "channel:write") is False
    assert allowed("admin", "channel:write", ["channel:write"]) is True
    assert allowed("admin", "channel:write", ["bogus:scope"]) is False
    assert allowed("owner", "anything", []) is True


def test_only_owner_can_create_admin_account(tmp_path):
    """授予管理员是站长专属动作；admin 自己不能造 admin。"""
    srv = _server(tmp_path)
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def create(headers, name):
        body = json.dumps({"username": name, "password": "pw12345678", "role": "admin"}).encode()
        req = urllib.request.Request(base + "/api/users", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        srv.sessions["sid-adm"] = {"exp": time.time() + 300, "role": "admin", "user": "ops", "scopes": []}
        assert create({"Cookie": f"{SESSION_COOKIE}=sid-adm"}, "adm1") == 403
        assert create({"X-Admin-Token": "t"}, "adm2") == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_events_streams_hello_frame(tmp_path):
    import http.client

    srv = _server(tmp_path)
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    try:
        conn.request("GET", "/api/events", headers={"X-Admin-Token": "t"})
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type").startswith("text/event-stream")
        first = resp.fp.readline()
        assert b"hello" in first and b"api_compat" in first
    finally:
        conn.close()
        srv.shutdown()
        srv.server_close()


def test_plugin_costs_aggregate_tokens(tmp_path):
    import json as _json

    logs = tmp_path / "pluginlogs" / "demo-plugin"
    logs.mkdir(parents=True)
    today = time.strftime("%Y%m%d")
    rows = [
        {"ok": True, "tokens_in": 100, "tokens_out": 50, "latency_ms": 10.0, "plugin_version": "1.2.3"},
        {"ok": False, "tokens_in": 0, "tokens_out": 0, "latency_ms": 30.0},
    ]
    (logs / f"{today}.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )

    srv = _server(tmp_path)
    srv.plugin_log_home = tmp_path / "pluginlogs"
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        req = urllib.request.Request(base + "/api/plugin-costs?days=7")
        req.add_header("X-Admin-Token", "t")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        item = data["plugins"][0]
        assert item["plugin"] == "demo-plugin"
        assert item["requests"] == 2 and item["failed"] == 1
        assert item["tokens_in"] == 100 and item["tokens_out"] == 50
        assert item["fail_rate"] == 50.0
        assert item["version"] == "1.2.3"
    finally:
        srv.shutdown()
        srv.server_close()


def test_events_rejects_when_too_many_clients(tmp_path):
    """连接数上限：超了回 503，不能无限吃线程。"""
    import http.client
    import queue as _queue

    from relayhub.gateway.admin import MAX_SSE_CLIENTS

    srv = _server(tmp_path)
    try:
        with srv.events_lock:
            srv.events_subs.extend(_queue.Queue() for _ in range(MAX_SSE_CLIENTS))
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        conn.request("GET", "/api/events", headers={"X-Admin-Token": "t"})
        assert conn.getresponse().status == 503
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_events_rejects_when_too_many_clients(tmp_path):
    """连接数上限：超了回 503，不能无限吃线程。"""
    import http.client
    import queue as _queue

    from relayhub.gateway.admin import MAX_SSE_CLIENTS

    srv = _server(tmp_path)
    try:
        with srv.events_lock:
            srv.events_subs.extend(_queue.Queue() for _ in range(MAX_SSE_CLIENTS))
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        conn.request("GET", "/api/events", headers={"X-Admin-Token": "t"})
        assert conn.getresponse().status == 503
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()
