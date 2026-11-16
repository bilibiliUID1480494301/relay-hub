# -*- coding: utf-8 -*-
"""A2A 路由测试：agent 池加密、agent card、message/send 转发与记账、鉴权、级联头。"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from relayhub.gateway import a2a as a2a_module
from relayhub.gateway.a2a import A2aAgent, A2aPool


class FakeA2aDownstream(BaseHTTPRequestHandler):
    """最小 A2A 下游：回 message/send 结果，并回显收到的 Via（验证级联头）。"""

    protocol_version = "HTTP/1.1"
    last_via = ""

    def log_message(self, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N815
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        FakeA2aDownstream.last_via = self.headers.get("Via") or ""
        body = json.dumps({
            "jsonrpc": "2.0", "id": request.get("id"),
            "result": {"task": {"id": "t1", "status": "done"}},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


from contextlib import contextmanager  # noqa: E402


@contextmanager
def a2a_downstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeA2aDownstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_agent_pool_roundtrip_is_sealed(tmp_path: Path) -> None:
    pool_path = tmp_path / "a2a_agents.json"
    pool = A2aPool()
    pool.add(A2aAgent(agent_id="a1", name="peer", url="http://127.0.0.1:9",
                      token="a2a_sekrit", created_at=1.0))
    pool.save(pool_path)
    raw = pool_path.read_text(encoding="utf-8")
    assert '"enc"' in raw and "a2a_sekrit" not in raw
    back = A2aPool.load(pool_path)
    assert back.agents[0].token == "a2a_sekrit"


def test_agent_card_lists_skills(tmp_path: Path) -> None:
    pool_path = tmp_path / "a2a_agents.json"
    pool = A2aPool()
    pool.add(A2aAgent(agent_id="a1", name="peer", url="http://127.0.0.1:9",
                      description="the other hub"))
    pool.save(pool_path)
    card = a2a_module.agent_card(pool_path, "lab-hub", "http://10.0.0.2:8799")
    assert card["name"] == "lab-hub"
    assert card["skills"][0]["id"] == "peer"


API_KEY = "rh_a2a_down"


@contextmanager
def relay_with_a2a(tmp_path: Path, downstream_url: str):
    from relayhub.gateway.pool import KeyPool
    from relayhub.gateway.router import KeyPoolRouter
    from relayhub.gateway.service import RelayServer
    from tests.support import make_key

    previous = os.environ.get("RELAYHUB_HOME")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["RELAYHUB_HOME"] = str(home)
    pool_path = home / "a2a_agents.json"
    pool = A2aPool()
    pool.add(A2aAgent(agent_id="a1", name="peer", url=downstream_url,
                      token="down_token", created_at=1.0))
    pool.save(pool_path)
    router = KeyPoolRouter(KeyPool([make_key("real", "http://127.0.0.1:9")]))
    server = RelayServer(("127.0.0.1", 0), router, api_key=API_KEY, event_delay=0.0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if previous is None:
            os.environ.pop("RELAYHUB_HOME", None)
        else:
            os.environ["RELAYHUB_HOME"] = previous


def _rpc(base: str, payload: dict, credential: str | None = API_KEY) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{base}/a2a", data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    request.add_header("Content-Type", "application/json")
    if credential:
        request.add_header("x-api-key", credential)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # noqa: TID251
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_gateway_agent_card_endpoint(tmp_path: Path) -> None:
    with a2a_downstream() as url:
        with relay_with_a2a(tmp_path, url) as base:
            with urllib.request.urlopen(f"{base}/.well-known/agent.json", timeout=10) as response:
                card = json.loads(response.read().decode("utf-8"))
            assert card["skills"][0]["id"] == "peer"


def test_gateway_message_send_forwards_with_cascade_headers(tmp_path: Path) -> None:
    """message/send 转发：Via 带上本站实例标记（级联判环依赖它）。"""
    with a2a_downstream() as url:
        with relay_with_a2a(tmp_path, url) as base:
            status, reply = _rpc(base, {"jsonrpc": "2.0", "id": 7, "method": "message/send",
                                        "params": {"agent": "peer", "message": {"text": "hi"}}})
            assert status == 200, reply
            assert reply["result"]["task"]["status"] == "done"
            assert "relayhub-" in FakeA2aDownstream.last_via


def test_gateway_message_send_requires_auth(tmp_path: Path) -> None:
    with a2a_downstream() as url:
        with relay_with_a2a(tmp_path, url) as base:
            status, reply = _rpc(base, {"jsonrpc": "2.0", "id": 1, "method": "message/send",
                                        "params": {"agent": "peer"}}, credential=None)
            assert status == 401, reply


def test_gateway_unknown_agent_is_clean_error(tmp_path: Path) -> None:
    with a2a_downstream() as url:
        with relay_with_a2a(tmp_path, url) as base:
            status, reply = _rpc(base, {"jsonrpc": "2.0", "id": 2, "method": "message/send",
                                        "params": {"agent": "ghost", "message": {}}})
            assert status == 200
            assert reply["error"]["code"] == -32602


def test_agent_card_version_follows_package(tmp_path: Path) -> None:
    """名片 version 必须随包版本单一来源（0.8.0 写死在 0.6.0）。"""
    from relayhub import __version__
    from relayhub.gateway.a2a import agent_card

    card = agent_card(tmp_path / "a2a.json", "st-name", "http://127.0.0.1:8799")
    assert card["version"] == __version__


def _wait_unread_counter(target: int, timeout: float = 3.0) -> int:
    """等服务端线程把计数器加上去（响应返回时记账可能还没跑完）。"""
    from relayhub.gateway.service import RelayHandler

    deadline = time.time() + timeout
    while time.time() < deadline and RelayHandler.unread_body_closes < target:
        time.sleep(0.02)
    return RelayHandler.unread_body_closes


def test_unread_body_counter_catches_the_bug(tmp_path: Path, monkeypatch) -> None:
    """关掉 drain 后，计数器必须抓到「请求体没读完」——证明这条不变量是活的。

    这是确定性判定：不依赖 RST 的概率复现。根因是鉴权先于读体，
    拒绝时请求体还压在接收缓冲区里，close 就会被 Windows 变成 RST，
    客户端偶发 ConnectionAbortedError（实测未鉴权 POST 600 次挂 50 次）。
    """
    from relayhub.gateway.service import RelayHandler

    monkeypatch.setattr(RelayHandler, "unread_body_closes", 0)
    monkeypatch.setattr(RelayHandler, "_drain_request_body", lambda self: None)

    aborted = 0
    with a2a_downstream() as url:
        with relay_with_a2a(tmp_path, url) as base:
            for _ in range(3):
                try:
                    status, _reply = _rpc(
                        base,
                        {"jsonrpc": "2.0", "id": 1, "method": "message/send",
                         "params": {"agent": "peer"}},
                        credential=None,
                    )
                    assert status == 401
                except OSError:
                    # 正是这个缺陷的表现：客户端没读到 401，只看到连接被中止
                    aborted += 1

    # 三次请求都应留下未读请求体；读数与客户端有没有被中止无关
    assert _wait_unread_counter(3) == 3, (
        f"计数器只记到 {_wait_unread_counter(0, 0)}/3 次：不变量本身失效了（客户端被中止 {aborted} 次）"
    )


def test_rejections_leave_no_unread_body(tmp_path: Path, monkeypatch) -> None:
    """拒绝路径（401 / 404 / 400）都必须把请求体读干净，否则关连接会变 RST。"""
    from relayhub.gateway.service import RelayHandler

    monkeypatch.setattr(RelayHandler, "unread_body_closes", 0)

    with a2a_downstream() as url:
        with relay_with_a2a(tmp_path, url) as base:
            # 未鉴权
            assert _rpc(base, {"jsonrpc": "2.0", "id": 1, "method": "message/send",
                               "params": {"agent": "peer"}}, credential=None)[0] == 401
            # 鉴权通过但请求体不是合法 JSON
            bad = urllib.request.Request(f"{base}/a2a", data=b"{not json",
                                         method="POST")
            bad.add_header("Content-Type", "application/json")
            bad.add_header("x-api-key", API_KEY)
            try:
                urllib.request.urlopen(bad, timeout=10).read()
            except urllib.error.HTTPError as exc:
                exc.read()
            # 未知路径
            miss = urllib.request.Request(f"{base}/nope", data=b'{"x":1}', method="POST")
            miss.add_header("Content-Type", "application/json")
            try:
                urllib.request.urlopen(miss, timeout=10).read()
            except urllib.error.HTTPError as exc:
                exc.read()

    assert _wait_unread_counter(0, timeout=0.3) == 0, "有请求体没读完就关连接，客户端会读不到响应"
