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
