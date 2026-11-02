"""入站 Gemini：客户端用 v1beta generateContent 调网关，拿到 Gemini 形态的响应。"""

from __future__ import annotations

import http.client
import json
import threading
from typing import Any

import pytest

from relayhub.gateway.service import DemoRouter, RelayServer, demo_pool

API_KEY = "rh_test_token"
MODEL = "glm-5.2"


@pytest.fixture()
def relay() -> str:
    server = RelayServer(("127.0.0.1", 0), DemoRouter(demo_pool()), api_key=API_KEY, event_delay=0.0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _conn(base: str) -> http.client.HTTPConnection:
    host, port = base.replace("http://", "").split(":")
    return http.client.HTTPConnection(host, int(port), timeout=20)


def _post(base: str, path: str, body: dict[str, Any]) -> tuple[int, bytes]:
    conn = _conn(base)
    raw = json.dumps(body).encode()
    conn.request(
        "POST",
        path,
        body=raw,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
            "x-api-key": API_KEY,
        },
    )
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def _payload(stream: bool = False) -> dict[str, Any]:
    return {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "generationConfig": {"maxOutputTokens": 64},
        **({"stream": True} if stream else {}),
    }


def test_generate_content_returns_gemini_shape(relay: str) -> None:
    status, raw = _post(relay, f"/v1beta/models/{MODEL}:generateContent", _payload())
    assert status == 200, raw[:300]
    data = json.loads(raw)
    parts = data["candidates"][0]["content"]["parts"]
    assert parts and "text" in parts[0]
    assert data["candidates"][0]["content"]["role"] == "model"
    assert "promptTokenCount" in data["usageMetadata"]


def test_stream_generate_content_emits_gemini_sse(relay: str) -> None:
    status, raw = _post(relay, f"/v1beta/models/{MODEL}:streamGenerateContent?alt=sse", _payload(True))
    assert status == 200, raw[:300]
    chunks = [
        json.loads(line[5:].strip())
        for line in raw.decode("utf-8", "replace").splitlines()
        if line.startswith("data:") and line[5:].strip()
    ]
    assert chunks, raw[:300]
    texts = "".join(
        part.get("text", "")
        for chunk in chunks
        for part in (chunk.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    )
    assert texts


def test_unknown_model_still_errors_in_gemini_shape(relay: str) -> None:
    status, raw = _post(relay, "/v1beta/models/no-such-model:generateContent", _payload())
    assert status in (404, 400, 500)
    data = json.loads(raw)
    assert "error" in data
