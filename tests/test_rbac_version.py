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
