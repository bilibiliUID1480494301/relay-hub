# -*- coding: utf-8 -*-
"""站间协作：配对令牌 + info/balance/pricing 三个只读接口。

刻意**不测**签名/重放——那套已经被去掉：两站之间走 TLS，签名只多一层
时钟偏差导致的运维问题，换不来真实安全性。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from relayhub.gateway import peer
from relayhub.gateway.service import RelayServer, demo_router
from relayhub.gateway.tokens import TokenStore
from relayhub.gateway.users import UserStore


def test_identify_accepts_only_known_enabled_tokens(tmp_path: Path) -> None:
    store = peer.PeerStore(tmp_path / "peers.json")
    token = peer.new_token()
    store.upsert(peer.Peer(peer_id="lab", url="http://127.0.0.1:1", token=token))

    got = peer.identify(store, {"Authorization": f"Bearer {token}"})
    assert got.peer_id == "lab"

    for bad in ("", "Bearer ", "Bearer wrong", token):
        with pytest.raises(peer.PeerError):
            peer.identify(store, {"Authorization": bad})

    # 停用后与不存在同样回答
    store.upsert(peer.Peer(peer_id="lab", url="http://127.0.0.1:1", token=token, enabled=False))
    with pytest.raises(peer.PeerError) as exc:
        peer.identify(store, {"Authorization": f"Bearer {token}"})
    assert "未授权" in str(exc.value)


def _get(base: str, path: str, token: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(f"{base}{path}")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


@pytest.fixture
def peer_relay(tmp_path: Path):
    import relayhub.paths as paths

    originals = {name: getattr(paths, name) for name in ("users_path", "peer_path")}
    paths.users_path = lambda: tmp_path / "users.json"
    paths.peer_path = lambda: tmp_path / "peers.json"
    UserStore(tmp_path / "users.json")

    server = RelayServer(("127.0.0.1", 0), demo_router(), api_key=None,
                         event_delay=0.0, tokens=TokenStore(tmp_path / "tokens.json"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, tmp_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        for name, value in originals.items():
            setattr(paths, name, value)


def test_no_peers_configured_looks_like_404(peer_relay) -> None:
    server, _ = peer_relay
    status, _body = _get(server.base_url, "/v1/_peer/info")
    assert status == 404


def test_endpoints_require_token_and_flag_advisory(peer_relay) -> None:
    server, home = peer_relay
    token = peer.new_token()
    peer.PeerStore(home / "peers.json").upsert(
        peer.Peer(peer_id="lab", url="http://127.0.0.1:1", token=token)
    )
    base = server.base_url

    status, _ = _get(base, "/v1/_peer/info")
    assert status == 401, "没令牌必须 401"
    status, _ = _get(base, "/v1/_peer/info", "rhp_wrong")
    assert status == 401

    for action in ("info", "balance", "pricing"):
        status, body = _get(base, f"/v1/_peer/{action}", token)
        assert status == 200, body
        # 提示必须随数据一起下发，前端不靠自觉加
        assert body["advisory"] is True
        assert body["source"] == "peer"
        assert "上游" in body["warning"] and "结算" in body["warning"]

    status, info = _get(base, "/v1/_peer/info", token)
    assert "balance" in info["capabilities"]
    assert "forward" not in info["capabilities"], "转发不在站间接口里，走普通上游渠道"


def test_balance_reports_unknown_as_minus_one(peer_relay) -> None:
    """渠道未上报额度（-1）时不能报 0：0 会被误读成"没钱了"。"""
    server, home = peer_relay
    token = peer.new_token()
    peer.PeerStore(home / "peers.json").upsert(
        peer.Peer(peer_id="lab", url="http://127.0.0.1:1", token=token)
    )
    status, body = _get(server.base_url, "/v1/_peer/balance", token)
    assert status == 200
    # 演示路由没有渠道池（keys=0）也属于「不知道」，一律 -1 而不是 0
    assert body["remaining"] == -1
    assert body["unknown_keys"] == body["keys"]


def _post(base: str, path: str, body: bytes, headers: dict | None = None) -> tuple[int, bytes, dict]:
    request = urllib.request.Request(f"{base}{path}", data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        request.add_header(k, v)
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


@pytest.fixture
def forward_relay(tmp_path: Path):
    """带主凭证的站：直通要靠 loopback 调自己的公开接口，没有主凭证只能 503。"""
    import relayhub.paths as paths

    originals = {name: getattr(paths, name) for name in ("users_path", "peer_path")}
    paths.users_path = lambda: tmp_path / "users.json"
    paths.peer_path = lambda: tmp_path / "peers.json"
    UserStore(tmp_path / "users.json")

    server = RelayServer(("127.0.0.1", 0), demo_router(), api_key="master-key",
                         event_delay=0.0, tokens=TokenStore(tmp_path / "tokens.json"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, tmp_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        for name, value in originals.items():
            setattr(paths, name, value)


def test_forward_requires_peer_token_and_dialect(forward_relay) -> None:
    server, home = forward_relay
    token = peer.new_token()
    peer.PeerStore(home / "peers.json").upsert(
        peer.Peer(peer_id="lab", url="http://127.0.0.1:1", token=token)
    )
    base = server.base_url
    body = json.dumps({"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]}).encode()

    status, _raw, _h = _post(base, "/v1/_peer/forward", body, {"X-Peer-Dialect": "openai"})
    assert status == 401

    status, _raw, _h = _post(base, "/v1/_peer/forward", body,
                             {"Authorization": f"Bearer {token}", "X-Peer-Path": "/v1/telepathy"})
    assert status == 400

    # 下游令牌不能当配对令牌用（防"顺手给下游开放直通"）
    status, _raw, _h = _post(base, "/v1/_peer/forward", body,
                             {"Authorization": "Bearer rht_not-a-peer-token",
                              "X-Peer-Path": "/v1/chat/completions"})
    assert status == 401

    status, raw, headers = _post(base, "/v1/_peer/forward", body,
                                 {"Authorization": f"Bearer {token}",
                                  "X-Peer-Path": "/v1/chat/completions"})
    assert status == 200, raw[:200]
    assert headers.get("X-Peer-Forwarded") == "lab"
    # 字节直通：回来的就是本站公开接口的响应，未被改写
    assert b"choices" in raw or b"content" in raw


def test_forward_accepts_gemini_path_without_fixed_mapping(forward_relay) -> None:
    """Gemini 把模型名放在路径里，白名单必须按正则校验后原样转发。"""
    server, home = forward_relay
    token = peer.new_token()
    peer.PeerStore(home / "peers.json").upsert(
        peer.Peer(peer_id="lab", url="http://127.0.0.1:1", token=token)
    )
    base = server.base_url
    body = json.dumps({"contents": [{"parts": [{"text": "hi"}]}]}).encode()

    ok = {"Authorization": f"Bearer {token}",
          "X-Peer-Path": "/v1beta/models/deepseek-v4-flash:generateContent"}
    status, raw, _ = _post(base, "/v1/_peer/forward", body, ok)
    assert status in (200, 400, 404), raw[:160]   # 只要是"被转发了"，不是被白名单挡掉
    assert b"not allowed" not in raw

    bad = {"Authorization": f"Bearer {token}",
           "X-Peer-Path": "/v1beta/models/x:deleteModel"}
    status, raw, _ = _post(base, "/v1/_peer/forward", body, bad)
    assert status == 400, raw[:160]
