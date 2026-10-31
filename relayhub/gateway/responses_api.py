# -*- coding: utf-8 -*-
"""OpenAI **Responses API** ⇄ 本站内部形态（Anthropic Messages）的翻译。

为什么需要一层翻译而不是另开一条转发链路：站内的鉴权、配额、记账、缓存、
故障切换都只认一条路径。再抄一条 = 两套计费，迟早对不上账。所以客户端说
Responses，进站先翻成 Messages，出站再翻回 Responses。

覆盖范围（够客户端跑起来的最小闭集）：
  * `input`：纯字符串、消息数组、`input_image` 图片块（url / base64 data URL）、
    `function_call` / `function_call_output` 回填；
  * `instructions` → system；
  * `max_output_tokens` / `temperature` / `top_p`；
  * `reasoning.effort` → 上游的 `reasoning_effort`；
  * `tools`（function 型）→ Anthropic tools；
  * 出站：文本 + `function_call` 输出项，usage 折算。

**故意不做**：`store` / 服务端会话延续（`previous_response_id`）、内置工具
（web_search、file_search、code_interpreter）。它们需要服务端持久化状态，
本站是无状态转发，假装支持只会让客户端拿到"有时能续上有时续不上"的体验。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterator

# Responses 的推理档位 → 上游 reasoning_effort。两者名字恰好一致，
# 但这里显式列出常见值，避免将来上游改词时没人知道要改哪儿。
# 不拿这张表**卡死**透传：站点自己广告的档位词（max/off/balanced……）也该
# 原样送到上游，让上游裁决——静默丢掉只会让用户以为开了深度思考。
_COMMON_EFFORTS = ("minimal", "low", "medium", "high")


def _push_text(blocks: list[dict[str, Any]], text: str) -> None:
    """文本追加进块数组，与上一段连续文本合并（Messages 不接受碎文本块）。"""
    if blocks and blocks[-1]["type"] == "text":
        blocks[-1]["text"] += text
    else:
        blocks.append({"type": "text", "text": text})


def _responses_image_to_anthropic(image_url: Any) -> dict[str, Any] | None:
    """Responses 的 input_image.image_url → Anthropic image 块。

    两种形状都收：裸 URL 字符串 / `{"url": ...}`。data URL（base64）拆成
    base64 source，普通 http(s) URL 用 url source（上游自己拉）。
    认不出返回 None——调用方负责丢图时别把文本一起丢。
    """
    url = image_url.get("url") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if url.startswith("data:"):
        head, _, data = url[len("data:") :].partition(",")
        media = head.split(";", 1)[0] or "image/png"
        if not data:
            return None
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _anthropic_content(content: Any) -> Any:
    """Responses 的 content → Anthropic 消息 content。

    纯文本返回字符串（Messages 线协议的最简形状，历史用例不变）；带
    `input_image` 时返回块数组，按原顺序穿插文本与图片。图片**不能静默丢**：
    客户端发图就是为了让模型看，吞掉只会得到文不对题的答案且不报错。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            if item:
                _push_text(blocks, item)
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in (None, "input_text", "output_text", "text"):
            text = item.get("text")
            if isinstance(text, str) and text:
                _push_text(blocks, text)
        elif kind == "input_image":
            image = _responses_image_to_anthropic(item.get("image_url"))
            if image is not None:
                blocks.append(image)
    if not blocks:
        return ""
    if len(blocks) == 1 and blocks[0]["type"] == "text":
        return blocks[0]["text"]
    return blocks


