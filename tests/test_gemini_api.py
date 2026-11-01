"""gemini-api（公开 Gemini API Key）协议：非流式与流式都要能翻译成 Anthropic 形态。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from relayhub.gateway import gemini_api
from relayhub.gateway.pool import UpstreamKey

MODEL = "gemini-2.0-flash"
RESP = {
    "candidates": [
        {"content": {"role": "model", "parts": [{"text": "hello"}]}, "finishReason": "STOP"}
    ],
    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
}


class Handler(BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        assert self.headers.get("x-goog-api-key") == "k", "必须用 x-goog-api-key 鉴权"
        path = self.path.split("?")[0]
        Handler.seen.append(path)
        if path.endswith(":generateContent"):
            raw = json.dumps(RESP).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path.endswith(":streamGenerateContent"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in ("he", "llo"):
                chunk = {"candidates": [{"content": {"role": "model", "parts": [{"text": piece}]}}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            tail = {
                "candidates": [{"finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
            }
            self.wfile.write(f"data: {json.dumps(tail)}\n\n".encode())
            self.wfile.flush()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def server():
    Handler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _key(base: str) -> UpstreamKey:
    return UpstreamKey(
        key_id="g1",
        label="g",
        base_url=base,
        api_key="k",
        protocol="gemini-api",
        models=(MODEL,),
    )


def _payload() -> dict:
    return {
        "model": MODEL,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }


def test_non_stream_translates_text_and_usage(server: str) -> None:
    message, tokens_in, tokens_out = gemini_api.call(_key(server), _payload(), 10)
    assert message["content"] == [{"type": "text", "text": "hello"}]
    assert (tokens_in, tokens_out) == (3, 2)
    assert Handler.seen[-1].endswith(":generateContent")


def test_stream_emits_anthropic_events(server: str) -> None:
    chunks = list(gemini_api.stream(_key(server), _payload(), 10, None))
    names = [c[0] for c in chunks]
    assert names[0] == "message_start"
    assert "content_block_delta" in names
    assert names[-1] == "message_stop"
    text = "".join(
        c[1].get("delta", {}).get("text", "") for c in chunks if c[0] == "content_block_delta"
    )
    assert text == "hello"
    assert Handler.seen[-1].endswith(":streamGenerateContent")


def test_from_gemini_request_maps_text_system_tools_and_config():
    payload = {
        "systemInstruction": {"parts": [{"text": "be brief"}]},
        "contents": [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "f", "args": {"a": 1}}}]},
        ],
        "tools": [{"functionDeclarations": [{"name": "f", "parameters": {"type": "object"}}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 128, "topP": 0.9},
    }
    body = gemini_api.from_gemini_request(payload, "gemini-2.0-flash", streaming=True)
    assert body["model"] == "gemini-2.0-flash"
    assert body["system"] == "be brief"
    assert body["stream"] is True
    assert body["max_tokens"] == 128
    assert body["temperature"] == 0.3 and body["top_p"] == 0.9
    assert body["messages"][0]["content"] == [{"type": "text", "text": "hi"}]
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert body["tools"][0]["name"] == "f"


def test_to_gemini_response_and_chunks():
    message = {
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    out = gemini_api.to_gemini_response(message, "m")
    assert out["candidates"][0]["content"]["parts"] == [{"text": "ok"}]
    assert out["usageMetadata"]["totalTokenCount"] == 5
    chunks = list(gemini_api.to_gemini_chunks([("content_block_delta", {"delta": {"type": "text_delta", "text": "hi"}})]))
    assert chunks[0]["candidates"][0]["content"]["parts"][0]["text"] == "hi"
