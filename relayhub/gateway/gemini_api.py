"""Gemini 公开 API 协议（API Key）：Anthropic 内部形态 ↔ v1beta generateContent。

与 `gemini-codeassist`（订阅 OAuth → v1internal，私有）分开：这里只认
`x-goog-api-key`，端点 `{base}/v1beta/models/{model}:generateContent` 与
`:streamGenerateContent?alt=sse`。响应/流统一翻译回 Anthropic 形态，好让网关
对下游保持单一内部表示。
"""

from __future__ import annotations

import json
import ssl
import uuid
from http.client import HTTPConnection, HTTPSConnection
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from .pool import UpstreamKey


class GeminiApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                out.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                out.append(part)
        return "".join(out)
    return ""


def to_gemini_request(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """Anthropic 形态（含 tools/tool_use/tool_result）→ Gemini generateContent 请求体。"""
    declarations = []
    for tool in payload.get("tools") or []:
        if isinstance(tool, dict) and tool.get("name"):
            declarations.append(
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                }
            )

    id_to_name: dict[str, str] = {}
    for message in payload.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    id_to_name[str(part.get("id"))] = str(part.get("name"))

    contents: list[dict[str, Any]] = []
    for message in payload.get("messages") or []:
        role = "model" if message.get("role") == "assistant" else "user"
        content = message.get("content")
        parts: list[dict[str, Any]] = []
        if isinstance(content, str):
            if content:
                parts.append({"text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text" and part.get("text"):
                    parts.append({"text": part["text"]})
                elif ptype == "tool_use":
                    parts.append(
                        {
                            "functionCall": {
                                "name": part.get("name"),
                                "args": part.get("input") or {},
                            }
                        }
                    )
                elif ptype == "tool_result":
                    name = id_to_name.get(str(part.get("tool_use_id")), str(part.get("tool_use_id")))
                    inner = part.get("content")
                    if not isinstance(inner, str):
                        inner = _text_of(inner)
                    parts.append({"functionResponse": {"name": name, "response": {"result": inner}}})
        if parts:
            contents.append({"role": role, "parts": parts})

    request: dict[str, Any] = {"contents": contents}
    system_text = _text_of(payload.get("system"))
    if system_text.strip():
        request["systemInstruction"] = {"parts": [{"text": system_text}]}
    if declarations:
        request["tools"] = [{"functionDeclarations": declarations}]

    generation: dict[str, Any] = {}
    if payload.get("max_tokens"):
        generation["maxOutputTokens"] = int(payload["max_tokens"])
    for source, target in (("temperature", "temperature"), ("top_p", "topP")):
        if payload.get(source) is not None:
            generation[target] = payload[source]
    if payload.get("stop_sequences"):
        generation["stopSequences"] = list(payload["stop_sequences"])
    if generation:
        request["generationConfig"] = generation
    return request


def _message(data: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    body = data.get("response") if isinstance(data.get("response"), dict) else data
    candidates = body.get("candidates") or []
    candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
    content = candidate.get("content") or {}
    blocks: list[dict[str, Any]] = []
    for part in content.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if "text" in part:
            if str(part["text"]).strip() or not any(b.get("type") == "text" for b in blocks):
                blocks.append({"type": "text", "text": str(part["text"] or "")})
        elif "functionCall" in part:
            call = part["functionCall"] or {}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "name": call.get("name"),
                    "input": call.get("args") or {},
                }
            )
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    usage = body.get("usageMetadata") or {}
    tokens_in = int(usage.get("promptTokenCount") or 0)
    tokens_out = int(usage.get("candidatesTokenCount") or 0)
    stop = "tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn"
    message = {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": candidate.get("modelVersion") or "",
        "content": blocks,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    }
    return message, tokens_in, tokens_out


def _base(key: UpstreamKey) -> str:
    return (key.base_url.strip() or "https://generativelanguage.googleapis.com").rstrip("/")


def _open(url: str, timeout: float):
    parts = urlsplit(url)
    if parts.scheme == "http":
        return HTTPConnection(parts.hostname or "", parts.port or 80, timeout=timeout), parts
    return HTTPSConnection(parts.hostname or "", parts.port or 443, timeout=timeout), parts


def _headers(key: UpstreamKey, accept_sse: bool) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-goog-api-key": key.api_key,
        "Accept": "text/event-stream" if accept_sse else "application/json",
    }