def _json_object(raw: Any) -> dict[str, Any]:
    """`arguments` 是 JSON 字符串；坏 JSON 当空对象，别让整条请求 500。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _tools_to_anthropic(tools: Any) -> list[dict[str, Any]]:
    """只翻 function 型；内置工具在这里被丢弃（见模块说明）。"""
    out: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "description": str(tool.get("description") or ""),
                "input_schema": tool.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return out


def to_anthropic_request(body: dict[str, Any]) -> dict[str, Any]:
    """Responses 请求 → Anthropic Messages 请求。

    只做形状转换，不做校验：上游会用自己的错误信息回话，
    在这里重复一遍校验只会让错误更难懂。
    """
    out: dict[str, Any] = {"model": str(body.get("model") or ""), "messages": []}

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        out["system"] = instructions

    messages: list[dict[str, Any]] = []
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "function_call":
                # 上一轮的助手工具调用：并成一条 assistant 的 tool_use 块。
                messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": str(item.get("call_id") or item.get("id") or ""),
                                "name": str(item.get("name") or ""),
                                "input": _json_object(item.get("arguments")),
                            }
                        ],
                    }
                )
                continue
            if item_type == "function_call_output":
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": str(item.get("call_id") or ""),
                                "content": str(item.get("output") or ""),
                            }
                        ],
                    }
                )
                continue
            role = str(item.get("role") or "user")
            content = _anthropic_content(item.get("content"))
            if content:
                messages.append({"role": role, "content": content})

    out["messages"] = messages or [{"role": "user", "content": ""}]

    if body.get("max_output_tokens") is not None:
        try:
            out["max_tokens"] = int(body["max_output_tokens"])
        except (TypeError, ValueError):
            pass
    for field in ("temperature", "top_p"):
        if body.get(field) is not None:
            out[field] = body[field]

    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        effort = str(reasoning.get("effort") or "").strip().lower()
        # 透传而不是查表卡死：站点广告的词（max/off/balanced……）原样上送，
        # 上游不认会自己报错——比静默丢掉好排查（见 _COMMON_EFFORTS 注释）。
        if effort:
            out["reasoning_effort"] = effort

    tools = _tools_to_anthropic(body.get("tools"))
    if tools:
        out["tools"] = tools
    return out


def new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex[:24]}"


def to_response_object(
    message: dict[str, Any], model: str, response_id: str | None = None
) -> dict[str, Any]:
    """Anthropic message → Responses 对象。"""
    response_id = response_id or new_response_id()
    blocks = message.get("content") or []
    text_parts: list[str] = []
    output: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            calls.append(block)

    text = "".join(text_parts)
    if text:
        output.append(
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:16]}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    for call in calls:
        output.append(
            {
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:16]}",
                "status": "completed",
                "call_id": str(call.get("id") or ""),
                "name": str(call.get("name") or ""),
                "arguments": json.dumps(
                    call.get("input") or {}, ensure_ascii=False
                ),
            }
        )

    usage = message.get("usage") or {}
    tokens_in = int(usage.get("input_tokens") or 0)
    tokens_out = int(usage.get("output_tokens") or 0)
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "status": "completed",
        "output": output,
        # 便捷字段：官方 SDK 会把 output 里的 output_text 拼成这一项，
        # 自己拼比让每个客户端各拼一遍可靠。
        "output_text": text,
        "usage": {
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "input_tokens_details": {"cached_tokens": int(usage.get("cache_read_input_tokens") or 0)},
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": tokens_in + tokens_out,
        },
        "incomplete_details": None,
    }


def to_response_events(
    events: Iterator[tuple[str, dict[str, Any]]],
    model: str,
    on_usage: Any = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Anthropic 事件流 → Responses 流事件。

    最小闭集：`response.created` / `response.output_text.delta` /
    `response.completed`。中间那些 `output_item.added`、`content_part.*`
    对多数客户端是噪声；少发不会错，多发一个客户端不认的事件反而可能报错。
    """
    response_id = new_response_id()
    text_parts: list[str] = []
    stop_reason = ""
    tokens_in = tokens_out = 0
    cache_read = cache_creation = 0
    started = False

    for name, payload in events:
        if name == "message_start":
            message = payload.get("message") or {}
            usage = message.get("usage") or {}
            tokens_in = int(usage.get("input_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
            if not started:
                started = True
                yield (
                    "response.created",
                    {
                        "type": "response.created",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "created_at": int(time.time()),
                            "model": model,
                            "status": "in_progress",
                            "output": [],
                        },
                    },
                )
            continue
        if name == "content_block_delta":
            delta = payload.get("delta") or {}
            piece = delta.get("text")
            if isinstance(piece, str) and piece:
                text_parts.append(piece)
                yield (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "response_id": response_id,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": piece,
                    },
                )
            continue
        if name == "message_delta":
            stop_reason = str((payload.get("delta") or {}).get("stop_reason") or stop_reason)
            usage = payload.get("usage") or {}
            if usage.get("output_tokens") is not None:
                tokens_out = int(usage["output_tokens"])
            continue

    text = "".join(text_parts)
    final = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "status": "completed",
        "output": (
            [
                {
                    "type": "message",
                    "id": f"msg_{uuid.uuid4().hex[:16]}",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            ]
            if text
            else []
        ),
        "output_text": text,
        "usage": {
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
        },
        "stop_reason": stop_reason,
    }
    if on_usage is not None:
        on_usage(tokens_in, tokens_out, cache_read, cache_creation)
    yield ("response.completed", {"type": "response.completed", "response": final})
