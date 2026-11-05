"""本站 billing 接口（对标 new-api）+ 功能开关。

数据面：`/v1/dashboard/billing/subscription` 与 `/usage` 由 `features.billing_enabled`
控制开关；关闭时 404，开启后用下游令牌鉴权。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from relayhub import paths
from relayhub.gateway import features
from relayhub.gateway.service import RelayServer, demo_router

API_KEY = "rh_test_token"


@pytest.fixture()
def relay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RELAYHUB_HOME", str(tmp_path))
    server = RelayServer(
        ("127.0.0.1", 0),
        demo_router(),
        api_key=API_KEY,
        event_delay=0.0,
        request_log=tmp_path / "requests.jsonl",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", tmp_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(base: str, path: str, token: str | None) -> tuple[int, object]:
    request = urllib.request.Request(base + path, method="GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- 开关


def test_features_default_off_and_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "features.json"
    assert features.load(path)["billing_enabled"] is False
    assert features.billing_enabled(path) is False
    saved = features.save(path, {"billing_enabled": True})
    assert saved["billing_enabled"] is True
    assert features.billing_enabled(path) is True
    # 未知键被忽略
    features.save(path, {"nope": True})
    assert features.load(path) == {"billing_enabled": True}


def test_features_corrupt_file_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "features.json"
    path.write_text("{not json", encoding="utf-8")
    assert features.load(path)["billing_enabled"] is False


# ---------------------------------------------------------------- billing 端点


def test_billing_disabled_by_default(relay) -> None:
    base, _ = relay
    assert _get(base, "/v1/dashboard/billing/subscription", API_KEY)[0] == 404
    assert _get(base, "/v1/dashboard/billing/usage", API_KEY)[0] == 404


def test_billing_enabled_shapes(relay) -> None:
    base, _ = relay
    features.save(paths.features_path(), {"billing_enabled": True})

    status, sub = _get(base, "/v1/dashboard/billing/subscription", API_KEY)
    assert status == 200, sub
    assert sub["object"] == "billing_subscription"
    assert set(("hard_limit_usd", "soft_limit_usd", "access_until")) <= set(sub)
    assert sub["display_type"] == "points"
    assert sub["unlimited"] is False

    status, use = _get(
        base,
        "/v1/dashboard/billing/usage?start_date=2026-10-01&end_date=2026-10-31",
        API_KEY,
    )
    assert status == 200, use
    assert use["object"] == "list"
    assert isinstance(use["total_usage"], int)
    assert use["display_type"] == "points"


def test_billing_requires_token(relay) -> None:
    base, _ = relay
    features.save(paths.features_path(), {"billing_enabled": True})
    assert _get(base, "/v1/dashboard/billing/subscription", None)[0] == 401