def call(key: UpstreamKey, payload: dict[str, Any], timeout: float) -> tuple[dict[str, Any], int, int]:
    model = str(payload.get("model") or "")
    body = json.dumps(to_gemini_request(payload, model), ensure_ascii=False).encode("utf-8")
    url = f"{_base(key)}/v1beta/models/{model}:generateContent"
    connection, parts = _open(url, timeout)
    try:
        connection.request("POST", parts.path, body=body, headers=_headers(key, False))
        response = connection.getresponse()
        raw = response.read().decode("utf-8", errors="replace")
        if response.status != 200:
            raise GeminiApiError(response.status, raw[:400] or "<空响应体>")
        return _message(json.loads(raw))
    except (OSError, ssl.SSLError) as exc:
        raise GeminiApiError(0, f"连接上游失败：{exc}") from exc
    finally:
        connection.close()


def stream(
    key: UpstreamKey,
    payload: dict[str, Any],
    timeout: float,
    on_usage: Callable[[int, int], None] | None = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    model = str(payload.get("model") or "")
    body = json.dumps(to_gemini_request(payload, model), ensure_ascii=False).encode("utf-8")
    url = f"{_base(key)}/v1beta/models/{model}:streamGenerateContent?alt=sse"
    connection, parts = _open(url, timeout)
    tokens_in = tokens_out = 0
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield "message_start", {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    started = False
    stop_reason = "end_turn"
    try:
        connection.request("POST", parts.path, body=body, headers=_headers(key, True))
        response = connection.getresponse()
        if response.status != 200:
            raise GeminiApiError(response.status, response.read()[:400].decode("utf-8", "replace"))
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            data = json.loads(chunk)
            body_of = data.get("response") if isinstance(data.get("response"), dict) else data
            usage = body_of.get("usageMetadata") or {}
            if usage:
                tokens_in = int(usage.get("promptTokenCount") or tokens_in)
                tokens_out = int(usage.get("candidatesTokenCount") or tokens_out)
            candidates = body_of.get("candidates") or []
            if not candidates:
                continue
            candidate = candidates[0]
            for part in (candidate.get("content") or {}).get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if "text" in part and part["text"]:
                    if not started:
                        started = True
                        yield "content_block_start", {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        }
                    yield "content_block_delta", {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": str(part["text"])},
                    }
                elif "functionCall" in part:
                    call = part["functionCall"] or {}
                    if started:
                        yield "content_block_stop", {"type": "content_block_stop", "index": 0}
                        started = False
                    stop_reason = "tool_use"
                    yield "content_block_start", {
                        "type": "content_block_start",
                        "index": 1,
                        "content_block": {
                            "type": "tool_use",
                            "id": f"call_{uuid.uuid4().hex[:16]}",
                            "name": call.get("name"),
                            "input": {},
                        },
                    }
                    yield "content_block_delta", {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(call.get("args") or {}, ensure_ascii=False),
                        },
                    }
                    yield "content_block_stop", {"type": "content_block_stop", "index": 1}
            if candidate.get("finishReason"):
                stop_reason = "tool_use" if stop_reason == "tool_use" else "end_turn"
    finally:
        connection.close()
    if started:
        yield "content_block_stop", {"type": "content_block_stop", "index": 0}
    yield "message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": tokens_out, "input_tokens": tokens_in},
    }
    yield "message_stop", {"type": "message_stop"}
    if on_usage:
        on_usage(tokens_in, tokens_out)


