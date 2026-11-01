"""new-api 账单查询：URL 归一化、分→元、分组倍率、路径回退、错误处理。

全用本地假站点，不打真实上游。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from relayhub.gateway import newapi


def _serve(routes: dict[str, object], *, key: str = "sk-test"):
    """起一个假 new-api。dashboard 路径要求 Bearer key；/api/* 公开。"""

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path.startswith("/dashboard") or "/dashboard/" in path:
                if self.headers.get("Authorization") != f"Bearer {key}":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"bad key"}')
                    return
            body = routes.get(path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not found")
                return
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


ROUTES = {
    "/api/status": {"data": {"quota_display_type": "CNY", "quota_per_unit": 500000}},
    "/v1/dashboard/billing/subscription": {
        "hard_limit_usd": 100.0,
        "soft_limit_usd": 50.0,
        "access_until": 0,
    },
    "/v1/dashboard/billing/usage": {"object": "list", "total_usage": 1234},
    "/api/pricing": {
        "success": True,
        "data": {"group_ratio": {"default": 1, "vip": 0.15}, "data": [{}, {}, {}]},
    },
}


def test_root_normalization() -> None:
    assert newapi._root("https://x.com/v1/") == "https://x.com"
    assert newapi._root("x.com") == "https://x.com"
    assert newapi._root("http://a.b/") == "http://a.b"
    with pytest.raises(newapi.NewApiError):
        newapi._root("")


def test_snapshot_units_and_math() -> None:
    server, base = _serve(ROUTES)
    try:
        snap = newapi.snapshot(base + "/v1", "sk-test")
    finally:
        server.shutdown()
        server.server_close()
    # total_usage=1234 分 → 12.34 元
    assert snap["hard_limit"] == 100.0
    assert snap["used"] == pytest.approx(12.34)
    assert snap["remaining"] == pytest.approx(87.66)
    assert snap["unit"] == "CNY"
    assert snap["groups"] == {"default": 1, "vip": 0.15}
    assert snap["model_count"] == 3


def test_falls_back_to_paths_without_v1() -> None:
    routes = {
        "/api/status": {"data": {"quota_display_type": "USD"}},
        "/dashboard/billing/subscription": {"hard_limit_usd": 10.0},
        "/dashboard/billing/usage": {"total_usage": 500},
    }
    server, base = _serve(routes)
    try:
        snap = newapi.snapshot(base, "sk-test")
    finally:
        server.shutdown()
        server.server_close()
    assert snap["unit"] == "USD"
    assert snap["used"] == pytest.approx(5.0)
    assert snap["remaining"] == pytest.approx(5.0)
    assert snap["groups"] is None


def test_bad_key_raises() -> None:
    server, base = _serve(ROUTES)
    try:
        with pytest.raises(newapi.NewApiError) as exc:
            newapi.snapshot(base, "wrong-key")
    finally:
        server.shutdown()
        server.server_close()
    assert "401" in str(exc.value)


def test_non_json_raises() -> None:
    server, base = _serve({"/api/status": b"<html>nope</html>"})
    try:
        with pytest.raises(newapi.NewApiError) as exc:
            newapi.status(base)
    finally:
        server.shutdown()
        server.server_close()
    assert "不是 JSON" in str(exc.value)


def test_request_cost() -> None:
    server, base = _serve({"/api/cost/request/abc123": {"data": {"cost": 0.02}}})
    try:
        result = newapi.request_cost(base, "sk-test", "abc123")
    finally:
        server.shutdown()
        server.server_close()
    assert result == {"data": {"cost": 0.02}}


def test_request_cost_needs_id() -> None:
    with pytest.raises(newapi.NewApiError):
        newapi.request_cost("http://x", "k", "  ")