# ---------------------------------------------------------------- 入站（客户端用 Gemini 原生协议调我们）


def from_gemini_request(payload: dict[str, Any], model: str, *, streaming: bool = False) -> dict[str, Any]:
    """Gemini generateContent 请求 → 内部 Anthropic 形态。"""
    system_parts: list[str] = []
    for key in ("systemInstruction", "system_instruction"):
        block = payload.get(key) or {}
        if isinstance(block, dict):
            for part in block.get("parts") or []:
                if isinstance(part, dict) and part.get("text"):
                    system_parts.append(str(part["text"]))

    messages: list[dict[str, Any]] = []
    for item in payload.get("contents") or []:
        if not isinstance(item, dict):
            continue
        role = "assistant" if item.get("role") == "model" else "user"
        blocks: list[dict[str, Any]] = []
        for part in item.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if "text" in part:
                blocks.append({"type": "text", "text": str(part.get("text") or "")})
            elif "functionCall" in part:
                call = part["functionCall"] or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": f"call_{uuid.uuid4().hex[:16]}",
                        "name": call.get("name"),
                        "input": call.get("args") or {},
                    }
                )
            elif "functionResponse" in part:
                resp = part["functionResponse"] or {}
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": str(resp.get("name") or ""),
                        "content": json.dumps(resp.get("response") or {}, ensure_ascii=False),
                    }
                )
        if blocks:
            messages.append({"role": role, "content": blocks})

    tools: list[dict[str, Any]] = []
    for group in payload.get("tools") or []:
        if not isinstance(group, dict):
            continue
        for decl in group.get("functionDeclarations") or []:
            if not isinstance(decl, dict) or not decl.get("name"):
                continue
            tools.append(
                {
                    "name": decl["name"],
                    "description": decl.get("description", ""),
                    "input_schema": decl.get("parameters") or {"type": "object", "properties": {}},
                }
            )

    generation = payload.get("generationConfig") or {}
    out: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": int(generation.get("maxOutputTokens") or 4096),
        "stream": bool(streaming or payload.get("stream")),
    }
    if system_parts:
        out["system"] = chr(10).join(system_parts)
    if generation.get("temperature") is not None:
        out["temperature"] = generation["temperature"]
    if generation.get("topP") is not None:
        out["top_p"] = generation["topP"]
    if generation.get("stopSequences"):
        out["stop_sequences"] = list(generation["stopSequences"])
    if tools:
        out["tools"] = tools
    return out


def to_gemini_response(message: dict[str, Any], model: str) -> dict[str, Any]:
    """Anthropic message → Gemini generateContent 响应。"""
    parts: list[dict[str, Any]] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append({"text": str(block.get("text") or "")})
        elif block.get("type") == "tool_use":
            parts.append({"functionCall": {"name": block.get("name"), "args": block.get("input") or {}}})
    usage = message.get("usage") or {}
    tokens_in = int(usage.get("input_tokens") or 0)
    tokens_out = int(usage.get("output_tokens") or 0)
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": tokens_in,
            "candidatesTokenCount": tokens_out,
            "totalTokenCount": tokens_in + tokens_out,
        },
        "modelVersion": model,
    }


def to_gemini_chunks(events: Any) -> Iterator[dict[str, Any]]:
    """Anthropic SSE 事件 → Gemini streamGenerateContent 的分片。"""
    for event, payload in events or ():
        if event == "content_block_delta":
            delta = payload.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                yield {"candidates": [{"content": {"role": "model", "parts": [{"text": delta["text"]}]}}]}
        elif event == "message_delta":
            usage = payload.get("usage") or {}
            yield {
                "candidates": [{"finishReason": "STOP", "index": 0}],
                "usageMetadata": {
                    "promptTokenCount": int(usage.get("input_tokens") or 0),
                    "candidatesTokenCount": int(usage.get("output_tokens") or 0),
                },
            }
