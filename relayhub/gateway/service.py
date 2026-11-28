"""中转站 HTTP 服务层。

负责：鉴权、路由分发、SSE 写入、把结果编码成客户端要的字节形态。
不负责：选哪个上游 Key、坏了怎么办、用量怎么记——那是 `router.py` + `pool.py`。

`DemoRouter` 是返回固定文本的假路由，只当测试靶子用。
真跑请用 `router.KeyPoolRouter`。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import traceback
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Iterator

from .. import paths
from . import drift as drift_module
from .. import __version__ as relayhub_version
from . import audit as audit_module
from . import mailer as mailer_module
from . import peer as peer_module
from . import a2a as a2a_module
from . import e2e as e2e_module
from . import mcp as mcp_module
from . import clients as clients_module
from . import pluginlogs
from . import landing as landing_module
from . import reqlog
from . import usageagg as usageagg_module
from . import wallpaper as wallpaper_module
from . import features as features_module
from . import gemini_api
from . import site as site_module
from . import responses_api
from . import toip as toip_module
from .router import RelayOutcome, RouterError

ident_module = None  # 未安装客户端扩展包时的占位：身份头整段跳过
from .tokens import (
    SCOPE_TEST,
    DownstreamToken,
    TokenError,
    TokenStore,
    generate_token,
    normalize_client_ip,
)
from .upstream import (
    UpstreamError,
    cache_from_usage,
    to_anthropic_request,
    tool_usage_from_usage,
)
from .users import RedeemStore, UserError, UserStore
from .users import can as user_can

ANTHROPIC_VERSION = "2023-06-01"

# 回错误响应前愿意读掉多少未读请求体。超出的直接放弃（大包体被拒时
# 宁可让客户端看到一次 RST，也不为了一个 401 去读 10MB）。
MAX_DRAIN_BODY = 1 << 20


class ChannelError(RuntimeError):
    """演示用渠道故障（真实上游错误见 upstream.UpstreamError）。"""


@dataclass
class Channel:
    name: str
    models: dict[str, int] = field(default_factory=dict)
    broken: bool = False
    failures: int = 0
    successes: int = 0

    def complete(self, model: str, prompt: str) -> str:
        if self.broken:
            self.failures += 1
            raise ChannelError(f"channel {self.name} is down")
        self.successes += 1
        return f"[{self.name}] 收到 {len(prompt)} 字符，模型 {model}。"


class ChannelPool:
    """演示用渠道池：轮询 + 失败跳过。不含真实上游调用。"""

    def __init__(self, channels: list[Channel]) -> None:
        if not channels:
            raise ValueError("渠道池不能为空")
        self.channels = channels
        self._cursor = 0

    def candidates(self) -> Iterator[Channel]:
        total = len(self.channels)
        start = self._cursor
        self._cursor = (self._cursor + 1) % total
        for offset in range(total):
            yield self.channels[(start + offset) % total]

    def complete(self, model: str, prompt: str) -> tuple[str, Channel]:
        last: ChannelError | None = None
        for channel in self.candidates():
            try:
                return channel.complete(model, prompt), channel
            except ChannelError as exc:
                last = exc
        raise ChannelError(f"所有渠道均失败：{last}")

    def known_models(self) -> dict[str, int]:
        merged: dict[str, int] = {}
        for channel in self.channels:
            for model, window in channel.models.items():
                merged.setdefault(model, window)
        return merged


# ---------------------------------------------------------------- 请求解析


def extract_prompt(body: dict[str, Any]) -> str:
    """从两种协议的请求体里取出最后一条用户消息的文本。"""
    messages = body.get("messages") or []
    for message in reversed(messages):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            if texts:
                return "".join(texts)
    if isinstance(body.get("input"), str):
        return body["input"]
    return ""


# ---------------------------------------------------------------- 响应编码


def anthropic_message(
    model: str, text: str, input_tokens: int = 12, output_tokens: int = 8
) -> dict[str, Any]:
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def anthropic_events(
    model: str,
    text: str,
    input_tokens: int = 12,
    output_tokens: int = 8,
    deltas: int = 1,
) -> list[tuple[str, dict[str, Any]]]:
    """Anthropic 流式事件序列。顺序与字段名必须严格正确，客户端才会接受。

    deltas>1 时把 text 均分成多段 delta（合成应答模拟真实生成节奏用），
    事件序列依然是合法形状：多个 content_block_delta 是真实模型的常态。
    """
    if deltas > 1:
        size = max(1, len(text) // deltas)
        pieces = [text[i : i + size] for i in range(0, len(text), size)][:deltas]
    else:
        pieces = [text] if text else [""]
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    ]
    for piece in pieces:
        events.append(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": piece},
                },
            )
        )
    events.extend(
        [
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": output_tokens},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )
    return events


# Anthropic stop_reason → OpenAI finish_reason（upstream.py 里那份是反方向，别混用）
_FINISH_REASON_FROM_ANTHROPIC = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}


def _message_text(message: dict[str, Any]) -> str:
    pieces: list[str] = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            pieces.append(str(block.get("text") or ""))
    return "".join(pieces)


def openai_completion(model: str, message: dict[str, Any]) -> dict[str, Any]:
    """Anthropic message → OpenAI chat.completion。

    路由层统一吐 Anthropic 形态（因为上游可能是 anthropic 也可能是 openai），
    所以「给 OpenAI 客户端的那一份」只能在这一层降级生成。
    message 里若含 tool_use 块，要还原成 OpenAI 的 tool_calls。
    """
    usage = message.get("usage") or {}
    tokens_in = int(usage.get("input_tokens") or 0)
    tokens_out = int(usage.get("output_tokens") or 0)
    text = _message_text(message)
    tool_uses = [
        block
        for block in message.get("content") or []
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    assistant: dict[str, Any] = {
        "role": "assistant",
        "content": text if not tool_uses else (text or ""),
    }
    if tool_uses:
        assistant["tool_calls"] = [
            {
                "id": block.get("id"),
                "type": "function",
                "function": {
                    "name": block.get("name"),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            }
            for block in tool_uses
        ]
    return {
        "id": str(message.get("id") or f"chatcmpl-{uuid.uuid4().hex[:20]}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(message.get("model") or model),
        "choices": [
            {
                "index": 0,
                "message": assistant,
                "finish_reason": _FINISH_REASON_FROM_ANTHROPIC.get(
                    str(message.get("stop_reason")), "stop"
                ),
            }
        ],
        "usage": {
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
        },
    }


# ---------------- Responses API（OpenAI 新口 /v1/responses） ----------------


def _responses_content_parts(content):
    """Responses 的 content → OpenAI chat 的 content。

    纯文本返回字符串（chat 线协议的最简形状）；带 `input_image` 时返回块数组
    （text + image_url parts）。图片**不能静默丢**：客户端发图就是为了让模型看，
    吞掉只会得到文不对题的答案且不报错——下游 `_openai_user_content_to_anthropic`
    认 image_url，会接着翻成 Anthropic 的 image 块。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    items = content if isinstance(content, list) else [content]
    parts = []
    for c in items:
        if isinstance(c, str):
            if c:
                if parts and parts[-1]["type"] == "text":
                    parts[-1]["text"] += c
                else:
                    parts.append({"type": "text", "text": c})
            continue
        if not isinstance(c, dict):
            continue
        kind = c.get("type")
        if kind in (None, "input_text", "output_text", "text"):
            text = str(c.get("text") or "")
            if text:
                if parts and parts[-1]["type"] == "text":
                    parts[-1]["text"] += text
                else:
                    parts.append({"type": "text", "text": text})
        elif kind == "input_image":
            url = c.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            if isinstance(url, str) and url.strip():
                parts.append({"type": "image_url", "image_url": {"url": url.strip()}})
    if not parts:
        return ""
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts


def responses_to_chat(body):
    """Responses 请求 → OpenAI chat 请求（随后走 to_anthropic_request 归一化）。

    input 支持 string 与 items 列表（message / function_call_output）；
    instructions 映射为 system；max_output_tokens 映射为 max_tokens；
    `input_image` 图片块翻成 image_url parts（不静默丢）；
    `reasoning.effort` 映射为 reasoning_effort（查表卡死只会让档位莫名不生效）。
    """
    msgs = []
    if body.get("instructions"):
        msgs.append({"role": "system", "content": str(body["instructions"])})
    inp = body.get("input")
    if isinstance(inp, str):
        msgs.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                msgs.append({"role": "user", "content": str(item)})
                continue
            itype = item.get("type", "message")
            if itype == "function_call_output":
                msgs.append({"role": "tool", "tool_call_id": str(item.get("call_id") or ""),
                             "content": str(item.get("output") or "")})
                continue
            if itype in ("reasoning", "item_reference"):
                continue  # 网关不存推理态，安全丢弃
            role = item.get("role", "user")
            if role not in ("user", "assistant", "system", "developer"):
                role = "user"
            content = _responses_content_parts(item.get("content"))
            if role == "developer":
                role = "system"
            msgs.append({"role": role, "content": content})
    else:
        msgs.append({"role": "user", "content": ""})

    chat = {"messages": msgs}
    for k in ("model", "temperature", "top_p", "stream", "tools", "tool_choice",
              "parallel_tool_calls", "user", "metadata"):
        if body.get(k) is not None:
            chat[k] = body[k]
    if body.get("max_output_tokens"):
        chat["max_tokens"] = body["max_output_tokens"]
    # 推理档位透传： Responses 的 reasoning.effort → chat 的 reasoning_effort，
    # 再由 to_anthropic_request 带进 IR、to_openai_request 映射给上游。
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        effort = str(reasoning.get("effort") or "").strip().lower()
        if effort:
            chat["reasoning_effort"] = effort
    return chat


def responses_completion(model, message):
    """Anthropic message（网关 IR）→ Responses 非流式应答。"""
    usage = message.get("usage") or {}
    tokens_in = int(usage.get("input_tokens") or 0)
    tokens_out = int(usage.get("output_tokens") or 0)
    text = _message_text(message)
    rid = "resp_" + uuid.uuid4().hex[:24]
    mid = "msg_" + uuid.uuid4().hex[:24]
    return {
        "id": rid,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": [{
            "type": "message", "id": mid, "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "output_text": text,
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out,
                  "total_tokens": tokens_in + tokens_out},
        "parallel_tool_calls": True,
    }


def responses_chunks(model, events):
    """Anthropic 事件流 → Responses SSE (event, payload) 序列（真增量）。

    事件序：response.created → output_item.added → content_part.added →
    每个 text_delta 一个 response.output_text.delta → text/part/item.done →
    response.completed（带 usage）。
    """
    rid = "resp_" + uuid.uuid4().hex[:24]
    mid = "msg_" + uuid.uuid4().hex[:24]
    created = int(time.time())
    seq = 0
    buf = ""
    tokens_in = tokens_out = 0

    def ev(name, payload):
        nonlocal seq
        seq += 1
        return (name, {"type": name, "sequence_number": seq, **payload})

    def resp_stub(status):
        return {"id": rid, "object": "response", "created_at": created,
                "status": status, "model": model, "output": [],
                "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out,
                          "total_tokens": tokens_in + tokens_out}}

    yield ev("response.created", {"response": resp_stub("in_progress")})
    yield ev("response.in_progress", {"response": resp_stub("in_progress")})
    yield ev("response.output_item.added", {
        "output_index": 0,
        "item": {"type": "message", "id": mid, "role": "assistant",
                 "status": "in_progress", "content": []},
    })
    yield ev("response.content_part.added", {
        "item_id": mid, "output_index": 0, "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    })
    for name, payload in events:
        if name == "message_start":
            u = (payload.get("message") or {}).get("usage") or {}
            tokens_in = int(u.get("input_tokens") or 0)
        elif name == "content_block_delta":
            d = payload.get("delta") or {}
            if d.get("type") == "text_delta":
                piece = d.get("text") or ""
                buf += piece
                yield ev("response.output_text.delta", {
                    "item_id": mid, "output_index": 0, "content_index": 0,
                    "delta": piece,
                })
        elif name == "message_delta":
            u = payload.get("usage") or {}
            tokens_out = int(u.get("output_tokens") or tokens_out)
    yield ev("response.output_text.done", {
        "item_id": mid, "output_index": 0, "content_index": 0, "text": buf,
    })
    yield ev("response.content_part.done", {
        "item_id": mid, "output_index": 0, "content_index": 0,
        "part": {"type": "output_text", "text": buf, "annotations": []},
    })
    yield ev("response.output_item.done", {
        "output_index": 0,
        "item": {"type": "message", "id": mid, "role": "assistant",
                 "status": "completed",
                 "content": [{"type": "output_text", "text": buf, "annotations": []}]},
    })
    done = resp_stub("completed")
    done["output"] = [{
        "type": "message", "id": mid, "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": buf, "annotations": []}],
    }]
    done["output_text"] = buf
    yield ev("response.completed", {"response": done})


def openai_chunks(
    model: str, events: Iterable[tuple[str, dict[str, Any]]]
) -> Iterator[dict[str, Any]]:
    """Anthropic 事件流 → OpenAI chat.completion.chunk 序列（不含末尾的 [DONE]）。

    真增量：每来一个 content_block_delta 立刻吐一个 chunk。若先把整条流收完再切分，
    OpenAI 客户端那边就退化成「等半天然后文字一次性出现」，SSE 白开了。
    """
    stream_id = f"chatcmpl-{uuid.uuid4().hex[:20]}"
    created = int(time.time())

    def chunk(
        delta: dict[str, Any],
        finish_reason: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": stream_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            item["usage"] = usage
        return item

    yield chunk({"role": "assistant", "content": ""})
    tokens_in = tokens_out = 0
    finish_reason = "stop"
    # Anthropic content 块索引 → OpenAI tool_calls 索引（文本块不占 tool_calls）
    tool_index_map: dict[int, int] = {}
    next_tool_idx = 0
    for name, payload in events:
        if name == "message_start":
            usage = (payload.get("message") or {}).get("usage") or {}
            tokens_in = int(usage.get("input_tokens") or 0)
            tokens_out = int(usage.get("output_tokens") or 0)
        elif name == "content_block_start":
            block = payload.get("content_block") or {}
            if block.get("type") == "tool_use":
                ai = int(payload.get("index") or 0)
                oi = tool_index_map.setdefault(ai, next_tool_idx)
                if oi == next_tool_idx:
                    next_tool_idx += 1
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": oi,
                                "id": block.get("id"),
                                "type": "function",
                                "function": {"name": block.get("name"), "arguments": ""},
                            }
                        ]
                    }
                )
        elif name == "content_block_delta":
            delta = payload.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta" and delta.get("text"):
                yield chunk({"content": str(delta["text"])})
            elif dtype == "input_json_delta":
                ai = int(payload.get("index") or 0)
                oi = tool_index_map.get(ai, 0)
                yield chunk(
                    {
                        "tool_calls": [
                            {"index": oi, "function": {"arguments": delta.get("partial_json", "")}}
                        ]
                    }
                )
        elif name == "message_delta":
            delta = payload.get("delta") or {}
            finish_reason = _FINISH_REASON_FROM_ANTHROPIC.get(
                str(delta.get("stop_reason")), finish_reason
            )
            usage = payload.get("usage") or {}
            # OpenAI 上游的 input(prompt) tokens 在流尾 message_delta 才出现；
            # Anthropic 上游则在 message_start 已给。两处都收，取最大值兜住。
            tokens_in = max(tokens_in, int(usage.get("input_tokens") or 0))
            tokens_out = max(tokens_out, int(usage.get("output_tokens") or 0))
        elif name == "error":
            error = payload.get("error") or {}
            raise ChannelError(str(error.get("message") or payload))
    yield chunk({}, finish_reason)
    yield chunk(
        {},
        None,
        usage={
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
        },
    )


class DemoRouter:
    """返回固定文本的假路由，仅用于测试与演示。"""

    def __init__(self, pool: ChannelPool) -> None:
        self.pool = pool

    def models(self) -> dict[str, int]:
        return self.pool.known_models()

    def relay(
        self,
        model: str,
        payload: dict[str, Any],
        stream: bool,
        trace_headers: dict[str, str] | None = None,
        fp: str | None = None,
    ) -> RelayOutcome:
        text, channel = self.pool.complete(model, extract_prompt(payload))
        if stream:
            return RelayOutcome(
                label=channel.name, model=model, events=iter(anthropic_events(model, text))
            )
        return RelayOutcome(
            label=channel.name, model=model, message=anthropic_message(model, text)
        )


class TestRouter:
    """测试密钥（scope=test）专用路由：只回本地合成应答，绝不触达真实上游。

    用途：把网关挂上公网给第三方做性能测试——他们测到的是中转站自身的
    吞吐 / 延迟 / SSE 行为，不烧渠道配额，也摸不到真实模型。
    设计取舍：
      * 任意模型名都接（性能测试不在乎模型存不存在），所以 models() 返回空表；
      * 应答体量跟 max_tokens 成比例，让不同规模的压测有可比的传输量；
      * usage 按字节数估算，形态真实但数字与任何真实模型无关。
    """

    LABEL = "test-echo"

    def models(self) -> dict[str, int]:
        return {}

    def relay(
        self,
        model: str,
        payload: dict[str, Any],
        stream: bool,
        trace_headers: dict[str, str] | None = None,
        fp: str | None = None,
    ) -> RelayOutcome:
        prompt = extract_prompt(payload)
        try:
            max_tokens = int(payload.get("max_tokens") or 256)
        except (TypeError, ValueError):
            max_tokens = 256
        max_tokens = max(16, min(max_tokens, 4096))
        # 粗略按 1 token ≈ 4 字节合成正文；流式切成 24 段模拟真实生成节奏
        text = f"[relay-hub test-echo model={model}] " + "x" * (max_tokens * 4)
        tokens_in = max(1, len(prompt) // 4)
        tokens_out = max_tokens
        if stream:
            return RelayOutcome(
                label=self.LABEL,
                model=model,
                events=iter(
                    anthropic_events(
                        model, text, input_tokens=tokens_in, output_tokens=tokens_out, deltas=24
                    )
                ),
            )
        return RelayOutcome(
            label=self.LABEL,
            model=model,
            message=anthropic_message(
                model, text, input_tokens=tokens_in, output_tokens=tokens_out
            ),
        )


class RateLimiter:
    """进程内滑动窗口限流（令牌 RPM）。

    刻意不落盘：RPM 是短窗行为控制，重启清零无伤大雅；要持久的是
    日配额，那在 tokens.py 的 usage 里。窗口按令牌全文分桶——
    token_id 在热加载后对象会换，字符串身份不会。
    """

    def __init__(self, window_seconds: float = 60.0) -> None:
        self._window = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def check(self, secret: str, rpm: int) -> bool:
        """放行 =True；超限 =False（且本次不计入，不惩罚重试者）。"""
        if rpm <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(secret, ()) if now - t < self._window]
            if len(hits) >= rpm:
                self._hits[secret] = hits
                return False
            hits.append(now)
            self._hits[secret] = hits
            # 顺手回收已清空的桶，防长跑膨胀
            if len(self._hits) > 1024:
                self._hits = {k: v for k, v in self._hits.items() if v}
            return True


class ConcurrencyGate:
    """并发闸门 + 有限排队（突发并发场景的「排队不拒绝」）+ 优先队列。

    纯 429 限流在突发场景体验很差：几十个客户端同时发起，只有前几个能拿到
    应答，剩下的直接失败还得手动重试。这里改成：在飞请求超过上限时进
    等待队列，排到就继续处理；只对两种情况才回 429——
      * 等待队列本身满了（说明排队也排不上了，硬拒）；
      * 排到了但一直没轮上（等到 queue_timeout 超时）。

    **优先队列**：acquire(priority=True) 的请求排 VIP 队——名额一释放先
    唤醒 VIP 等待者，普通队只在无 VIP 时被叫醒；新到的 VIP 更是直接插队
    （有空位就拿走，不等普通等待者醒来再抢）。谁是 VIP 由 policy.py 的
    优先名单决定（设备 ID / 令牌名 / IP），高峰时段管理员把自己或值班设备
    标优先即可。

    两个等待池共用同一把互斥锁（分别建 Condition），release 时刻意先叫
    VIP 队——这是唯一偏心的地方。进程内存态，重启清零；
    与 RateLimiter 互补——那个限「发起频率」，这个限「同时在跑多少个」。
    """

    def __init__(self, max_in_flight: int, queue_size: int, queue_timeout: float) -> None:
        self._max_in_flight = max(1, max_in_flight)
        self._queue_size = max(0, queue_size)
        self._queue_timeout = max(0.0, queue_timeout)
        mutex = threading.Lock()
        self._prio = threading.Condition(mutex)
        self._norm = threading.Condition(mutex)
        self._in_flight = 0
        self._prio_waiting = 0
        self._norm_waiting = 0

    def acquire(self, priority: bool = False) -> bool:
        """拿到并发名额 =True；队列满/排队超时 =False。priority=True 排 VIP 队。"""
        if self._queue_timeout <= 0 and self._in_flight >= self._max_in_flight:
            return False  # 配置成不排队：满即拒
        deadline = time.monotonic() + self._queue_timeout
        cond = self._prio if priority else self._norm
        with self._prio:  # 两把 Condition 同一把锁，用哪把进锁都一样
            if self._in_flight < self._max_in_flight:
                self._in_flight += 1
                return True
            if self._prio_waiting + self._norm_waiting >= self._queue_size:
                return False  # 队列也满了：硬拒，别让等待者无限堆积
            if priority:
                self._prio_waiting += 1
            else:
                self._norm_waiting += 1
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False  # 排到了但没轮上：超时拒绝
                    got = cond.wait(remaining)
                    if got and self._in_flight < self._max_in_flight:
                        self._in_flight += 1
                        return True
                    # got=False → 本次等到超时，下轮 remaining<=0 收尾；
                    # got=True 但名额被别的等待者抢走 → 回去继续等
            finally:
                if priority:
                    self._prio_waiting -= 1
                else:
                    self._norm_waiting -= 1

    def release(self) -> None:
        with self._prio:
            self._in_flight = max(0, self._in_flight - 1)
            # 先叫 VIP：VIP 等待者没被叫醒时名额留给普通队
            if self._prio_waiting > 0:
                self._prio.notify()
            elif self._norm_waiting > 0:
                self._norm.notify()

    @property
    def stats(self) -> dict[str, int]:
        with self._prio:
            return {
                "in_flight": self._in_flight,
                "waiting": self._prio_waiting + self._norm_waiting,
                "priority_waiting": self._prio_waiting,
                "max_in_flight": self._max_in_flight,
                "queue_size": self._queue_size,
            }


_DEFAULT_BG = "background:#F7F6F3;"

# 转发链路 Hops 上限：默认 4 覆盖「官方/本地上游 + 两三级中转」的真实拓扑。
# 环境变量 RELAYHUB_MAX_HOPS 可调大——级联更多家中转站时放宽，不是判环的唯一防线。
try:
    MAX_HOPS = max(1, int(os.environ.get("RELAYHUB_MAX_HOPS") or 4))
except ValueError:
    MAX_HOPS = 4

# 判环时回给客户端的状态码。
#
# 曾经是 508（Loop Detected）。换成 429 的理由：508 出自 RFC 5842（WebDAV
# 扩展），OpenAI / Anthropic 的 SDK 错误表里没有它，客户端只会当成"未知错误"
# 抛出来；而环路的正确客户端动作就是**别再发了、退避**，这正是 429 的语义，
# 各家 SDK 都有成熟的退避重试处理。拒请求的时机与日志原因不变（仍是立即拒、
# 日志里仍写 relay loop），可诊断性不丢。
#
# 若将来需要把"环"和"真限流"在客户端侧区分开，改这里一个常量即可；
# 想回到 508 也只改这一行。
RELAY_LOOP_STATUS = 429


_WALLPAPER_CACHE: dict[str, tuple[float, str]] = {}
_WALLPAPER_TTL = 30.0


def _fetch_wallpaper_url() -> str:
    """服务端代取壁纸地址：上游 Key 只在这里出现，绝不下发给浏览器。"""
    import urllib.request

    wp = _panel_wallpaper()
    if wp.get("mode") != "api" or not wp.get("api_url"):
        return ""
    api = wp["api_url"]
    now = time.time()
    cached = _WALLPAPER_CACHE.get(api)
    if cached and now - cached[0] < _WALLPAPER_TTL:
        return cached[1]
    headers = {"accept": "application/json, text/plain;q=0.9"}
    if wp.get("api_key"):
        header = wp.get("api_header") or "Authorization"
        value = wp["api_key"]
        if header.lower() == "authorization" and not value.lower().startswith(("bearer ", "basic ")):
            value = "Bearer " + value
        headers[header] = value
    try:
        request = urllib.request.Request(api, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=6) as response:
            ctype = (response.headers.get("content-type") or "").lower()
            final = response.geturl()
            # 302 直接重定向到图片：最终地址就是壁纸，不用读正文
            if ctype.startswith("image/"):
                url = wallpaper_module.clean_image(final) if final.startswith("http") else ""
                if url:
                    _WALLPAPER_CACHE[api] = (now, url)
                return url
            raw = response.read(4096).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 取不到就安静降级，不把上游错误带给浏览器
        return cached[1] if cached else ""
    url = wallpaper_module.pick_url(raw, allow_http=bool(wp.get("allow_http")))
    if url:
        _WALLPAPER_CACHE[api] = (now, url)
    return url


def _panel_bg_css() -> str:
    """从 webui.json 读外观配置，生成 body 背景 CSS。

    尺寸规则常驻（contain + 居中 + 不重复）：API 模式下图片由 JS 在运行时设置，
    若这里不给尺寸，浏览器会按默认 repeat 平铺，比例全乱。
    """
    try:
        cfg = json.loads(paths.webui_theme_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _DEFAULT_BG
    color = str(cfg.get("bg_color") or "")
    wp = wallpaper_module.normalize(cfg)
    css = ""
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", color):
        css += f"background-color:{color};"
    image = wp["images"][0] if wp["images"] else ""
    fit = str(cfg.get("bg_fit") or "cover").strip().lower()
    if fit not in ("cover", "contain"):
        fit = "cover"
    if wp["mode"] != "static" or image:
        css += (
            f"background-size:{fit};background-repeat:no-repeat;"
            "background-attachment:fixed;background-position:center;"
        )
    if image:
        css += f"background-image:url('{image}');"
    return css or _DEFAULT_BG


_BLUR_LAYER_CSS = (
    "body::before{content:\"\";position:fixed;inset:-40px;z-index:-1;pointer-events:none;"
    "background-image:inherit;background-size:cover;background-position:center;"
    "filter:blur(28px) saturate(1.15) brightness(.62);transform:scale(1.06);}"
)


def _panel_wallpaper() -> dict:
    """读一次 webui.json 并归一化（渲染层唯一入口）。"""
    try:
        cfg = json.loads(paths.webui_theme_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return wallpaper_module.normalize({})
    return wallpaper_module.normalize(cfg)


def _apply_panel_theme(html: str) -> str:
    """把背景 CSS 与壁纸脚本注入面板模板（不铺伪元素层：它会盖住内容）。"""
    bg = _panel_bg_css()
    cfg = _panel_wallpaper()
    script = wallpaper_module.rotate_script(cfg)
    html = html.replace("__THEME_BG__", bg, 1)
    if cfg.get("images"):
        # 面板卡片默认不透明，壁纸只在登录页露出来。有壁纸时把 --panel 换成
        # 半透明并加一点背景模糊：既看得见图，表格文字也不糊。
        html = html.replace(
            "</head>",
            "<style id=\"rh-panel-glass\">"
            ":root{--panel:rgba(255,255,255,.86)}"
            "@media(prefers-color-scheme:dark){:root{--panel:rgba(23,24,27,.86)}}"
            ".card,.stat,#pn{backdrop-filter:blur(6px)}"
            "</style></head>",
            1,
        )
    if script:
        if "__THEME_JS__" in html:
            html = html.replace("__THEME_JS__", script, 1)
        else:
            html = html.replace("</body>", script + "</body>", 1)
    return html


def _theme_vars(config) -> str:
    """只输出 CSS 变量；颜色已在 site.sanitize_color 里限成 #rgb/#rrggbb。"""
    rules = []
    if config.theme_primary:
        rules.append(f"--brand:{config.theme_primary}")
    if config.theme_bg:
        rules.append(f"--bg:{config.theme_bg}")
    return ("<style>:root{" + ";".join(rules) + "}</style>") if rules else ""


ANNOUNCEMENT_SCRIPT = """<script>
fetch('/public/announcements').then(function(r){return r.json();}).then(function(a){
  if(!a || !a.items || !a.items.length) return;
  var a0=a.items[0];
  var colors={info:'#2563eb',warning:'#d97706',critical:'#dc2626'};
  var bar=document.createElement('div');
  bar.textContent=(a0.pinned?'【置顶】':'')+a0.text;
  bar.setAttribute('role','status');
  bar.style.cssText='position:fixed;top:0;left:0;right:0;z-index:9999;padding:10px 16px;'
    +'text-align:center;font-size:14px;line-height:1.4;color:#fff;background:'
    +(colors[a0.level]||colors.info)+';box-shadow:0 1px 6px rgba(0,0,0,.15)';
  document.body.prepend(bar);
}).catch(function(){});
</script>"""

PANEL_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23185FA5'/%3E%3Cpath d='M9 23V9h5l9 9v5h-5l-9-9v9z' fill='%23fff'/%3E%3C/svg%3E">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__SITE__ 用户面板</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--fg:#111827;--muted:#6b7280;--line:#e5e7eb;
--brand:#2563eb;--brand-weak:#eff4ff;--ok:#16815a;--warn:#b5770a;--bad:#c0392b;
--r:10px;--ctl:34px}
@media(prefers-color-scheme:dark){:root{--bg:#101113;--panel:#17181b;--fg:#e8e8ea;
--muted:#9aa1ab;--line:#2a2c30;--brand:#5b8def;--brand-weak:#1a2333}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);__THEME_BG__color:var(--fg);
font:13.5px/1.6 system-ui,"Segoe UI","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
h2{font-size:16px;font-weight:600;margin:0 0 4px}
h3{font-size:13.5px;font-weight:600;margin:18px 0 8px}
p.sub,.sub{color:var(--muted);font-size:12px;margin:0 0 10px}
a{color:var(--brand)}
/* 登录 / 注册 */
.auth-wrap{display:flex;justify-content:center;align-items:flex-start;min-height:100vh;padding:48px 16px}
.box{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:28px;
width:100%;max-width:400px;box-shadow:0 8px 28px rgba(17,24,39,.06)}
/* 主框架 */
.shell{display:grid;grid-template-columns:196px minmax(0,1fr);gap:18px;max-width:1200px;
margin:0 auto;padding:20px 22px 60px;align-items:start}
aside{position:sticky;top:20px}
.brand{font-weight:600;font-size:14px;padding:6px 10px 12px;letter-spacing:.2px}
.brand .sub{display:block;font-weight:400;margin:2px 0 0}
#pn{display:flex;flex-direction:column;gap:2px;background:var(--panel);border:1px solid var(--line);
border-radius:var(--r);padding:6px}
#pn button{height:32px;text-align:left;padding:0 10px;border:0;background:transparent;color:var(--muted);
border-radius:8px;font:inherit;cursor:pointer;transition:background .12s,color .12s}
#pn button:hover{background:var(--line);color:var(--fg)}
#pn button.on{background:var(--brand-weak);color:var(--brand);font-weight:600}
.side-foot{margin-top:10px;padding:0 4px}
/* 面板块 */
.pane{display:none}
.pane.on{display:block}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:12px}
/* 控件 */
label{display:flex;flex-direction:column;gap:4px;font-size:11.5px;color:var(--muted);min-width:0}
input,select,textarea{height:var(--ctl);font:inherit;font-size:13px;color:var(--fg);background:var(--panel);
border:1px solid var(--line);border-radius:8px;padding:0 10px;width:100%}
textarea{height:auto;padding:8px 10px}
input:focus,select:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px var(--brand-weak)}
button{height:var(--ctl);font:inherit;font-size:13px;padding:0 12px;border-radius:8px;cursor:pointer;
border:1px solid var(--line);background:var(--panel);color:var(--fg);white-space:nowrap}
button:hover{border-color:var(--muted)}
button.primary{background:var(--brand);border-color:var(--brand);color:#fff}
button.ghost{background:transparent}
button.link{border:0;background:none;color:var(--brand);padding:0;height:auto}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.row label{flex:1 1 170px}
.row>button{flex:0 0 auto}
/* 表格 */
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;font-weight:600;font-size:11.5px;color:var(--muted);padding:7px 8px;
border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:hover td{background:var(--brand-weak)}
td.mono,th.mono,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
td.nowrap{white-space:nowrap}
/* 统计块 */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:12px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.stat .k{font-size:11.5px;color:var(--muted)}
.stat .v{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.3}
/* 卡片网格（模型广场 / 排行榜） */
.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(228px,1fr));gap:12px}
.mcard{border:1px solid var(--line);border-radius:12px;padding:12px 13px;background:var(--panel);
display:flex;flex-direction:column;gap:8px;transition:border-color .14s,transform .14s,box-shadow .14s}
.mcard:hover{border-color:var(--brand);transform:translateY(-1px);box-shadow:0 6px 18px rgba(37,99,235,.10)}
.mtop{display:flex;align-items:center;gap:9px;min-width:0}
.mname{font-weight:600;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mrank{width:22px;height:22px;border-radius:7px;display:flex;align-items:center;justify-content:center;
font-size:11.5px;font-weight:700;background:#f1f2f4;color:#5F5E5A;flex:0 0 auto}
.mrank.r1{background:#fde9c8;color:#8a5a00}.mrank.r2{background:#e8eaee;color:#4b5563}.mrank.r3{background:#f7e2d3;color:#8a4b20}
.mrow{display:flex;justify-content:space-between;align-items:baseline;font-variant-numeric:tabular-nums}
.mrow b{font-size:13px;font-weight:600}
.mbar{height:4px;border-radius:99px;background:#eef0f3;overflow:hidden}
.mbar>i{display:block;height:100%;background:var(--brand);opacity:.75}
/* 提示 */
.err{color:var(--bad);font-size:12px;min-height:16px;margin:4px 0}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;border:1px solid var(--line);color:var(--muted)}
.tag.ok{color:var(--ok);border-color:var(--ok)}
.tag.warn{color:var(--warn);border-color:var(--warn)}
/* 公告 */
#ann{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
.ann{background:var(--brand-weak);border:1px solid var(--brand);border-radius:10px;padding:10px 12px;font-size:12.5px}
.ann.warning{background:#fdf3e2;border-color:var(--warn)}
.ann.critical{background:#fdeceb;border-color:var(--bad)}
@media(max-width:860px){
  .shell{grid-template-columns:minmax(0,1fr);padding:14px;gap:12px}
  aside{position:static}
  .brand{padding:2px 6px 8px}
  #pn{flex-direction:row;flex-wrap:wrap;gap:4px}
  main,.pane,.card{min-width:0}      /* 否则表格的 min-content 会把整页撑宽 */
  #pn button{flex:1 1 calc(50% - 6px);text-align:center;padding:0 6px;font-size:12px;
             white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  /* 表格在窄屏横向滚动，宁可滑动也不让单元格挤成竖排 */
  .card{overflow-x:auto}
  .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
  .row label{flex:1 1 100%}
  .row>button{flex:1 1 auto}
  .mcard{padding:11px 12px}
  h2{font-size:15px}
}
@media(max-width:420px){
  .stats{grid-template-columns:minmax(0,1fr)}
  .stat .v{font-size:17px}
  .row>button{flex:1 1 100%}       /* 窄屏按钮整行，避免两个挤成一条 */
  #pn button{font-size:11.5px;padding:0 4px}
}
/* 统计卡标签：宁可省略也不要折成两行 */
.stat .k{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
</style>
</head>
<body>
<div class="auth-wrap" id="auth"><div class="box" id="authbox"></div></div>

<div class="shell" id="app" style="display:none">
  <aside>
    <div class="brand">__SITE__<span class="sub">用户面板</span></div>
    <nav id="pn">
      <button data-p="overview" class="on">概览</button>
      <button data-p="keys">我的密钥</button>
      <button data-p="models">模型广场</button>
      <button data-p="board">排行榜</button>
      <button data-p="logs">使用日志</button>
      <button data-p="audit">审计日志</button>
      <button data-p="secure">账户安全</button>
    </nav>
    <div class="side-foot"><button class="ghost" id="logout">退出登录</button></div>
  </aside>

  <main>
    <div id="ann"></div>

    <section class="pane on" id="p-overview">
      <div class="card" id="ov-head"></div>
      <div class="stats" id="ov-stats"></div>
      <div class="card">
        <h3 style="margin-top:0">额度</h3>
        <div class="row">
          <label>兑换码 <input id="code" placeholder="输入兑换码"></label>
          <button class="primary" id="redeem">兑换</button>
          <button class="ghost" id="rotate">重置主 Key</button>
        </div>
        <p class="sub" style="margin:8px 0 0">主 Key 是所有设备共用的入口；单独用途请在「我的密钥」里建。</p>
      </div>
      <div class="card" id="ov-clients"></div>
      <div class="card" id="ov-toip"></div>
    </section>

    <section class="pane" id="p-keys">
      <div class="card">
        <h2>我的密钥</h2>
        <p class="sub">每把可独立设额度、有效期、模型与来源 IP；用尽或过期即停用，不影响其他密钥。</p>
        <div id="keys"></div>
        <div id="keyform"></div>
      </div>
    </section>

    <section class="pane" id="p-models">
      <div class="card">
        <h2>模型广场</h2>
        <p class="sub">价格按你的分组倍率与当前时段计算；速度/成功率优先用你自己的调用，样本不足时退回全站并标注。</p>
        <div class="row">
          <label>搜索 <input id="mk-q" placeholder="模型名"></label>
          <label>排序 <select id="mk-sort">
            <option value="calls">按调用量</option><option value="speed">按速度</option>
            <option value="ok">按成功率</option><option value="price">按价格</option><option value="name">按名称</option>
          </select></label>
          <button id="mk-refresh">刷新</button>
          <span class="sub" id="mk-msg" style="flex:1 1 100%;margin:0"></span>
        </div>
        <div id="mk-box" style="margin-top:12px"><p class="sub">加载中…</p></div>
      </div>
    </section>

    <section class="pane" id="p-board">
      <div class="card">
        <h2>排行榜</h2>
        <p class="sub">全站调用聚合（只出计数与速率，不含任何用户/令牌信息）。</p>
        <div class="row">
          <label>范围 <select id="lb-period">
            <option value="today">今天</option><option value="week" selected>近 7 天</option><option value="month">近 30 天</option>
          </select></label>
          <span class="sub" id="lb-msg" style="margin:0"></span>
        </div>
        <div id="lb-box" style="margin-top:12px"><p class="sub">加载中…</p></div>
      </div>
    </section>

    <section class="pane" id="p-logs">
      <div class="card">
        <h2>使用日志</h2>
        <div class="row">
          <label>范围 <select id="lg-days"><option value="1">今天</option><option value="7" selected>近 7 天</option><option value="30">近 30 天</option></select></label>
          <label>模型 <input id="lg-model" placeholder="留空 = 全部"></label>
          <label>结果 <select id="lg-status"><option value="">全部</option><option value="ok">成功</option><option value="fail">失败</option></select></label>
          <button id="lg-go">筛选</button>
          <button class="ghost" id="lg-csv">导出 CSV</button>
        </div>
        <div id="lg-box" style="margin-top:12px"><p class="sub">（点「筛选」按条件加载）</p></div>
      </div>
    </section>

    <section class="pane" id="p-audit">
      <div class="card">
        <h2>审计日志</h2>
        <p class="sub">登录、密钥变更、改密、邮箱绑定等操作记录。只有你自己的。</p>
        <div id="au-box"><p class="sub">加载中…</p></div>
      </div>
    </section>

    <section class="pane" id="p-secure">
      <div class="card">
        <h2>账户安全</h2>
        <p class="sub">改密码会同时让其他设备上的登录失效。邮箱仅用于联系与人工找回（本站未接入邮件服务）。</p>
        <div class="row">
          <label>原密码 <input id="pw-old" type="password" autocomplete="current-password"></label>
          <label>新密码 <input id="pw-new" type="password" autocomplete="new-password" placeholder="至少 8 位"></label>
          <button class="primary" id="pw-go">修改密码</button>
          <button class="ghost" id="sess-revoke">退出其他设备</button>
        </div>
      </div>
      <div class="card">
        <h3 style="margin-top:0">邮箱</h3>
        <div class="row">
          <label>邮箱 <input id="em-addr" placeholder="用于联系与找回"></label>
          <label>当前密码 <input id="em-pw" type="password" autocomplete="current-password"></label>
          <button class="primary" id="em-go">保存</button>
        </div>
      </div>
      <div class="card">
        <h3 style="margin-top:0">背景</h3>
        <p class="sub" style="margin:0">站点背景由站长在管理面「公网面板外观」统一设置（颜色、图片、透明度）。
          个人侧不单独覆盖，避免每个页面风格漂移。</p>
      </div>
    </section>
  </main>
</div>

<script>
var base='',ME=null,CUR='overview';
var MARKET=[],LOADED={};

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function fmtNum(n){n=Number(n||0);if(n>=1e6)return (n/1e6).toFixed(1)+'M';
  if(n>=1e3)return (n/1e3).toFixed(1)+'k';return String(Math.round(n));}
async function api(path,body){
  var opt={credentials:'same-origin',headers:{}};
  if(body!==undefined){opt.method='POST';opt.headers['content-type']='application/json';
    opt.body=JSON.stringify(body);}
  var r=await fetch(base+path,opt);
  var t=await r.text(),j={};
  try{j=t?JSON.parse(t):{};}catch(e){j={ok:false,error:t.slice(0,120)};}
  if(!r.ok&&j.ok===undefined)j.ok=false;
  return j;
}

/* ---------- 侧栏切换（只有当前面板块跑加载，避免一次打满接口） ---------- */
function show(p){
  CUR=p;
  document.querySelectorAll('#pn button').forEach(function(b){b.classList.toggle('on',b.dataset.p===p);});
  document.querySelectorAll('.pane').forEach(function(s){s.classList.toggle('on',s.id==='p-'+p);});
  if(p==='keys')renderKeys();
  if(p==='models'&&!LOADED.models){loadMarket();}
  if(p==='board'&&!LOADED.board){loadBoard();}
  if(p==='audit'&&!LOADED.audit){loadAudit();}
  if(location.hash!=='#'+p)history.replaceState(null,'','#'+p);
}
document.getElementById('pn').addEventListener('click',function(e){
  var b=e.target.closest('button[data-p]'); if(b)show(b.dataset.p);
});

/* ---------- 登录 / 注册 ---------- */
function renderAuth(showRegister){
  document.getElementById('app').style.display='none';
  document.getElementById('auth').style.display='flex';
  var box=document.getElementById('authbox');
  var mailRow=showRegister
    ?'<label style="margin-top:8px">邮箱<input id="a-mail" type="email" autocomplete="email"></label>'
     +'<div class="row" style="margin-top:8px;align-items:flex-end">'
     +'<label style="flex:1 1 120px">验证码<input id="a-code" inputmode="numeric" maxlength="6"></label>'
     +'<button class="ghost" id="a-send" style="flex:0 0 auto">获取验证码</button></div>'
     +'<p class="sub" style="margin:2px 0 0">本站开启邮件服务时必须填验证码；未开启时留空即可。</p>'
    :'';
  box.innerHTML='<h2>'+(showRegister?'注册账号':'登录')+'</h2>'
    +'<p class="sub">'+(showRegister?'注册后自动获得一把 API Key':'用站点账号登录，管理你自己的密钥')+'</p>'
    +'<label>用户名<input id="a-user" autocomplete="username"></label>'
    +'<label style="margin-top:8px">密码<input id="a-pass" type="password" autocomplete="current-password"></label>'
    + mailRow
    +'<div class="err" id="a-err"></div>'
    +'<button class="primary" style="width:100%;margin-top:10px" id="a-go">'+(showRegister?'注册':'登录')+'</button>'
    +'<p class="sub" style="margin-top:12px">'
      +(showRegister?'已有账号？<button class="link" id="a-switch">去登录</button>'
                    :'没有账号？<button class="link" id="a-switch">注册</button> · <button class="link" id="a-forgot">忘记密码</button>')+'</p>';
  if(showRegister&&document.getElementById('a-send')) sendCodeButton('a-send','a-mail','register');
  if(!showRegister&&document.getElementById('a-forgot')){
    document.getElementById('a-forgot').onclick=function(){renderReset();};
  }
  document.getElementById('a-go').onclick=function(){showRegister?doRegister():doLogin();};
  document.getElementById('a-switch').onclick=function(){renderAuth(!showRegister);};
  document.getElementById('a-pass').onkeydown=function(e){if(e.key==='Enter')(showRegister?doRegister:doLogin)();};
}
async function doLogin(){
  var j=await api('/api/auth/login',{username:document.getElementById('a-user').value.trim(),
    password:document.getElementById('a-pass').value});
  if(!j.ok){document.getElementById('a-err').textContent=j.error||'登录失败';return;}
  boot();
}
async function doRegister(){
  var body={username:document.getElementById('a-user').value.trim(),
    password:document.getElementById('a-pass').value};
  if(document.getElementById('a-mail')) body.email=document.getElementById('a-mail').value.trim();
  if(document.getElementById('a-code')) body.code=document.getElementById('a-code').value.trim();
  var j=await api('/api/auth/register',body);
  if(!j.ok){document.getElementById('a-err').textContent=j.error||'注册失败';return;}
  boot();
}

/* 验证码按钮：60 秒倒计时，避免连点把邮件发爆 */
function sendCodeButton(btnId,mailId,purpose){
  var btn=document.getElementById(btnId);
  if(!btn) return;
  btn.onclick=async function(){
    var email=(document.getElementById(mailId).value||'').trim();
    if(!email){document.getElementById('a-err').textContent='请先填邮箱';return;}
    btn.disabled=true;
    var j=await api('/api/auth/email/code',{email:email,purpose:purpose});
    if(!j.ok){
      document.getElementById('a-err').textContent=j.error||'发送失败';
      btn.disabled=false;return;
    }
    document.getElementById('a-err').textContent='验证码已发送，请查收（含垃圾箱）';
    var left=60;btn.textContent=left+'s';
    var timer=setInterval(function(){
      left--;btn.textContent=left+'s';
      if(left<=0){clearInterval(timer);btn.disabled=false;btn.textContent='获取验证码';}
    },1000);
  };
}

/* 忘记密码：邮箱 + 验证码 + 新密码 */
function renderReset(){
  document.getElementById('app').style.display='none';
  document.getElementById('auth').style.display='flex';
  document.getElementById('authbox').innerHTML='<h2>重置密码</h2>'
    +'<p class="sub">用注册邮箱收验证码，验证通过后设置新密码。</p>'
    +'<label>邮箱<input id="r-mail" type="email" autocomplete="email"></label>'
    +'<div class="row" style="margin-top:8px;align-items:flex-end">'
    +'<label style="flex:1 1 120px">验证码<input id="r-code" inputmode="numeric" maxlength="6"></label>'
    +'<button class="ghost" id="r-send" style="flex:0 0 auto">获取验证码</button></div>'
    +'<label style="margin-top:8px">新密码<input id="r-pass" type="password" placeholder="至少 8 位"></label>'
    +'<div class="err" id="a-err" style="margin-top:8px"></div>'
    +'<button class="primary" style="width:100%;margin-top:10px" id="r-go">重置并登录</button>'
    +'<p class="sub" style="margin-top:12px"><button class="link" id="r-back">返回登录</button></p>';
  sendCodeButton('r-send','r-mail','reset');
  document.getElementById('r-back').onclick=function(){renderAuth(false);};
  document.getElementById('r-go').onclick=async function(){
    var j=await api('/api/auth/reset',{
      email:document.getElementById('r-mail').value.trim(),
      code:document.getElementById('r-code').value.trim(),
      new_password:document.getElementById('r-pass').value});
    if(!j.ok){document.getElementById('a-err').textContent=j.error||'重置失败';return;}
    document.getElementById('a-err').textContent='';
    alert('密码已重置，请用新密码登录');
    renderAuth(false);
  };
}
document.getElementById('logout').onclick=async function(){
  await api('/api/auth/logout',{});renderAuth(false);
};

/* ---------- 概览 ---------- */
function usageBlock(u){
  var st=(u&&u.usage)||{};
  var bal=(u&&u.unlimited)?'不限':fmtNum((u&&u.quota)||0);
  return '<div class="stats">'
    +'<div class="stat"><div class="k">余额</div><div class="v">'+bal+'</div></div>'
    +'<div class="stat"><div class="k">请求 / 7 天</div><div class="v">'+fmtNum(st.requests)+'</div></div>'
    +'<div class="stat"><div class="k">tokens 进 / 出</div><div class="v" style="font-size:15px">'+fmtNum(st.tokens_in)+' / '+fmtNum(st.tokens_out)+'</div></div>'
    +'<div class="stat"><div class="k">缓存命中</div><div class="v">'+fmtNum(st.cache_read)+'<span class="sub" style="margin-left:4px">'+(st.cache_hit_rate||0)+'%</span></div></div>'
    +'</div>';
}
function renderOverview(u){
  document.getElementById('ov-head').innerHTML='<h2>'+esc(u.username)+'</h2>'
    +'<p class="sub">分组 '+esc(u.group||'default')+' · 角色 '+esc(u.role||'user')
    +(u.email?(' · '+esc(u.email)):'')+'</p>';
  document.getElementById('ov-stats').innerHTML=usageBlock(u);
  document.getElementById('ov-clients').innerHTML='<h3 style="margin-top:0">我的客户端（近 7 天）</h3>'
    +'<div id="cl-box"><p class="sub">加载中…</p></div>'
    +'<div id="rc-box"></div>';
  document.getElementById('ov-toip').innerHTML='<h3 style="margin-top:0">我的通行证（TOIP）</h3>'
    +'<p class="sub">给插件接入用，用量记在你自己名下。</p>'
    +'<button class="ghost" id="tk-go">生成通行证</button><div id="tk"></div>';
  document.getElementById('tk-go').onclick=issueMyTicket;
  loadClients();
  var rc=(u.recent||[]);
  if(rc.length){
    var rows=rc.slice(0,8).map(function(e){
      var d=new Date((e.ts||0)*1000);
      return '<tr><td class="mono nowrap">'+d.toLocaleString()+'</td><td>'+esc(e.model||'-')+'</td>'
        +'<td class="nowrap">'+(e.ok?'<span class="tag ok">成功</span>':'<span class="tag warn">失败</span>')+'</td>'
        +'<td class="mono">'+fmtNum(e.tokens_in)+' → '+fmtNum(e.tokens_out)+'</td><td class="mono">'+(e.cost||0)+'</td></tr>';
    }).join('');
    document.getElementById('rc-box').innerHTML='<h3>最近消费</h3>'
      +'<table><thead><tr><th>时间</th><th>模型</th><th>结果</th><th>tokens</th><th>扣点</th></tr></thead><tbody>'+rows+'</tbody></table>';
  }
}
async function loadClients(){
  var box=document.getElementById('cl-box'); if(!box)return;
  var d=await api('/api/user/usage?days=7');
  if(!d||!d.by_client){box.innerHTML='<p class="sub">（暂无数据）</p>';return;}
  var rows=(d.by_client||[]).map(function(x){
    return '<tr><td>'+esc(x.client)+'</td><td class="mono">'+x.requests+'</td>'
      +'<td class="mono">'+fmtNum(x.tokens_in)+' / '+fmtNum(x.tokens_out)+'</td><td class="mono">'+(x.cost||0)+'</td></tr>';
  }).join('');
  box.innerHTML=rows?'<table><thead><tr><th>客户端</th><th>请求</th><th>tokens 进/出</th><th>扣点</th></tr></thead><tbody>'+rows+'</tbody></table>'
    :'<p class="sub">（还没有调用记录）</p>';
}
async function issueMyTicket(){
  var box=document.getElementById('tk');
  box.innerHTML='<p class="sub">签发中…</p>';
  var j=await api('/api/user/toip',{name:'我的客户端'});
  if(!j.ok){box.innerHTML='<p class="err">'+esc(j.error||'签发失败')+'</p>';return;}
  box.innerHTML='<p class="sub">明文只显示这一次</p><div class="mono" style="word-break:break-all">'+esc(j.ticket)+'</div>';
}

/* ---------- 密钥 ---------- */
function keyRow(k){
  var quota=k.quota_tokens>0?(fmtNum(k.used)+' / '+fmtNum(k.quota_tokens)):'不限';
  var exp=k.expires_at?new Date(k.expires_at*1000).toLocaleDateString():'永久';
  return '<tr><td>'+esc(k.name)+'</td><td class="mono nowrap">'+esc(k.hint||'—')+'</td>'
    +'<td class="mono nowrap">'+quota+'</td><td class="nowrap">'+exp+'</td>'
    +'<td class="nowrap">'+(k.enabled?'<span class="tag ok">启用</span>':'<span class="tag">停用</span>')+'</td>'
    +'<td class="nowrap">'
    +'<button class="ghost" data-id="'+k.token_id+'" data-next="'+(!k.enabled)+'" onclick="toggleKey(this)">'+(k.enabled?'停用':'启用')+'</button> '
    +'<button class="ghost" data-id="'+k.token_id+'" data-name="'+esc(k.name)+'" onclick="delKey(this)">删除</button></td></tr>';
}
function renderKeys(){
  var keys=(ME&&ME.keys)||[];
  var box=document.getElementById('keys');
  box.innerHTML=keys.length
    ?'<table><thead><tr><th>名称</th><th>密钥</th><th>额度</th><th>有效期</th><th>状态</th><th></th></tr></thead><tbody>'
      +keys.map(keyRow).join('')+'</tbody></table>'
    :'<p class="sub">（还没有密钥）</p>';
  var limit=(ME&&ME.key_limit)||20;
  document.getElementById('keyform').innerHTML=keys.length<limit
    ?'<h3>新建密钥</h3><div class="row">'
      +'<label>名称 <input id="k-name" placeholder="手机 / 平板 / 某工具"></label>'
      +'<label>额度(tokens) <input id="k-quota" type="number" min="0" placeholder="0=不限"></label>'
      +'<label>有效期(天) <input id="k-days" type="number" min="0" placeholder="0=永久"></label>'
      +'<label>模型限制 <input id="k-models" placeholder="逗号分隔；留空=不限"></label>'
      +'<label>IP 白名单 <input id="k-ip" placeholder="一行一个；留空=不限"></label>'
      +'<label style="flex:0 0 auto"><span>&nbsp;</span><button class="primary" onclick="createKey()">创建</button></label>'
      +'</div>'
    :'<p class="sub">已达上限 '+limit+' 把，删掉不用的再建。</p>';
}
async function createKey(){
  var body={name:(document.getElementById('k-name').value||'').trim(),
    quota_tokens:parseInt(document.getElementById('k-quota').value,10)||0,
    days:parseInt(document.getElementById('k-days').value,10)||0,
    models_text:document.getElementById('k-models').value,
    ip_text:document.getElementById('k-ip').value,allow_self_query:true};
  if(!body.name){alert('请填名称');return;}
  var j=await api('/api/user/tokens',body);
  if(!j.ok){alert(j.error||'创建失败');return;}
  ME.keys=j.tokens;renderKeys();
  alert('已创建，明文只显示这一次（请立即复制）：'+String.fromCharCode(10,10)+j.token);
}
async function toggleKey(el){
  var j=await api('/api/user/tokens/update',{token_id:el.dataset.id,enabled:el.dataset.next==='true'});
  if(!j.ok){alert(j.error||'操作失败');return;}
  ME.keys=j.tokens;renderKeys();
}
async function delKey(el){
  if(!confirm('删除「'+(el.dataset.name||'这把密钥')+'」？立即失效，无法恢复。'))return;
  var j=await api('/api/user/tokens/delete',{token_id:el.dataset.id});
  if(!j.ok){alert(j.error||'删除失败');return;}
  ME.keys=j.tokens;renderKeys();
}

/* ---------- 模型广场 ---------- */
async function loadMarket(){
  var box=document.getElementById('mk-box');
  var j=await api('/api/user/models?days=7');
  if(!j.ok){box.innerHTML='<p class="err">'+esc(j.error||'加载失败')+'</p>';return;}
  MARKET=j.models||[];LOADED.models=true;
  document.getElementById('mk-msg').textContent=MARKET.length+' 个模型 · 分组 '+j.group+' ×'+j.group_multiplier+(j.peak_now?'（峰时）':'');
  renderMarket();
}
function renderMarket(){
  var box=document.getElementById('mk-box');
  if(!MARKET.length){box.innerHTML='<p class="sub">（没有可用模型）</p>';return;}
  var q=(document.getElementById('mk-q').value||'').trim().toLowerCase();
  var key=document.getElementById('mk-sort').value;
  var list=MARKET.filter(function(m){return !q||m.model.toLowerCase().indexOf(q)>=0;});
  list.sort(function(a,b){
    if(key==='speed')return b.speed-a.speed;
    if(key==='ok')return b.ok_rate-a.ok_rate;
    if(key==='price')return a.price_in_per_1m-b.price_in_per_1m;
    if(key==='name')return a.model.localeCompare(b.model);
    return b.calls-a.calls;});
  box.innerHTML='<div class="mgrid">'+list.map(function(m){
    var dot=m.warm?'#2e9e6b':'#d79a2b';
    var src={mine:'我的实测',site:'全站数据',none:'暂无数据'}[m.scope]||'';
    return '<div class="mcard">'
      +'<div class="mtop"><span style="width:9px;height:9px;border-radius:50%;background:'+dot+';flex:0 0 auto"></span>'
      +'<span class="mname" title="'+esc(m.model)+'">'+esc(m.model)+'</span></div>'
      +'<span class="sub">'+esc(src)+(m.warm?'':' · 冷却中')+'</span>'
      +'<div class="mrow"><span class="sub">入 / 出（元/百万）</span><b>'+m.price_in_per_1m.toFixed(2)+' / '+m.price_out_per_1m.toFixed(2)+'</b></div>'
      +'<div class="mrow"><span class="sub">速度</span><b>'+(m.speed?m.speed.toFixed(1)+' tok/s':'—')+'</b></div>'
      +'<div class="mrow"><span class="sub">成功率</span><b>'+(m.calls?m.ok_rate.toFixed(1)+'%':'—')+'</b></div>'
      +'<div class="mrow"><span class="sub">调用</span><b>'+(m.calls||0)+'</b></div>'
      +'</div>';
  }).join('')+'</div>';
}

/* ---------- 排行榜 ---------- */
async function loadBoard(){
  var box=document.getElementById('lb-box');
  var period=document.getElementById('lb-period').value;
  var j=await api('/api/user/leaderboard?period='+encodeURIComponent(period));
  if(!j.ok){box.innerHTML='<p class="err">'+esc(j.error||'加载失败')+'</p>';return;}
  LOADED.board=true;
  document.getElementById('lb-msg').textContent='共 '+j.total_calls+' 次调用';
  if(!j.models.length){box.innerHTML='<p class="sub">（这个范围内还没有调用）</p>';return;}
  var top=j.models[0].calls||1;
  box.innerHTML='<div class="mgrid">'+j.models.map(function(m,i){
    var cls=i===0?'r1':(i===1?'r2':(i===2?'r3':''));
    return '<div class="mcard">'
      +'<div class="mtop"><span class="mrank '+cls+'">'+(i+1)+'</span>'
      +'<span class="mname" title="'+esc(m.model)+'">'+esc(m.model)+'</span></div>'
      +'<span class="sub">'+esc(m.vendor)+'</span>'
      +'<div class="mbar"><i style="width:'+Math.max(6,Math.round(m.calls/top*100))+'%"></i></div>'
      +'<div class="mrow"><span class="sub">调用</span><b>'+m.calls+'</b></div>'
      +'<div class="mrow"><span class="sub">tokens</span><b>'+fmtNum(m.tokens)+'</b></div>'
      +'<div class="mrow"><span class="sub">成功率 / 速度</span><b>'+m.ok_rate.toFixed(1)+'% · '+(m.speed?m.speed.toFixed(1):'—')+'</b></div>'
      +'</div>';
  }).join('')+'</div>';
}

/* ---------- 日志 ---------- */
function logQuery(){
  var d=document.getElementById('lg-days').value;
  var m=(document.getElementById('lg-model').value||'').trim();
  var s=document.getElementById('lg-status').value;
  var q='days='+encodeURIComponent(d);
  if(m)q+='&model='+encodeURIComponent(m);
  if(s)q+='&status='+encodeURIComponent(s);
  return q;
}
async function loadLogs(){
  var box=document.getElementById('lg-box');
  box.innerHTML='<p class="sub">加载中…</p>';
  var j=await api('/api/user/logs?'+logQuery());
  if(!j.ok){box.innerHTML='<p class="err">'+esc(j.error||'加载失败')+'</p>';return;}
  if(!j.entries||!j.entries.length){box.innerHTML='<p class="sub">（这个范围内没有调用）</p>';return;}
  var rows=j.entries.slice().reverse().map(function(e){
    var d=new Date((e.ts||0)*1000);
    return '<tr><td class="mono nowrap">'+d.toLocaleString()+'</td><td>'+esc(e.model||'-')+'</td>'
      +'<td class="nowrap">'+(e.ok?'<span class="tag ok">成功</span>':'<span class="tag warn">失败</span>')+'</td>'
      +'<td class="mono nowrap">'+fmtNum(e.tokens_in)+' → '+fmtNum(e.tokens_out)+'</td>'
      +'<td class="mono nowrap">'+(e.latency_ms||0)+'ms</td><td class="mono">'+(e.cost||0)+'</td></tr>';
  }).join('');
  box.innerHTML='<p class="sub">共 '+j.total+' 条'+(j.total>j.entries.length?'（显示最近 '+j.entries.length+' 条）':'')+'</p>'
    +'<table><thead><tr><th>时间</th><th>模型</th><th>结果</th><th>tokens</th><th>耗时</th><th>扣点</th></tr></thead><tbody>'+rows+'</tbody></table>';
}
function exportLogs(){location.href='/api/user/logs?'+logQuery()+'&format=csv';}

/* ---------- 审计 ---------- */
async function loadAudit(){
  var box=document.getElementById('au-box');
  var j=await api('/api/user/audit?limit=100');
  if(!j.ok){box.innerHTML='<p class="err">'+esc(j.error||'加载失败')+'</p>';return;}
  LOADED.audit=true;
  if(!j.events||!j.events.length){box.innerHTML='<p class="sub">（还没有记录）</p>';return;}
  var rows=j.events.map(function(e){
    var d=new Date((e.ts||0)*1000);
    return '<tr><td class="mono nowrap">'+d.toLocaleString()+'</td>'
      +'<td class="mono">'+esc(e.event)+'</td><td class="mono">'+esc(e.ip||'-')+'</td></tr>';
  }).join('');
  box.innerHTML='<table><thead><tr><th>时间</th><th>操作</th><th>来源 IP</th></tr></thead><tbody>'+rows+'</tbody></table>';
}

/* ---------- 安全 ---------- */
async function changePassword(){
  var o=document.getElementById('pw-old').value,n=document.getElementById('pw-new').value;
  if(n.length<8){alert('新密码至少 8 位');return;}
  var j=await api('/api/user/password',{old_password:o,new_password:n});
  if(!j.ok){alert(j.error||'修改失败');return;}
  alert('密码已修改，其他设备的登录已失效（'+(j.revoked_sessions||0)+' 个）');
  document.getElementById('pw-old').value='';document.getElementById('pw-new').value='';
}
async function revokeSessions(){
  if(!confirm('让其他设备上的登录全部失效？'))return;
  var j=await api('/api/user/sessions/revoke',{});
  alert(j.ok?('已退出 '+(j.revoked_sessions||0)+' 个会话'):(j.error||'操作失败'));
}
async function bindEmail(){
  var addr=(document.getElementById('em-addr').value||'').trim();
  var j=await api('/api/user/email',{email:addr,password:document.getElementById('em-pw').value});
  if(!j.ok){alert(j.error||'保存失败');return;}
  ME.email=addr;document.getElementById('em-pw').value='';
  alert(addr?('已绑定 '+addr):'已解绑邮箱');
}

/* ---------- 兑换 / 主 Key ---------- */
async function doRedeem(){
  var j=await api('/api/user/redeem',{code:(document.getElementById('code').value||'').trim()});
  if(!j.ok){alert(j.error||'兑换失败');return;}
  document.getElementById('code').value='';
  alert('兑换成功，+'+j.credits+' 点');
  boot();
}
async function doRotate(){
  if(!confirm('重置主 Key？旧 Key 立即失效。'))return;
  var j=await api('/api/user/rotate-key',{});
  if(!j.ok){alert(j.error||'操作失败');return;}
  alert('新主 Key（只显示这一次）：'+String.fromCharCode(10,10)+j.token);
}

/* ---------- 公告 ---------- */
async function loadAnnouncements(){
  try{
    var r=await fetch('/public/announcements');var a=await r.json();
    var box=document.getElementById('ann');if(!box)return;
    var items=(a&&a.items)||[];
    box.innerHTML=items.slice(0,3).map(function(x){
      return '<div class="ann '+(x.level||'info')+'">'+(x.pinned?'<b>置顶</b> · ':'')+esc(x.text)+'</div>';
    }).join('');
  }catch(e){}
}

/* ---------- 启动 ---------- */
async function boot(){
  var j=await api('/api/user/me');
  if(!j||j.ok===false&&j.error){renderAuth(false);return;}
  if(!j.username&&!j.ok){renderAuth(false);return;}
  ME=j;
  document.getElementById('auth').style.display='none';
  document.getElementById('app').style.display='grid';
  renderOverview(j);
  document.getElementById('em-addr').value=j.email||'';
  var h=(location.hash||'').replace('#','');
  show(['overview','keys','models','board','logs','audit','secure'].indexOf(h)>=0?h:'overview');
  loadAnnouncements();
}
document.getElementById('redeem').onclick=doRedeem;
document.getElementById('rotate').onclick=doRotate;
document.getElementById('mk-refresh').onclick=loadMarket;
document.getElementById('mk-q').oninput=renderMarket;
document.getElementById('mk-sort').onchange=renderMarket;
document.getElementById('lb-period').onchange=loadBoard;
document.getElementById('lg-go').onclick=loadLogs;
document.getElementById('lg-csv').onclick=exportLogs;
document.getElementById('pw-go').onclick=changePassword;
document.getElementById('sess-revoke').onclick=revokeSessions;
document.getElementById('em-go').onclick=bindEmail;
boot();
</script>
</body></html>
"""


# ---------------------------------------------------------------- HTTP 层


def _token_hint(secret: str) -> str:
    """尾 4 位提示，与 admin.key_hint 同语义。service 不反向依赖 admin 模块。"""
    return f"…{secret[-4:]}" if secret and len(secret) >= 4 else "…"


# 只有在**可信代理**后面才认 X-Forwarded-For / X-Real-IP。
# 公网直连的请求由客户端自带这两个头，认了就等于让任何人自选 IP——
# 既能绕过 IP 白名单，也能把限流嫁祸给别人。默认信任回环与私网（本机反代、
# cloudflared、frp、docker 网桥都在其中），可用 RELAYHUB_TRUSTED_PROXIES 覆盖。
_DEFAULT_TRUSTED = (
    "127.0.0.0/8", "::1/128",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "fc00::/7", "fe80::/10",
)


def trusted_networks() -> list:
    import ipaddress
    import os

    raw = os.environ.get("RELAYHUB_TRUSTED_PROXIES", "").strip()
    items = [x.strip() for x in raw.replace(",", " ").split() if x.strip()] if raw else list(_DEFAULT_TRUSTED)
    nets = []
    for item in items:
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return nets


def resolve_client_ip(peer: str, forwarded: str = "", real: str = "", networks: list | None = None) -> str:
    """决定「这个请求到底来自哪」。

    * 对端是可信代理 → 取 XFF 第一个（最初客户端），其次 X-Real-IP；
    * 对端不可信（公网直连）→ 一律用 TCP 对端地址，忽略任何转发头。
    """
    import ipaddress

    peer_ip = normalize_client_ip(peer)
    nets = trusted_networks() if networks is None else networks
    trusted = False
    try:
        addr = ipaddress.ip_address(peer_ip)
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped
        trusted = any(addr in net for net in nets)
    except ValueError:
        trusted = False
    if not trusted:
        return peer_ip

    first = str(forwarded or "").split(",")[0].strip()
    if first:
        return normalize_client_ip(first)
    if str(real or "").strip():
        return normalize_client_ip(real)
    return peer_ip


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # 结构不变量计数器：请求结束时请求体还没读完的次数。
    # 正常情况下恒为 0（拒绝路径也会先 drain）；>0 说明关连接时会变 RST，
    # 客户端可能读不到那个 401/403。测试用它做确定性判定，不靠概率复现。
    unread_body_closes = 0
    # Server 响应头里的版本号。早先写死 "relayhub/0.2"——排查时永远不知道
    # 对面跑的是哪一版，只好去问部署的人。改成跟随包版本单一来源
    # （与 /health、agent card 同一份），别在这里再抄一个数字。
    server_version = f"relayhub/{relayhub_version}"

    def version_string(self) -> str:
        """Server 头只报自己的名字和版本，**不带 Python 版本**。

        `BaseHTTPRequestHandler.version_string()` 默认拼上 `sys_version`
        （形如 "Python/3.11.9"），等于把解释器版本号广播给每一个客户端——
        扫端口的人拿它直接查该版本有哪些 CVE 可用。这是免费信息，别送。
        """
        return self.server_version

    @property
    def router(self) -> Any:
        return self.server.router  # type: ignore[attr-defined]

    @property
    def api_key(self) -> str | None:
        return self.server.api_key  # type: ignore[attr-defined]

    @property
    def tokens(self) -> TokenStore | None:
        return self.server.tokens  # type: ignore[attr-defined]

    @property
    def rate_limiter(self) -> RateLimiter:
        return self.server.rate_limiter  # type: ignore[attr-defined]

    @property
    def ip_limiter(self) -> RateLimiter:
        return self.server.ip_limiter  # type: ignore[attr-defined]

    @property
    def ip_rpm(self) -> int:
        return self.server.ip_rpm  # type: ignore[attr-defined]

    @property
    def conc_gate(self) -> ConcurrencyGate | None:
        return getattr(self.server, "conc_gate", None)  # type: ignore[attr-defined]

    @property
    def test_router(self) -> TestRouter:
        return self.server.test_router  # type: ignore[attr-defined]

    @property
    def users(self) -> UserStore | None:
        return getattr(self.server, "users", None)  # type: ignore[attr-defined]

    @property
    def redeem(self) -> RedeemStore | None:
        return getattr(self.server, "redeem", None)  # type: ignore[attr-defined]

    @property
    def loop_guard(self) -> LoopGuard:
        return self.server.loop_guard  # type: ignore[attr-defined]

    @property
    def response_cache(self) -> ResponseCache:
        return self.server.response_cache  # type: ignore[attr-defined]

    # -- 会话（用户面板） --------------------------------------------------

    SESSION_COOKIE = "rh_session"
    SESSION_TTL = 86400.0

    def _set_cookie_header(self, value: str, max_age: int) -> None:
        self.send_header(
            "Set-Cookie",
            f"{self.SESSION_COOKIE}={value}; Path=/; HttpOnly; Max-Age={max_age}; SameSite=Lax",
        )

    def _issue_session(self, user_id: str) -> str:
        sid = uuid.uuid4().hex + secrets.token_hex(16)
        with self.server.session_lock:  # type: ignore[attr-defined]
            self.server.sessions[sid] = (user_id, time.time() + self.SESSION_TTL)  # type: ignore[attr-defined]
        return sid

    def _drop_session(self) -> None:
        sid = self._session_id()
        if not sid:
            return
        with self.server.session_lock:  # type: ignore[attr-defined]
            self.server.sessions.pop(sid, None)  # type: ignore[attr-defined]

    def _session_id(self) -> str:
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.SESSION_COOKIE:
                return value.strip()
        return ""

    def _session_user(self) -> Any:
        """会话 → User。无效/过期返回 None。"""
        sid = self._session_id()
        if not sid or self.users is None:
            return None
        with self.server.session_lock:  # type: ignore[attr-defined]
            entry = self.server.sessions.get(sid)  # type: ignore[attr-defined]
        if entry is None or time.time() > entry[1]:
            return None
        return self.users.pool.get(entry[0])

    def _send_json(self, status: int, payload: dict[str, Any], cookie: str | None = None,
                   cookie_max_age: int = 0) -> None:
        # 响应前先把请求体读干净：客户端多半带 Connection: close，
        # 缓冲区里残留未读数据会让关闭变成 RST，401 会被客户端读丢。
        self._drain_request_body()
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._echo_request_id()
        pending = getattr(self, "_pending_retry_after", None)
        if pending:
            self.send_header("Retry-After", str(int(pending)))
            self._pending_retry_after = None
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cookie:
            self._set_cookie_header(cookie, cookie_max_age)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _echo_request_id(self) -> None:
        """把本请求的 id 回给客户端（响应头 X-Request-ID）。

        为什么非做不可：请求 id 此前只往上游和日志流。客户端拿到一个 500 时
        没有任何凭据可引用——客服问「哪个请求」，用户只能答「刚才那次」。
        有了这个头，客户端可以直接把 id 贴给我们，日志里能一刀定位到那一行。

        客户端自己带了 X-Request-ID 就沿用它的（链路两端对齐），没带才用我们生成的。
        """
        request_id = getattr(self, "_current_request_id", "")
        if request_id:
            self.send_header("X-Request-ID", request_id)

    def _issue_user_api_key(self, user: Any) -> str:
        """给用户发/换 API Key：绑定 user_id 的 normal 令牌，旧的全删。"""
        token_store = self.tokens
        if token_store is None:
            raise UserError("网关未启用下游令牌，无法发 API Key")
        secret = generate_token()
        record = DownstreamToken(
            token_id=str(uuid.uuid4()),
            name=f"user-{user.username}",
            token=secret,
            scope="normal",
            user_id=user.user_id,
            note=f"panel:{user.username}",
        )

        def change(pool: Any) -> None:
            for old in [t for t in pool.tokens if t.user_id == user.user_id]:
                pool.remove(old.token_id)
            pool.add(record)

        token_store.mutate(change)
        return secret

    def _panel_models(self, user: Any) -> list[str]:
        """名下启用令牌的渠道组并集对应的模型——「每用户不同模型列表」的落地。"""
        groups = {
            t.channel_group
            for t in (self.tokens.pool.tokens if self.tokens else [])
            if t.user_id == user.user_id and t.enabled and t.channel_group
        }
        try:
            if groups:
                merged: list[str] = []
                for g in sorted(groups):
                    for m in self.router.models(group=g):
                        if m not in merged:
                            merged.append(m)
                return merged
            return list(self.router.models())
        except TypeError:
            # 旧签名路由器（测试桩/第三方）：退回全量清单
            return list(self.router.models())

    def usage_for(self, identity: Any) -> dict[str, Any]:
        """调用方自己的余额与用量（按令牌归集）。

        只读**自己**的账：令牌名过滤请求日志，用户信息按令牌绑定的 user_id 取。
        master 令牌没有 user 记录时，余额字段给 0 且 `unlimited=false`，
        不假装有额度。
        """
        # master 令牌没有名字，但请求日志里就写作 "master"——用同一口径，
        # 否则 master 调 /v1/usage 永远是 0，看起来像"没有用量"。
        token_name = str(getattr(identity, "name", "") or "")
        if not token_name:
            # master 走的是 api_key，identity 可能是 None——请求日志里仍记作
            # "master"，这里必须用同一口径，否则 master 查用量永远是 0。
            token_name = "master"
        user = None
        user_id = str(getattr(identity, "user_id", "") or "")
        if user_id and self.users is not None:
            user = self.users.pool.get(user_id)

        entries: list[dict[str, Any]] = []
        if self.request_log and token_name:
            try:
                entries = [
                    e for e in reqlog.tail(Path(self.request_log), limit=500)
                    if e.get("token") == token_name
                ]
            except Exception:  # noqa: BLE001 - 报表读失败不该让 /v1/usage 500
                entries = []
        tokens_in = sum(int(e.get("tokens_in") or 0) for e in entries)
        cache_read = sum(int(e.get("cache_read") or 0) for e in entries)
        quota = int(getattr(user, "quota", 0) or 0) if user else 0
        return {
            "ok": True,
            "token": token_name,
            "username": str(getattr(user, "username", "") or token_name),
            "role": str(getattr(user, "role", "user")) if user else "user",
            "quota": quota,
            "used": int(getattr(user, "used", 0) or 0) if user else 0,
            "unlimited": quota < 0,
            "requests": len(entries),
            "tokens_in": tokens_in,
            "tokens_out": sum(int(e.get("tokens_out") or 0) for e in entries),
            "cache_read": cache_read,
            "cache_hit_rate": round(cache_read / tokens_in * 100, 1) if tokens_in else 0.0,
            "cost": sum(int(e.get("cost") or 0) for e in entries),
        }
    def _panel_recent(
        self, user: Any
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """本人最近消费明细 + 用量汇总。

        汇总里必须带 **cache_read**：用户最想知道的就是「我的缓存命中了多少、
        一共花了多少钱」。早先这里只给 tokens/cost，面板就永远显示不出命中率——
        而后端其实早就记下了这个数（reqlog 里有 cache_read），只是没往外送。

        明细取**最新** 15 条：`tail()` 返回的是按时间正序的窗口，取前 15 条
        其实取到了窗口里最老的那批，方向反了。
        """
        empty = {"requests": 0, "tokens_in": 0, "tokens_out": 0,
                 "cache_read": 0, "cost": 0, "cache_hit_rate": 0.0}
        names = {
            t.name
            for t in (self.tokens.pool.tokens if self.tokens else [])
            if t.user_id == user.user_id
        }
        if not names or not self.request_log:
            return [], empty
        try:
            entries = reqlog.tail(Path(self.request_log), limit=500)
        except Exception:
            return [], empty

        mine = [e for e in entries if e.get("token") in names]
        recent = [
            {
                "ts": e.get("ts"), "model": e.get("model"), "ok": e.get("ok"),
                "tokens_in": e.get("tokens_in"), "tokens_out": e.get("tokens_out"),
                "cache_read": e.get("cache_read") or 0,
                "cost": e.get("cost") or 0,
                "stream": bool(e.get("stream")),
            }
            for e in mine[-15:][::-1]          # 最新在前
        ]
        tokens_in = sum(int(e.get("tokens_in") or 0) for e in mine)
        cache_read = sum(int(e.get("cache_read") or 0) for e in mine)
        usage = {
            "requests": len(mine),
            "tokens_in": tokens_in,
            "tokens_out": sum(int(e.get("tokens_out") or 0) for e in mine),
            "cache_read": cache_read,
            "cost": sum(int(e.get("cost") or 0) for e in mine),
            # 命中率按输入 token 计：cache_read / tokens_in。tokens_in 为 0 时给 0，
            # 不返回 None——前端不必再做空值分支。
            "cache_hit_rate": round(cache_read / tokens_in * 100, 1) if tokens_in else 0.0,
        }
        return recent, usage

    def _handle_panel_api(self, path: str) -> None:
        """用户面板 API。/api/auth/* 无需会话；/api/user/* 需要会话。"""
        if self.users is None:
            self._send_json(503, {"ok": False, "error": "用户系统未启用"})
            return
        try:
            if path == "/api/auth/email/code":
                self._auth_email_code()
                return
            if path == "/api/auth/reset":
                self._auth_reset()
                return
            if path == "/api/auth/register":
                body = self._read_json() or {}
                cfg = mailer_module.load_config(paths.mail_path())
                email = mailer_module.clean_email(body.get("email"))
                if cfg.ready:
                    # 开了邮件服务就强制验证：否则「验证码」只是摆设
                    if not email:
                        self._send_json(400, {"ok": False, "error": "请填写邮箱并获取验证码"})
                        return
                    if not mailer_module.domain_allowed(email, cfg.allowed_domains):
                        self._send_json(400, {"ok": False,
                                              "error": "本站仅允许指定域名邮箱注册"})
                        return
                    ok, reason = mailer_module.CodeStore(paths.verify_codes_path()).verify(
                        email, "register", str(body.get("code") or "")
                    )
                    if not ok:
                        self._send_json(400, {"ok": False, "error": reason})
                        return
                user = self.users.register(
                    str(body.get("username") or ""), str(body.get("password") or "")
                )
                if email:
                    try:
                        self.users.mutate(lambda pool: pool.set_email(user.user_id, email))
                    except Exception:
                        pass
                audit_module.record(
                    "panel.register",
                    path=paths.audit_path(),
                    username=user.username,
                    ip=self.client_address[0],
                )
                sid = self._issue_session(user.user_id)
                api_key = self._issue_user_api_key(user)
                self._send_json(
                    200,
                    {"ok": True, "username": user.username, "token": api_key,
                     "quota": user.quota},
                    cookie=sid,
                    cookie_max_age=int(self.SESSION_TTL),
                )
                return
            if path == "/api/user/toip":
                self._user_toip()
                return
            if path == "/api/user/usage":
                self._user_usage()
                return
            if path == "/api/auth/login":
                body = self._read_json() or {}
                # 兼容两种字段名：面板历史上传 u/p，注册与 API 用 username/password
                account = str(body.get("username") or body.get("u") or "")
                secret = str(body.get("password") or body.get("p") or "")
                user = self.users.verify(account, secret)
                if user is None:
                    # 登录失败也留痕（含 IP）：撞库/爆破在这里能看出来
                    audit_module.record(
                        "panel.login_failed",
                        path=paths.audit_path(),
                        username=str(body.get("username") or body.get("u") or "")[:32],
                        ip=self.client_address[0],
                    )
                    self._send_json(401, {"ok": False, "error": "用户名或密码错误"})
                    return
                audit_module.record(
                    "panel.login",
                    path=paths.audit_path(),
                    username=user.username,
                    ip=self.client_address[0],
                )
                sid = self._issue_session(user.user_id)
                self._send_json(
                    200,
                    {"ok": True, "username": user.username},
                    cookie=sid,
                    cookie_max_age=int(self.SESSION_TTL),
                )
                return
            if path == "/api/auth/logout":
                self._drop_session()
                self._send_json(200, {"ok": True})
                return
            user = self._session_user()
            if user is None:
                self._send_json(401, {"ok": False, "error": "请先登录"})
                return
            required = {
                "/api/user/rotate-key": "panel.key",
                "/api/user/redeem": "panel.redeem",
            }.get(path, "panel.view")
            if not user_can(user, required):
                self._send_json(
                    403,
                    {"ok": False,
                     "error": "当前账号无权使用网页面板（接入方式为 API 直连）"
                     if str(getattr(user, "channel", "web")) == "api"
                     else "当前账号无权执行该操作"},
                )
                return
            if path == "/api/user/rotate-key":
                api_key = self._issue_user_api_key(user)
                self._send_json(200, {"ok": True, "token": api_key})
                return
            # ---- 自助密钥管理（创建 / 修改 / 删除都由用户自己做） ----
            # 必须放在下面 required-scope 的兜底之前：兜底默认按 panel.view，
            # 那条会让只读账号也能建密钥。
            if path == "/api/user/tokens":
                self._user_token_create(user)
                return
            if path == "/api/user/tokens/update":
                self._user_token_update(user)
                return
            if path == "/api/user/tokens/delete":
                self._user_token_delete(user)
                return
            # ---- 账户安全（对标 new-api 的「安全与访问」）----
            if path == "/api/user/password":
                self._user_password(user)
                return
            if path == "/api/user/sessions/revoke":
                self._user_sessions_revoke(user)
                return
            if path == "/api/user/email":
                self._user_email(user)
                return
            if path == "/api/user/redeem":
                body = self._read_json() or {}
                if self.redeem is None:
                    self._send_json(503, {"ok": False, "error": "兑换系统未启用"})
                    return
                credits = self.redeem.redeem(str(body.get("code") or ""), user.user_id)

                def _add_quota(pool: Any) -> None:
                    target = pool.get(user.user_id)
                    if target is None:
                        raise UserError("用户不存在")
                    if target.quota < 0:
                        target.used += credits  # 不限量管理员：兑换只记累计
                    else:
                        target.quota += credits

                self.users.mutate(_add_quota)
                fresh = self.users.pool.get(user.user_id)
                self._send_json(200, {"ok": True, "credits": credits, "quota": fresh.quota})
                return
            self._send_json(404, {"ok": False, "error": f"未知面板路径 {path}"})
        except UserError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})

    @property
    def pairing(self) -> Any:
        return self.server.pairing  # type: ignore[attr-defined]

    @property
    def request_log(self) -> Any:
        return self.server.request_log  # type: ignore[attr-defined]

    @property
    def policy(self) -> Any:
        """接入策略（PolicyStore）。旧构造路径没有该属性时等价于「无策略」。"""
        return getattr(self.server, "policy", None)


    def _log_request(
        self,
        *,
        started: float,
        dialect: str,
        model: str = "",
        identity: DownstreamToken | None = None,
        ok: bool,
        stream: bool = False,
        status: int = 200,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cache_read: int = 0,
        cache_creation: int = 0,
        cost: int = 0,
        upstream_cost: int = 0,
        channel: str = "",
        reason: str = "",
        tool_search: int = 0,
        tool_fetch: int = 0,
        tool_image: int = 0,
    ) -> None:
        """写一条请求明细。终端只有一个：本方法不该被同一请求调两次。"""
        if identity is not None:
            who = identity.name
        elif self.api_key or (self.tokens and self.tokens.pool.tokens):
            who = "master"
        else:
            who = "-"
        # 客户端身份：本请求解出的身份字段（无则全空，reqlog 就不写这些键）
        ident = getattr(self, "_ident", None) or {}
        ident_bad = ident.get("bad", "")
        latency_ms = (time.monotonic() - started) * 1000
        request_id = getattr(self, "_current_request_id", "")
        ip = self._client_ip()

        # 插件日志：**先于 request_log 的开关判断**——`--no-request-log` 是
        # 「别给这个网关留全局流水」的意思，不该顺带把插件自己的接入记录灭掉。
        # 两者服务的追问不同（见 pluginlogs 模块头部的三方分工表）。
        plugin = self._plugin_identity(identity)
        if plugin is not None:
            pluginlogs.record(
                self._plugin_log_home(),
                plugin["id"],
                plugin_version=plugin.get("version", ""),
                token=who,
                dialect=dialect,
                model=model,
                ok=ok,
                stream=stream,
                status=status,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                ip=ip,
                reason=reason,
            )

        log_path = self.request_log
        if log_path is None:
            return
        reqlog.record(
            log_path,
            token=who,
            dialect=dialect,
            model=model,
            channel=channel,
            ok=ok,
            stream=stream,
            status=status,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_read=cache_read,
            cache_creation=cache_creation,
            cost=cost,
            upstream_cost=upstream_cost,
            request_id=request_id,
            ip=ip,
            ident_user=ident.get("user", ""),
            ident_ver=ident.get("ver", ""),
            ident_dev=ident.get("dev", ""),
            ident_did=ident.get("did", ""),
            ident_src=("bad:" + ident_bad) if ident_bad else ident.get("src", ""),
            latency_ms=latency_ms,
            reason=reason,
            tool_search=tool_search,
            tool_fetch=tool_fetch,
            tool_image=tool_image,
        )

    # -- 插件身份与插件日志 ------------------------------------------------

    def _plugin_log_home(self) -> Any:
        """插件日志根目录。允许测试用 server.plugin_log_home 覆盖。"""
        override = getattr(self.server, "plugin_log_home", None)
        return override if override is not None else paths.relayhub_home()

    def _plugin_identity(
        self, identity: DownstreamToken | None = None
    ) -> dict[str, str] | None:
        """解出「这个请求属于哪个插件」。解不出返回 None（不写插件日志）。

        优先级：插件自报的头 → 令牌的 TOIP 绑定 → 无。

        为什么令牌绑定要排在头**之后**而不是之前：插件头是插件自己填的，
        能区分「同一台机器上的两个插件」；令牌绑定只能区分到设备。
        但头可以伪造，所以它只当分类标签用——**鉴权永远只看令牌**
        （`_authenticate`），伪造插件头最坏后果是把日志记到隔壁插件名下。

        校验必须走 `toip.sanitize_plugin_id` 同一套白名单：插件 id 会变成
        **目录名**，`../` 这类值能把日志写到数据根之外。
        """
        raw = (
            self.headers.get(toip_module.PLUGIN_ID_HEADER, "")
            or self.headers.get(toip_module.PLUGIN_ID_HEADER_GENERIC, "")
        ).strip()
        if not raw and identity is not None:
            raw = toip_module.plugin_of_note(identity.note)
        if not raw:
            return None
        try:
            plugin_id = toip_module.sanitize_plugin_id(raw)
        except toip_module.ToipError:
            # 非法插件 id 不拒请求（它不参与鉴权），只是不记插件日志。
            self.log_error("忽略非法的插件身份头：%r", raw[:80])
            return None
        return {
            "id": plugin_id,
            "version": self.headers.get(toip_module.PLUGIN_VERSION_HEADER, "").strip()[:32],
        }

    @property
    def toip(self) -> Any:
        """TOIP 接入服务。未配置站点身份时为 None（端点回 404）。"""
        return getattr(self.server, "toip", None)

    @property
    def event_delay(self) -> float:
        return self.server.event_delay  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # -- 基础设施 --------------------------------------------------------
    # _send_json 在会话区定义（带 Set-Cookie 支持），这里不重复定义。

    def _site_name(self) -> str:
        """当前站点名（用户面板/落地页标题用），随 site.json 改动即时生效。"""
        return site_module.load_site(paths.site_path()).name

    def _send_html(self, html: str) -> None:
        html = site_module.render(html, self._site_name())
        raw = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(
        self,
        status: int,
        message: str,
        kind: str = "invalid_request_error",
        dialect: str = "anthropic",
        code: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        """两种客户端对错误体的形状要求不一样，别互相糊。

        Anthropic：{"type":"error","error":{"type":...,"message":...}}
        OpenAI   ：{"error":{"message":...,"type":...,"code":...}}
        """
        self._pending_retry_after = retry_after
        rid = getattr(self, "_current_request_id", "")
        if dialect in ("openai", "responses"):
            self._send_json(
                status,
                {"error": {"message": message, "type": kind, "code": code, "request_id": rid}},
            )
        else:
            self._send_json(status, {"type": "error", "error": {"type": kind, "message": message}})

    def _supplied_credential(self) -> str:
        """接受 Anthropic 的 x-api-key，也接受 OpenAI 的 Authorization: Bearer。

        两个口（有的客户端发 Bearer、有的发 x-api-key）都指到同一个鉴权面，
        所以这里对头部不做协议假设。
        """
        supplied = self.headers.get("x-api-key", "")
        if not supplied:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:]
        return supplied.strip()

    # 站间直通：白名单正则（不做通用代理）。
    # Gemini 把模型名编码在路径里（/v1beta/models/{model}:generateContent），
    # 所以不能像其它三种那样映射成固定字符串——只能校验形状后原样转发。
    _PEER_FORWARD_RULES = (
        (re.compile(r"^/v1/chat/completions$"), "openai"),
        (re.compile(r"^/v1/messages$"), "anthropic"),
        (re.compile(r"^/v1/responses$"), "openai-responses"),
        (re.compile(r"^/v1beta/models/[A-Za-z0-9._-]{1,80}:(generateContent|streamGenerateContent)$"), "gemini"),
    )
    _PEER_FORWARD_MAX_BODY = 2 * 1024 * 1024

    def _peer_forward(self) -> None:
        """站间字节直通：把本机当成对方的上游，原样转发、原样回传。

        设计取舍（重要）：

        * **不做翻译**。进来的字节直接交给本站自己的公开接口（loopback），
          返回的字节也原样还给对方。省掉的是「入口站 → 出口站」这一跳的
          解析与重组；出口站到自己上游的翻译仍需按方言进行。
        * **复用本站规则**。走 loopback 调用公开接口，意味着令牌、额度、
          IP 白名单、RPM、模型限制全部照常生效——直通省的是 CPU，不是规则。
        * **路径白名单**。只认三种对话接口，不做通用代理；body 上限 2MB。
        """
        import http.client

        store = peer_module.PeerStore(paths.peer_path())
        if not store.list():
            self._error(404, f"未知路径 {self.path.split('?')[0]}", "not_found_error")
            return
        try:
            peer = peer_module.identify(store, self.headers)
        except peer_module.PeerError as exc:
            self._error(401, str(exc), "authentication_error")
            return

        # 下游接口要求什么形状，就按什么形状直通；入口站不做翻译。
        target = str(self.headers.get("X-Peer-Path") or "").strip()
        dialect = ""
        for pattern, name in self._PEER_FORWARD_RULES:
            if pattern.match(target):
                dialect = name
                break
        if not dialect:
            self._error(400, f"不允许直通的路径 {target!r}", "invalid_request_error")
            return
        if not self.api_key:
            # 没有主凭证就无法在 loopback 上通过本站鉴权；宁可明确拒绝，
            # 也不要在直通路径上开一个"免鉴权后门"
            self._error(503, "本站未配置主凭证（--api-key），不能作为直通出口", "api_error")
            return

        length = 0
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > self._PEER_FORWARD_MAX_BODY:
            self._error(413, "请求体过大", "invalid_request_error")
            return
        body = self.rfile.read(length) if length else b""

        port = self.server.server_address[1]  # type: ignore[attr-defined]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
        try:
            conn.request("POST", target, body=body, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": self.headers.get("Accept") or "application/json",
                "X-Peer-Forwarded-By": peer.peer_id,
            })
            upstream = conn.getresponse()
            payload = upstream.read()
            self.send_response(upstream.status)
            for header, value in upstream.getheaders():
                if header.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(header, value)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Peer-Forwarded", peer.peer_id)
            self.end_headers()
            self.wfile.write(payload)
            audit_module.record("peer.forward", path=paths.audit_path(),
                                peer=peer.peer_id, dialect=dialect, target=target,
                                status=upstream.status, bytes=len(payload),
                                ip=self._client_ip())
        except OSError as exc:
            self._error(502, f"出口站转发失败：{exc}", "api_error")
        finally:
            conn.close()

    def _handle_peer(self, path: str) -> None:
        """站间私有协议：info / balance / pricing（只读）。

        返回的数值一律标 advisory：那是上游自报的数字，上游随时可以改。
        这里**不做请求签名**——两站之间已有 TLS，签名只能证明「是谁在问」，
        而这件事一个 Bearer 令牌就够了。真要不可否认的凭证，等有第三方
        对账需求时再加，不要预支复杂度。
        """
        store = peer_module.PeerStore(paths.peer_path())
        if not store.list():
            # 没配任何 peer 时，与「路径不存在」完全一致：不给探测者任何信号
            self._error(404, f"未知路径 {self.path.split('?')[0]}", "not_found_error")
            return
        try:
            peer = peer_module.identify(store, self.headers)
        except peer_module.PeerError as exc:
            self._error(401, str(exc), "authentication_error")
            return

        action = path.rsplit("/", 1)[-1]
        base = {
            "ok": True,
            "advisory": True,
            "source": "peer",
            "peer_id": peer.peer_id,
            "warning": peer_module.ADVISORY_WARNING,
            "served_at": time.time(),
        }
        if action == "info":
            base.update({
                "station": {
                    "name": self._site_name(),
                    "version": relayhub_version,
                },
                "capabilities": list(peer_module.CAPABILITIES),
                "models": len(self.router.models()),
            })
            self._send_json(200, base)
            return
        if action == "balance":
            keys = list(getattr(getattr(self.router, "pool", None), "keys", []) or [])
            known = [k for k in keys if int(getattr(k, "credits", -1)) >= 0]
            base.update({
                "currency": "CNY",
                # -1 表示「不知道」，不要当 0 报出去：0 会被误读成"没钱了"
                "remaining": sum(int(getattr(k, "credits", 0)) for k in known) if known else -1,
                "unknown_keys": len(keys) - len(known),
                "keys": len(keys),
                "updated_at": max((float(getattr(k, "credits_updated_at", 0) or 0) for k in known), default=0.0),
            })
            self._send_json(200, base)
            return
        if action == "pricing":
            pool = self.users.pool if self.users else None
            mult = pool.time_multiplier() if pool else 1.0
            models = []
            if pool:
                for model in sorted(self.router.models()):
                    models.append({
                        "model": model,
                        "in_per_1m": round(pool.price_in_per_1k * 1000 * mult, 4),
                        "out_per_1m": round(pool.price_out_per_1k * 1000 * mult, 4),
                    })
            base.update({
                "currency": "CNY",
                "unit": "per_1m_tokens",
                "peak_now": mult > 1.0,
                "models": models,
            })
            self._send_json(200, base)
            return
        self._error(404, f"未知的站间接口 {action}", "not_found_error")

    def _client_ip(self) -> str:
        """请求来源 IP（用于单 IP 限流）。

        优先取反向代理头：经过 nginx/frp 等转发时，client_address 是代理的
        回环地址，按它限流会把所有人的额度算到代理头上。多级代理取
        X-Forwarded-For 的**第一个**（最初客户端），但注意该头可被伪造——
        直连场景（无代理头）用 TCP 对端地址，不可伪造。
        """
        return resolve_client_ip(
            self.client_address[0],
            self.headers.get("X-Forwarded-For", ""),
            self.headers.get("X-Real-IP", ""),
        )

    def _handle_pair(self) -> None:
        """POST /v1/pair：发放下游令牌 + onboarding 载荷。

        两种发放模式（--pair-mode）：
          * code：带一次性配对码（带外传递），窗口没开直接拒。
          * auto-lan：局域网免码——来源是回环/私有地址即发，同设备幂等复用。
            公网来源自动退回配对码模式（没有码就 403）。
        响应里的 base_url 取 Host 头——客户端用哪个地址连上来，就用哪个地址
        回填 spec，多网卡/自定义端口都不需要网关猜。
        """
        pairing = self.pairing
        if pairing is None:
            self._error(404, "本网关未启用配对服务", "not_found_error")
            return
        body = self._read_json()
        if body is None:
            return
        code = str(body.get("code") or "").strip()
        device_name = str(body.get("name") or "").strip()
        client_id = str(body.get("client") or "app").strip().lower()
        try:
            clients_module.get(client_id)
        except KeyError as exc:
            self._error(400, str(exc), "invalid_request_error")
            return

        from .pairing import PairingError

        try:
            if code:
                record = pairing.redeem(code, device_name, client_id)
            elif str(getattr(self.server, "pair_mode", "code")) == "auto-lan":
                record = pairing.auto_pair(
                    device_name, client_id, ip=self._client_ip()
                )
            else:
                self._error(
                    403, "本站要求配对码：请向管理员索取", "permission_error",
                )
                return
        except PairingError as exc:
            self._error(403, str(exc), "permission_error")
            return

        host = self.headers.get("Host") or f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        models = [
            {"model_id": model, "context_window": window or None}
            for model, window in sorted(self.router.models().items())
        ]
        payload = clients_module.onboarding(
            client_id,
            base_url=f"http://{host}",
            api_key=record.token,
            models=models,
        )
        self._send_json(200, payload)

    # -- TOIP 接入 ---------------------------------------------------------

    def _http_root(self) -> str:
        """本站对外的 HTTP 根地址（无尾斜杠），用于拼给插件的接入载荷。

        与 `_handle_pair` 同一口径取 Host 头：客户端用哪个地址连上来，
        就用哪个地址回填它的配置——多网卡/自定义端口/反代前缀都不需要
        网关猜，也不需要管理员维护一份「对外地址」配置（那是最容易过期的东西）。
        """
        host = self.headers.get("Host") or (
            f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        )
        return f"http://{host}"

    def _handle_toip_station(self) -> None:
        """GET /v1/toip/station：公开的站点 TOIP 能力声明。

        未启用 TOIP 时回 404 而不是 200 + enabled:false——「这个站没有这个
        功能」与「这个站有这个功能但现在关着」对客户端是同一种处置（换配对码），
        但 404 让探测方一眼看出该换路子，不必解析 body。
        """
        service = self.toip
        if service is None or not service.enabled():
            self._error(404, "本网关未启用 TOIP 接入", "not_found_error")
            return
        payload = service.station_public()
        # base_url 一律以本次请求看到的地址为准（station 文件里的 base_url
        # 只在管理员显式写死时才用，那种场景是跨网段/NAT 后无法从来源推断）。
        payload["base_url"] = payload.get("base_url") or self._http_root()
        payload["models"] = len(self.router.models())
        self._send_json(200, payload)

    def _handle_toip_join(self, path: str) -> None:
        """POST /v1/toip/join|enroll：动态口令 / 登记口令 → 会话令牌 + 接入载荷。

        成功响应就是「插件自助接入所需的一切」：会话令牌、要写进客户端配置的
        base URL、可选的模型清单、以及一个 `dsh` 块（客户端就填这个，别的字段
        是给人看的）。字段命名刻意与 `clients.onboarding` 对齐，让「配对」和
        「TOIP」两条接入路径在客户端侧可以用同一套解析。
        """
        service = self.toip
        if service is None or not service.enabled():
            self._error(404, "本网关未启用 TOIP 接入", "not_found_error")
            return
        body = self._read_json("openai")
        if body is None:
            return

        code = str(body.get("code") or "").strip()
        ticket = str(body.get("ticket") or "").strip()
        if path == "/v1/toip/enroll" and not ticket:
            self._error(400, "enroll 需要登记口令 ticket", "invalid_request_error")
            return
        plugin_id = str(body.get("plugin") or body.get("plugin_id") or "").strip()
        if not plugin_id:
            self._error(
                400,
                "缺少 plugin（插件 id，用于分账与日志目录；建议 dsh-relayhub-bridge）",
                "invalid_request_error",
            )
            return
        # 设备名**不要**在这里默认成插件 id：那样会盖住 toip._grant 的
        # 「沿用旧令牌名」回退，导致同一台设备用动态口令重接后被改名
        # （tokens.json 与日志的 token 列跟着变，历史对不上）。
        name = str(body.get("name") or body.get("device") or "").strip()
        client_id = str(body.get("client") or "dsh").strip().lower()

        from .toip import ToipError

        ip = self._client_ip()
        try:
            result = service.join(
                code=code,
                ticket=ticket,
                name=name,
                plugin_id=plugin_id,
                ip=ip,
                client_id=client_id,
            )
        except ToipError as exc:
            self._error(
                403 if "无效" in str(exc) or "不正确" in str(exc) or "过期" in str(exc) else 400,
                str(exc),
                "permission_error",
            )
            return

        root = self._http_root()
        models = [
            {"model_id": model, "context_window": window or None}
            for model, window in sorted(self.router.models().items())
            if result.token.allows(model)
        ]
        # 复用客户端档案生成通用接入载荷：未知 client_id 会抛 KeyError，
        # 那不该让已经发出的令牌白丢，所以退化成通用形态。
        try:
            payload = clients_module.onboarding(
                client_id, base_url=root, api_key=result.token_plain, models=models
            )
        except KeyError:
            payload = {
                "client": client_id,
                "display_name": client_id,
                "status": "guided",
                "inbound": "anthropic-messages",
                "endpoint": "/v1/messages",
                "base_url": root,
                "api_key": result.token_plain,
                "models": models,
            }

        payload["protocol"] = toip_module.PROTOCOL
        payload["version"] = toip_module.PROTOCOL_VERSION
        current = service.station()
        payload["station"] = {
            "id": current.station_id if current else "",
            "base_url": root,
        }
        payload["plugin"] = {
            "id": result.plugin_id,
            "version": str(body.get("plugin_version") or "")[:32],
            # 插件下次重接用的路径与头名：写在响应里，插件不必硬编码。
            "join_path": "/v1/toip/join",
            "session_path": "/v1/toip/session",
            "headers": {
                "plugin_id": toip_module.PLUGIN_ID_HEADER,
                "plugin_version": toip_module.PLUGIN_VERSION_HEADER,
                "station_id": toip_module.STATION_ID_HEADER,
            },
        }
        payload["session"] = {
            "token": result.token_plain,
            "token_hint": toip_module.token_hint_of(result.token_plain),
            "expires_at": float(result.token.expires_at or 0.0),
            "rotated": result.returned_session,
        }
        # DSH 客户端区块：插件的配置面只需要照抄这三个值。
        # base_url 必须带 /v1——DSH 的 Messages 适配器只在 pathname 不以
        # /v1 结尾时补 /v1（`messagesApiRoot`），带 /v1 是唯一确定的写法。
        payload["dsh"] = {
            "provider": "relayhub",
            "baseURL": f"{root}/v1",
            "apiKey": result.token_plain,
            "models": [
                {"id": item["model_id"], "contextWindow": item["context_window"] or None}
                for item in models
            ],
        }
        payload["otp"] = {
            "algorithm": toip_module.TOTP_ALGORITHM.upper(),
            "digits": toip_module.TOTP_DIGITS,
            "period": int(toip_module.TOTP_STEP),
            # 方便插件在 UI 上提示「还剩几秒」，减少跨窗失败。
            "seconds_left": round(toip_module.totp_seconds_left(), 1),
        }
        self._send_json(200, payload)

    def _authenticate(self, dialect: str = "anthropic") -> tuple[bool, DownstreamToken | None]:
        """鉴权并返回本次请求的身份。

        返回 (通过?, 令牌身份)。令牌身份为 None 表示 master 凭证或未启用鉴权；
        失败时已经把 401 写回客户端。

        鉴权面按 one-api 的两层语义组织：
          * **master 凭证**（`serve --api-key`）：网关自持的根凭证，不记用量、不受模型限制。
          * **下游令牌**（tokens.json）：每台设备一个，可禁用、可限模型、按令牌记账。
        """
        token_store = self.tokens
        has_tokens = bool(token_store and token_store.pool.tokens)
        if not self.api_key and not has_tokens:
            return True, None
        supplied = self._supplied_credential()
        if not supplied:
            self._error(401, "缺少凭证", "authentication_error", dialect)
            return False, None
        if token_store is not None:
            match = token_store.find(supplied)
            if match is not None:
                if not match.enabled:
                    self._error(
                        401, f"令牌 {match.name} 已被禁用", "authentication_error", dialect
                    )
                    return False, None
                if match.is_expired():
                    self._error(
                        401,
                        f"令牌 {match.name} 已于 "
                        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(match.expires_at))} 过期",
                        "authentication_error",
                        dialect,
                    )
                    return False, None
                # 令牌级额度：用完就拒，403。用 402 更贴切但客户端常把它当支付错误，
                # 403 至少能让人去看「这把 key 的额度」。
                if match.quota_exhausted():
                    self._error(
                        403,
                        f"令牌 {match.name} 的额度已用尽（{match.quota_tokens} tokens）",
                        "permission_error",
                        dialect,
                    )
                    return False, None
                # IP 白名单（对标 new-api）：令牌级别收窄来源，403 而不是 401——
                # 凭证是对的，只是不许从这个地址用。
                ip = self._client_ip()
                if not match.ip_allowed(ip):
                    self._error(
                        403,
                        f"令牌 {match.name} 不允许从 {ip or '未知来源'} 访问",
                        "permission_error",
                        dialect,
                    )
                    return False, None
                return True, match
        if self.api_key and hmac.compare_digest(supplied, self.api_key):
            return True, None
        self._error(401, "凭证无效或已吊销", "authentication_error", dialect)
        return False, None


    def _record(
        self, identity: DownstreamToken | None, ok: bool, tokens_in: int = 0, tokens_out: int = 0
    ) -> None:
        """按令牌记账。master 凭证与未启用鉴权的请求不记（它们不是设备流量）。"""
        if identity is None:
            return
        # 客户端身份速览：写进 usage（运行时状态，指纹已排除，不触发重载）。
        # 只在身份可信（src 有值且无 bad）时落——bad 也记 ip，但设备字段留空。
        ident = getattr(self, "_ident", None) or {}
        if ident.get("src") and not ident.get("bad"):
            identity.usage.last_ip = self._client_ip()
            identity.usage.last_user = ident.get("user", "")
            identity.usage.last_ver = ident.get("ver", "")
            identity.usage.last_device = ident.get("dev", "")
            identity.usage.last_device_id = ident.get("did", "")
        token_store = self.tokens
        if token_store is None:
            return
        try:
            token_store.report(identity, ok, tokens_in, tokens_out)
        except OSError as exc:
            # 记账失败不该弄死正在返回的响应，但必须留痕
            self.log_error("令牌用量落盘失败：%r", exc)

    def handle_one_request(self) -> None:
        """每个请求收尾核对结构不变量：请求体不许有剩余。

        没读完就关连接，Windows 会把这次关闭变成 RST，客户端可能读不到响应。
        计数器只给测试和排障用，不参与请求处理。
        """
        try:
            super().handle_one_request()
        finally:
            remaining = getattr(self, "_body_len", 0) - getattr(self, "_body_read", 0)
            if remaining > 0:
                type(self).unread_body_closes += 1

    def _read_json(self, dialect: str = "anthropic") -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "Content-Length 非法", dialect=dialect)
            return None
        raw = self._read_body_bytes(length)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype == e2e_module.ENVELOPE_CONTENT_TYPE:
            return self._open_envelope(raw, dialect)
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(400, f"请求体不是合法 JSON：{exc}", dialect=dialect)
            return None
        if not isinstance(parsed, dict):
            self._error(400, "请求体必须是 JSON 对象", dialect=dialect)
            return None
        return parsed

    def _open_envelope(self, raw: bytes, dialect: str) -> dict[str, Any] | None:
        """解开 E2E 信封，把明文请求交给既有链路。

        解密发生在鉴权**之后**（调用方保证），盐用本次供应的凭证串——同一份
        密文换个令牌就是密文不匹配，信封与身份绑定。解密失败统一 400：
        具体失败原因（密钥/GCM 校验）对窃听者也是信息，不外泄细节。
        """
        try:
            envelope = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(400, f"信封不是合法 JSON：{exc}", dialect=dialect)
            return None
        if not isinstance(envelope, dict):
            self._error(400, "信封必须是 JSON 对象", dialect=dialect)
            return None
        identity = self.server.e2e_identity()  # type: ignore[attr-defined]
        try:
            return e2e_module.open_envelope(
                identity, envelope, self._supplied_credential(), self.server.e2e_replay  # type: ignore[attr-defined]
            )
        except e2e_module.E2eError as exc:
            self._log_request(
                started=getattr(self, "_started", time.time()), dialect=dialect,
                identity=getattr(self, "_current_identity", None),
                ok=False, status=400, reason=f"e2e envelope rejected: {exc}",
            )
            self._error(400, str(exc), "invalid_request_error", dialect)
            return None

    # -- SSE -------------------------------------------------------------

    def _begin_sse(self) -> None:
        """用 chunked 编码，HTTP/1.1 下不依赖连接关闭来界定正文。"""
        self.send_response(200)
        self._echo_request_id()  # 流式同样要能拿 id 回溯（见 _echo_request_id）
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _sse(self, event: str, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False)
        self._sse_raw(f"event: {event}\ndata: {body}\n\n")

    def _sse_raw(self, text: str) -> None:
        """写一段原始 SSE 文本并按 event_delay 节流（OpenAI 那种只有 data 行也走这里）。"""
        self._write_chunk(text.encode("utf-8"))
        if self.event_delay:
            time.sleep(self.event_delay)

    def _end_chunks(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # -- 路由 ------------------------------------------------------------

    def _begin_request(self) -> str:
        """请求入口：定下本请求的 id（客户端带了就沿用它的）。

        必须在任何错误返回**之前**调用，否则早期错误（404 未知路径、
        403 拉黑、429 限流）响应里没有 X-Request-ID 可回——而那恰恰是
        最需要客户端拿 id 来申诉的几类。
        """
        request_id = (self.headers.get("X-Request-ID") or uuid.uuid4().hex)[:64]
        self._current_request_id = request_id
        try:
            self._body_len = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._body_len = 0
        self._body_read = 0
        return request_id

    def _read_body_bytes(self, length: int) -> bytes:
        """读请求体，并记账已读字节——回错误响应时要知道还剩多少没读。"""
        if length <= 0:
            return b""
        raw = self.rfile.read(length)
        self._body_read = getattr(self, "_body_read", 0) + len(raw)
        return raw

    def _drain_request_body(self) -> None:
        """把没读完的请求体读掉再回响应。

        为什么必须：客户端（urllib / requests）默认带 `Connection: close`，
        服务端读完响应就 close()。此时接收缓冲区里还压着没读完的请求体，
        Windows 发的是 **RST 而不是 FIN**，客户端于是偶尔读不到那个 401/403，
        只看到 `ConnectionAbortedError`——实测未鉴权 POST 600 次里挂 25 次
        （4.2%），表现成「像网络抖动」的随机失败。
        """
        remaining = getattr(self, "_body_len", 0) - getattr(self, "_body_read", 0)
        if remaining <= 0 or remaining > MAX_DRAIN_BODY:
            return
        try:
            self._read_body_bytes(remaining)
        except OSError:
            pass

    def do_GET(self) -> None:  # noqa: N802
        self._begin_request()
        # 注意别对根路径做 rstrip("/")："/" 会被剥成空串，导致根路径判断失效
        path = self.path.split("?")[0]
        if path != "/":
            path = path.rstrip("/")
        if path == "/panel/wallpaper":
            self._panel_wallpaper_json()
            return
        if path == "/":
            # 落地页与用户面板共用同一套外观（配色、卡片、壁纸、图标）
            self._send_html(_apply_panel_theme(landing_module.LANDING_HTML))
            return
        if path == "/.well-known/agent.json":
            station = toip_module.load_station(paths.toip_station_path())
            card = a2a_module.agent_card(
                paths.a2a_agents_path(),
                station.name if station else "relay-hub",
                self.server.base_url,
            )
            self._send_json(200, card)
            return
        if path == "/mcp/tools":
            authorized, identity = self._authenticate("anthropic")
            if not authorized:
                return
            payload = mcp_module.list_tools(paths.mcp_channels_path())
            self._send_json(200, payload)
            return
        if path == "/v1/e2e/params":
            # E2E 信封参数（公开材料，不鉴权）：客户端拿公钥后即可加密。
            # cryptography 缺失时回 501 + 指路，而不是 404 装作没这个功能。
            if e2e_module._crypto() is None:
                self._send_json(
                    501,
                    {"ok": False, "error": "服务端未启用 E2E：pip install \"hubrelay[e2e]\""},
                )
                return
            identity = self.server.e2e_identity()  # type: ignore[attr-defined]
            self._send_json(
                200,
                {"scheme": e2e_module.SCHEME, "key_id": identity.key_id,
                 "server_public": identity.public_b64(), "ts_window": e2e_module.TS_WINDOW},
            )
            return
        if path == "/healthz":
            # 无需凭证的探活端点：Docker HEALTHCHECK / 负载均衡 / 监控用。
            # 刻意不泄露版本号与模型信息——探活只需要知道"活着"。
            self._send_json(200, {"ok": True, "server": "relay-hub"})
            return
        if path == "/public/announcements":
            # 未认证可读，但只返回显式标记公开的公告（脱敏后）。
            config = site_module.load_site(paths.site_path())
            active = site_module.announcement_active(config)
            self._send_json(
                200,
                {
                    "text": config.announcement if active else "",
                    "level": config.announcement_level,
                    "pinned": bool(getattr(config, "announcement_pinned", False)),
                    "until": config.announcement_until,
                },
            )
            return
        if path == "/api/announcement":
            # 公开端点：面板/落地页都要能读到公告，不需要凭证；过期自动为空。
            site_config = site_module.load_site(paths.site_path())
            active = site_module.announcement_active(site_config) and site_config.announcement_public
            items = (
                [{
                    "text": site_config.announcement,
                    "level": site_config.announcement_level,
                    "until": site_config.announcement_until,
                }]
                if active
                else []
            )
            self._send_json(200, {"items": items})
            return
        if path == "/panel":
            panel = _apply_panel_theme(PANEL_HTML)
            panel = panel.replace("</head>", _theme_vars(site_module.load_site(paths.site_path())) + "</head>", 1)
            self._send_html(panel)
            return
        if path == "/api/user/tokens":
            self._user_tokens_list()
            return
        if path == "/api/user/models":
            self._user_models_market()
            return
        if path == "/api/user/leaderboard":
            self._user_leaderboard()
            return
        if path == "/api/user/audit":
            self._user_audit()
            return
        if path == "/api/user/logs":
            self._user_logs()
            return
        if path == "/api/user/me":
            user = self._session_user()
            if user is None or self.users is None:
                self._send_json(401, {"ok": False, "error": "请先登录"})
                return
            pool = self.users.pool
            fresh = pool.get(user.user_id)
            api_key_hint = ""
            for t in self.tokens.pool.tokens if self.tokens else []:
                if t.user_id == user.user_id:
                    api_key_hint = t.token_hint
                    break
            self._send_json(
                200,
                {
                    "ok": True,
                    "username": fresh.username if fresh else user.username,
                    "quota": fresh.quota if fresh else 0,
                    "used": fresh.used if fresh else 0,
                    "group": fresh.group if fresh else "default",
                    # 站点侧三级角色：user / vip / admin。控制台据此决定能做什么。
                    "role": (fresh.role if fresh else user.role) or "user",
                    "allow_register": pool.allow_register,
                    "api_key_hint": api_key_hint,
                    "unlimited": bool(fresh and fresh.quota < 0),
                    # —— #20 面板增强：名下密钥 / 可见模型 / 本人消费明细 ——
                    "keys": [self._token_view(t) for t in self._own_tokens(user)],
                    "email": str(getattr(user, "email", "") or ""),
                    "models": self._panel_models(user),
                    "key_limit": self.USER_TOKEN_LIMIT,
                    **dict(zip(("recent", "usage"), self._panel_recent(user))),
                },
            )
            return
        if path.startswith("/v1/_peer/"):
            self._handle_peer(path)
            return
        if path == "/v1/toip/station":
            # 公开端点：与 /v1/whoami 同级的信息披露——只报「本站支不支持
            # TOIP、该往哪发口令、口令怎么算」。**不含种子、口令、令牌**，
            # 所以发现成本与一次 404 等价。
            self._handle_toip_station()
            return
        if path == "/v1/toip/session":
            # 已接入插件的自查：我是谁、还剩多久、我这个令牌用了多少。
            # 与 /v1/models 同鉴权口径（走令牌），但只读且不改任何状态。
            service = self.toip
            if service is None:
                self._error(404, "本网关未启用 TOIP 接入", "not_found_error")
                return
            authorized, identity = self._authenticate()
            if not authorized or identity is None:
                if authorized:
                    # master 凭证没有「插件身份」，自查没有主语可说；
                    # 明确 400 比回一份全零的假身份更省排查时间。
                    self._error(
                        400,
                        "TOIP 自查需要下游令牌（master 凭证没有插件身份）",
                        "invalid_request_error",
                    )
                return
            self._send_json(200, service.describe_session(identity))
            return
        if path == "/v1/whoami":
            # 官方站自动识别（App 填个网址就能探测）：公开、只报协议身份与
            # 能力开关，不含模型清单、凭证线索与计数——探测成本与 404 相同。
            self._send_json(
                200,
                {
                    "server": "relay-hub",
                    "version": relayhub_version,
                    "pairing_open": bool(
                        getattr(self.server, "pairing", None)
                        and self.server.pairing.window_open()
                    ),
                    "services": {
                        "llm": True,
                    },
                    "pair_mode": str(
                        getattr(self.server, "pair_mode", "code")
                    ),
                },
            )
            return
        if path in (
            "/v1/dashboard/billing/subscription",
            "/v1/dashboard/billing/usage",
        ):
            # 本站也提供 new-api 风格的余额/用量查询；默认关闭，管理面开关控制。
            if not features_module.billing_enabled(paths.features_path()):
                self._error(
                    404, "billing API 未开放（管理面「账单」页可开启）", "not_found_error"
                )
                return
            authorized, identity = self._authenticate()
            if not authorized:
                return
            info = self.usage_for(identity)
            unlimited = bool(info.get("unlimited"))
            quota = int(info.get("quota") or 0)
            used = float(info.get("used") or 0)
            expires = int(getattr(identity, "expires_at", 0) or 0)
            if path.endswith("/subscription"):
                hard = -1.0 if unlimited else float(quota)
                self._send_json(
                    200,
                    {
                        "object": "billing_subscription",
                        "hard_limit_usd": hard,
                        "soft_limit_usd": hard,
                        "access_until": expires,
                        "display_type": "points",
                        "unlimited": unlimited,
                        "quota": quota,
                        "used": used,
                    },
                )
            else:
                self._send_json(
                    200,
                    {
                        "object": "list",
                        "total_usage": int(round(used * 100)),
                        "display_type": "points",
                        "used": used,
                        "quota": quota,
                        "unlimited": unlimited,
                    },
                )
            return
        if path == "/v1/usage":
            authorized, identity = self._authenticate()
            if not authorized:
                return
            if identity is not None and not getattr(identity, "allow_self_query", True):
                self._error(
                    403,
                    f"令牌 {identity.name} 不允许自查用量",
                    "permission_error",
                )
                return
            payload = self.usage_for(identity)
            if identity is not None:
                # 令牌级额度：和用户余额分开报，前端才分得清「钱没花完但 key 的额度用完了」
                payload["token_quota"] = int(getattr(identity, "quota_tokens", 0) or 0)
                payload["token_quota_left"] = int(identity.quota_left())
                payload["token_expires_at"] = float(getattr(identity, "expires_at", 0.0) or 0.0)
            self._send_json(200, payload)
            return
        if path != "/v1/models":
            self._error(404, f"未知路径 {self.path.split('?')[0]}", "not_found_error")
            return
        authorized, identity = self._authenticate()
        if not authorized:
            return
        created = int(time.time())
        data = []
        for model, window in sorted(self.router.models().items()):
            # 令牌级模型白名单同步到列表：限了模型的令牌不该看到用不了的模型
            # （one-api 同语义；master 凭证 identity=None，不受限）。
            if identity is not None and identity.models and model not in identity.models:
                continue
            item: dict[str, Any] = {
                "id": model,
                "object": "model",
                "created": created,
                "owned_by": "relay-hub",
            }
            if window:
                item["context_window"] = window
            data.append(item)
        self._send_json(200, {"object": "list", "data": data})

    def do_POST(self) -> None:  # noqa: N802
        self._begin_request()
        try:
            self._post()
        except Exception as exc:  # noqa: BLE001
            # 兜底：handler 里抛未捕获异常时，socketserver 只会静默掐断连接，
            # 客户端看到的是 RemoteDisconnected（像网络问题），排查方向会被带偏。
            # 宁可回一个难看的 500，也让它带上真实错误。
            self._note_unhandled(exc)
            fragment = self.path.split("?")[0].rstrip("/")
            dialect = "openai" if fragment == "/v1/chat/completions" else "anthropic"
            try:
                self._error(500, f"中转站内部错误：{type(exc).__name__}: {exc}", "api_error", dialect)
            except OSError:
                pass

    def _panel_wallpaper_json(self) -> None:
        """给面板自己的代理端点：返回 {url}，不含任何凭据。"""
        url = _fetch_wallpaper_url()
        if not url:
            self._error(404, "未配置壁纸接口或暂时取不到")
            return
        self._send_json(200, {"url": url})

    def _user_toip(self) -> None:
        """为用户本人签发一枚 TOIP 通行证（明文只返回一次）。"""
        user = self._session_user()
        if user is None:
            self._send_json(401, {"ok": False, "error": "请先登录"})
            return
        store = getattr(getattr(self, "toip", None), "tickets", None)
        if store is None:
            self._send_json(404, {"ok": False, "error": "本站未启用 TOIP"})
            return
        body = self._read_json() or {}
        name = str(body.get("name") or f"{user.username} 的客户端").strip()
        try:
            ttl = max(0.0, float(body.get("ttl") or 0))
        except (TypeError, ValueError):
            ttl = 0.0
        try:
            record, secret = toip_module.issue_for_user(
                store, user.user_id, name, ttl=ttl, note="用户自建"
            )
        except toip_module.ToipError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)[:200]})
            return
        self._send_json(200, {
            "ok": True, "ticket": secret, "name": record.name,
            "expires_at": record.expires_at,
            "note": "明文只显示这一次，请立即保存",
        })

    def _user_password(self, user: Any) -> None:
        """改密码：先验旧密码，成功后**踢掉本会话之外的所有会话**。

        只改密码不踢会话等于没改：旧设备上的 cookie 还能用 TTL 那么久。
        """
        body = self._read_json() or {}
        old = str(body.get("old_password") or "")
        new = str(body.get("new_password") or "")
        if self.users.verify(user.username, old) is None:
            self._send_json(403, {"ok": False, "error": "原密码不正确"})
            return
        try:
            def change(pool: Any) -> None:
                pool.set_password(user.user_id, new)

            self.users.mutate(change)
        except Exception as exc:  # UserError 或强度不足
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        dropped = self._drop_other_sessions(user.user_id)
        audit_module.record(
            "panel.password", path=paths.audit_path(),
            username=user.username, revoked_sessions=dropped,
        )
        self._send_json(200, {"ok": True, "revoked_sessions": dropped})

    def _drop_other_sessions(self, user_id: str) -> int:
        """保留当前 sid，其余同用户会话全部作废。返回踢掉的数量。"""
        keep = self._session_id()
        dropped = 0
        with self.server.session_lock:  # type: ignore[attr-defined]
            sessions = self.server.sessions  # type: ignore[attr-defined]
            for sid, (uid, _exp) in list(sessions.items()):
                if uid == user_id and sid != keep:
                    del sessions[sid]
                    dropped += 1
        return dropped

    def _user_sessions_revoke(self, user: Any) -> None:
        dropped = self._drop_other_sessions(user.user_id)
        self._send_json(200, {"ok": True, "revoked_sessions": dropped})

    def _user_models_market(self) -> None:
        """用户侧模型广场：我能不能用、多少钱、最近表现如何。

        三点必须诚实：
          * 价格是**我的**单价（全局价 × 我的分组倍率 × 当前时段倍率），不是上游进价；
          * 速度/成功率优先用**本人**最近 7 天的数据，样本不足时退回全站，
            并明确标记来源（scope 字段），避免把别人的体感当成自己的；
          * 渠道在冷却中的模型标 warm=false，而不是从列表里抹掉——
            「现在不能用」和「本站没有」是两件事。
        """
        user = self._session_user()
        if user is None:
            self._send_json(401, {"ok": False, "error": "请先登录"})
            return
        query = parse_qs(urlsplit(self.path).query)
        try:
            days = min(max(int((query.get("days") or ["7"])[0]), 1), 30)
        except ValueError:
            days = 7

        pool = self.users.pool if self.users else None
        mult = pool.multiplier_for(user) if pool else 1.0
        now_mult = pool.time_multiplier() if pool else 1.0
        price_in = (pool.price_in_per_1k if pool else 0.0) * mult * now_mult
        price_out = (pool.price_out_per_1k if pool else 0.0) * mult * now_mult

        models = self._panel_models(user)
        # 冷却状态：直接看渠道池里还有没有「启用 + 未冷却 + 支持该模型」的 key。
        # 用路由的 candidates() 会推进轮询游标，等于用户刷页面就改了调度顺序。
        keys = list(getattr(getattr(self.router, "pool", None), "keys", []) or [])
        stamp = time.time()

        def _warm(model: str) -> bool:
            if not keys:
                return True   # 拿不到渠道池就别谎报不可用
            return any(
                k.enabled and k.is_available(stamp) and (not k.models or model in k.models)
                for k in keys
            )

        warm = {m: _warm(m) for m in models}

        names = {t.name for t in self._own_tokens(user)}
        cutoff = time.time() - days * 86400
        try:
            rows = reqlog.tail(Path(self.request_log), limit=4000) if self.request_log else []  # type: ignore[attr-defined]
        except Exception:
            rows = []
        mine: dict[str, list[dict]] = {}
        site: dict[str, list[dict]] = {}
        for e in rows:
            if float(e.get("ts") or 0) < cutoff:
                continue
            model = str(e.get("model") or "")
            if not model:
                continue
            site.setdefault(model, []).append(e)
            if str(e.get("token") or "") in names:
                mine.setdefault(model, []).append(e)

        def _stats(entries: list[dict]) -> dict:
            ok = [e for e in entries if e.get("ok")]
            speeds = [
                float(e["tokens_out"]) / (float(e["latency_ms"]) / 1000.0)
                for e in ok
                if float(e.get("latency_ms") or 0) > 200 and float(e.get("tokens_out") or 0) > 0
            ]
            speeds.sort()
            median = speeds[len(speeds) // 2] if speeds else 0.0
            return {
                "calls": len(entries),
                "ok_rate": round(len(ok) / len(entries) * 100, 1) if entries else 0.0,
                "speed": round(median, 1),
            }

        out = []
        for m in models:
            mine_rows = mine.get(m, [])
            site_rows = site.get(m, [])
            if len(mine_rows) >= 3:
                stats, scope = _stats(mine_rows), "mine"
            elif site_rows:
                stats, scope = _stats(site_rows), "site"
            else:
                stats, scope = {"calls": 0, "ok_rate": 0.0, "speed": 0.0}, "none"
            out.append({
                "model": m,
                "warm": warm.get(m, True),
                "price_in_per_1m": round(price_in * 1000, 4),
                "price_out_per_1m": round(price_out * 1000, 4),
                "peak_now": now_mult > 1.0,
                "group_multiplier": mult,
                **stats,
                "scope": scope,
            })
        self._send_json(200, {
            "ok": True,
            "days": days,
            "group": getattr(user, "group", "default"),
            "group_multiplier": mult,
            "peak_now": now_mult > 1.0,
            "models": out,
        })

    _VENDOR_PATTERNS: tuple[tuple[str, str], ...] = (
        ("claude", "Anthropic"), ("anthropic", "Anthropic"),
        ("gpt", "OpenAI"), ("o1", "OpenAI"), ("o3", "OpenAI"), ("o4", "OpenAI"),
        ("gemini", "Google"), ("gemma", "Google"),
        ("deepseek", "DeepSeek"),
        ("glm", "Z.AI"), ("zhipu", "Z.AI"),
        ("qwen", "Alibaba"), ("qwq", "Alibaba"),
        ("kimi", "Moonshot"), ("moonshot", "Moonshot"),
        ("grok", "xAI"), ("doubao", "ByteDance"),
        ("mimo", "Xiaomi"), ("minimax", "MiniMax"), ("step", "StepFun"),
    )

    def _vendor_of(self, model: str) -> str:
        low = model.lower()
        for needle, vendor in self._VENDOR_PATTERNS:
            if low.startswith(needle) or needle in low:
                return vendor
        return "其他"

    def _user_leaderboard(self) -> None:
        """排行榜：按模型/厂商聚合调用量与 tokens（全站口径，仅聚合数）。

        隐私边界：只出计数与速率，不出任何用户名、令牌名、IP——排行本身就是
        公开榜单，但明细属于个人。样本不足的车次直接不列入（避免 1 次调用登顶）。
        """
        user = self._session_user()
        if user is None:
            self._send_json(401, {"ok": False, "error": "请先登录"})
            return
        query = parse_qs(urlsplit(self.path).query)
        period = str((query.get("period") or ["week"])[0])
        days = {"today": 1, "week": 7, "month": 30}.get(period, 7)
        # 「今天」按自然日算，不是滚动 24 小时
        cutoff = time.time() - days * 86400 if period != "today" else time.time() - (time.time() % 86400)

        try:
            rows = reqlog.tail(Path(self.request_log), limit=6000) if self.request_log else []  # type: ignore[attr-defined]
        except Exception:
            rows = []

        agg: dict[str, dict] = {}
        for e in rows:
            if float(e.get("ts") or 0) < cutoff:
                continue
            model = str(e.get("model") or "")
            if not model:
                continue
            item = agg.setdefault(model, {"calls": 0, "ok": 0, "tokens": 0, "speeds": []})
            item["calls"] += 1
            if e.get("ok"):
                item["ok"] += 1
                ms = float(e.get("latency_ms") or 0)
                out = float(e.get("tokens_out") or 0)
                if ms > 200 and out > 0:
                    item["speeds"].append(out / (ms / 1000.0))
            item["tokens"] += int(e.get("tokens_in") or 0) + int(e.get("tokens_out") or 0)

        models = []
        for name, it in agg.items():
            speeds = sorted(it["speeds"])
            models.append({
                "model": name,
                "vendor": self._vendor_of(name),
                "calls": it["calls"],
                "tokens": it["tokens"],
                "ok_rate": round(it["ok"] / it["calls"] * 100, 1) if it["calls"] else 0.0,
                "speed": round(speeds[len(speeds) // 2], 1) if speeds else 0.0,
            })
        models.sort(key=lambda m: (-m["calls"], -m["tokens"]))

        vendors: dict[str, dict] = {}
        for m in models:
            v = vendors.setdefault(m["vendor"], {"vendor": m["vendor"], "calls": 0, "tokens": 0, "models": 0})
            v["calls"] += m["calls"]
            v["tokens"] += m["tokens"]
            v["models"] += 1
        vendor_list = sorted(vendors.values(), key=lambda v: -v["calls"])

        self._send_json(200, {
            "ok": True,
            "period": period,
            "models": models[:15],
            "vendors": vendor_list[:8],
            "total_calls": sum(m["calls"] for m in models),
        })

    def _user_audit(self) -> None:
        """本人相关的审计事件（登录、密钥、密码、邮箱）。

        只按 username 过滤自己——管理员面那条 /api/audit 是全站的，
        用户面板不能复用，否则等于把别人的登录记录也给出去了。
        """
        user = self._session_user()
        if user is None:
            self._send_json(401, {"ok": False, "error": "请先登录"})
            return
        query = parse_qs(urlsplit(self.path).query)
        try:
            limit = min(max(int((query.get("limit") or ["100"])[0]), 1), 500)
        except ValueError:
            limit = 100
        events = audit_module.tail(paths.audit_path(), limit=500)
        mine = [
            {
                "ts": e.get("ts", 0),
                "event": str(e.get("event") or ""),
                "ip": str(e.get("ip") or e.get("client") or ""),
                "detail": {
                    k: v for k, v in e.items()
                    if k not in ("ts", "event", "ip", "client", "username")
                },
            }
            for e in events
            if str(e.get("username") or "") == user.username
        ]
        self._send_json(200, {"ok": True, "events": mine[-limit:][::-1]})  # 最新在前

    def _user_logs(self) -> None:
        """调用日志：按天/模型/结果筛选，可导出 CSV。

        数据源是 reqlog（每次调用一行），只取**本人令牌**产生的记录——
        判定口径与 usage_for 一致（按令牌名集合），避免看到别人的用量。
        """
        user = self._session_user()
        if user is None:
            self._send_json(401, {"ok": False, "error": "请先登录"})
            return
        query = parse_qs(urlsplit(self.path).query)
        try:
            days = min(max(int((query.get("days") or ["7"])[0]), 1), 90)
        except ValueError:
            days = 7
        model_filter = str((query.get("model") or [""])[0])
        status_filter = str((query.get("status") or [""])[0])   # ok | fail | ""
        want_csv = (query.get("format") or [""])[0] == "csv"

        names = {t.name for t in self._own_tokens(user)}
        if not names:
            self._send_json(200, {"ok": True, "entries": [], "total": 0, "models": []})
            return
        cutoff = time.time() - days * 86400
        limit = 5000 if want_csv else 300
        try:
            rows = reqlog.tail(Path(self.request_log), limit=limit * 3) if self.request_log else []
        except Exception:
            rows = []
        entries = []
        for e in rows:
            if str(e.get("token") or "") not in names:
                continue
            if float(e.get("ts") or 0) < cutoff:
                continue
            if model_filter and str(e.get("model") or "") != model_filter:
                continue
            if status_filter == "ok" and not e.get("ok"):
                continue
            if status_filter == "fail" and e.get("ok"):
                continue
            entries.append(e)
        entries = entries[-limit:]
        models = sorted({str(e.get("model") or "") for e in entries if e.get("model")})

        if want_csv:
            import csv as _csv
            import io as _io

            buf = _io.StringIO()
            writer = _csv.writer(buf)
            writer.writerow(["时间", "模型", "渠道", "结果", "输入tokens", "输出tokens", "缓存读", "耗时ms", "扣点"])
            for e in entries:
                writer.writerow([
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(e.get("ts") or 0))),
                    e.get("model", ""), e.get("channel", ""),
                    "成功" if e.get("ok") else "失败",
                    int(e.get("tokens_in") or 0), int(e.get("tokens_out") or 0),
                    int(e.get("cache_read") or 0), int(e.get("latency_ms") or 0),
                    int(e.get("cost") or 0),
                ])
            raw = buf.getvalue().encode("utf-8-sig")   # BOM：Excel 打开中文不乱码
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="usage.csv"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        self._send_json(200, {
            "ok": True,
            "days": days,
            "total": len(entries),
            "models": models,
            "entries": entries[-300:],
        })

    # ---- 邮箱验证码（注册 / 重置密码） ----

    def _auth_email_code(self) -> None:
        """发验证码。**不透露邮箱是否已注册**——否则这里是账号枚举接口。"""
        body = self._read_json() or {}
        cfg = mailer_module.load_config(paths.mail_path())
        if not cfg.ready:
            self._send_json(503, {"ok": False,
                                  "error": "本站未开启邮件服务，请联系站长开通或改用邀请/兑换方式注册"})
            return
        email = mailer_module.clean_email(body.get("email"))
        purpose = str(body.get("purpose") or "register")
        if not email:
            self._send_json(400, {"ok": False, "error": "邮箱格式不正确"})
            return
        if purpose not in mailer_module.PURPOSES:
            self._send_json(400, {"ok": False, "error": "用途只能是 register / reset"})
            return
        # 域名白名单只约束注册：找回密码不受限，否则域名被移除的人会被锁死在门外
        if purpose == "register" and not mailer_module.domain_allowed(email, cfg.allowed_domains):
            self._send_json(400, {"ok": False,
                                  "error": "本站仅允许指定域名邮箱注册：" +
                                           "、".join(cfg.allowed_domains)})
            return
        store = mailer_module.CodeStore(paths.verify_codes_path())
        try:
            code = store.issue(email, purpose)
        except mailer_module.MailError as exc:
            self._send_json(429, {"ok": False, "error": str(exc)})
            return
        try:
            mailer_module.send_code(cfg, email, code, purpose)
        except mailer_module.MailError as exc:
            # 发不出去就把码作废，避免"用户没收到但码已存在"的困惑
            store.verify(email, purpose, code)
            self._send_json(502, {"ok": False, "error": str(exc)})
            return
        audit_module.record("panel.email_code", path=paths.audit_path(),
                            purpose=purpose, ip=self.client_address[0])
        self._send_json(200, {"ok": True, "expires_in": mailer_module.CODE_TTL})

    def _auth_reset(self) -> None:
        """邮箱验证码重置密码。成功后踢掉该用户所有会话。"""
        body = self._read_json() or {}
        cfg = mailer_module.load_config(paths.mail_path())
        if not cfg.ready:
            self._send_json(503, {"ok": False, "error": "本站未开启邮件服务，请联系站长重置密码"})
            return
        email = mailer_module.clean_email(body.get("email"))
        code = str(body.get("code") or "")
        new_password = str(body.get("new_password") or "")
        if len(new_password) < 8:
            self._send_json(400, {"ok": False, "error": "新密码至少 8 位"})
            return
        ok, reason = mailer_module.CodeStore(paths.verify_codes_path()).verify(email, "reset", code)
        if not ok:
            self._send_json(400, {"ok": False, "error": reason})
            return
        target = None
        if self.users is not None:
            for u in self.users.pool.users:
                if u.email and u.email.lower() == email:
                    target = u
                    break
        if target is None:
            # 码对但没这个邮箱：同样回成功，不泄露「这个邮箱有没有注册」
            self._send_json(200, {"ok": True})
            return

        def change(pool: Any) -> None:
            pool.set_password(target.user_id, new_password)

        try:
            self.users.mutate(change)
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        dropped = self._drop_other_sessions(target.user_id)
        audit_module.record("panel.reset", path=paths.audit_path(),
                            username=target.username, revoked_sessions=dropped,
                            ip=self.client_address[0])
        self._send_json(200, {"ok": True})

    def _user_email(self, user: Any) -> None:
        """绑定邮箱：要密码确认（否则拿到 cookie 就能把找回渠道改成自己的）。"""
        body = self._read_json() or {}
        if self.users.verify(user.username, str(body.get("password") or "")) is None:
            self._send_json(403, {"ok": False, "error": "密码不正确"})
            return
        email = str(body.get("email") or "").strip()
        try:
            self.users.mutate(lambda pool: pool.set_email(user.user_id, email))
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        audit_module.record("panel.email", path=paths.audit_path(),
                            username=user.username, has_email=bool(email))
        self._send_json(200, {"ok": True, "email": email})

    # ---- 自助密钥：用户建自己的 key，参数自己选（额度/有效期/模型/IP/自查） ----

    USER_TOKEN_LIMIT = 20  # 单账号最多持有多少把，防刷爆索引

    def _own_tokens(self, user: Any) -> list[Any]:
        store = self.tokens
        if store is None:
            return []
        return [t for t in store.pool.tokens if t.user_id == user.user_id]

    def _token_view(self, t: Any) -> dict[str, Any]:
        """给本人看的视图：**不含明文**，只有尾 4 位。"""
        return {
            "token_id": t.token_id,
            "name": t.name,
            "hint": t.token_hint,
            "enabled": bool(t.enabled),
            "models": list(t.models),
            "quota_tokens": int(t.quota_tokens),
            "quota_left": int(t.quota_left()),
            "used": int(t.usage.tokens_in) + int(t.usage.tokens_out),
            "expires_at": float(t.expires_at or 0.0),
            "ip_allowlist": list(t.ip_allowlist),
            "allow_self_query": bool(t.allow_self_query),
            "rpm": int(t.rpm),
            "created_at": float(t.created_at or 0.0),
        }

    def _user_tokens_list(self) -> None:
        user = self._session_user()
        if user is None:
            self._send_json(401, {"ok": False, "error": "请先登录"})
            return
        items = [self._token_view(t) for t in self._own_tokens(user)]
        items.sort(key=lambda x: x["created_at"])
        self._send_json(200, {"ok": True, "tokens": items, "limit": self.USER_TOKEN_LIMIT})

    def _user_token_create(self, user: Any) -> None:
        body = self._read_json() or {}
        name = str(body.get("name") or "").strip()
        if not name:
            self._send_json(400, {"ok": False, "error": "请填写密钥名称"})
            return
        if len(self._own_tokens(user)) >= self.USER_TOKEN_LIMIT:
            self._send_json(
                409, {"ok": False, "error": f"每人最多 {self.USER_TOKEN_LIMIT} 把密钥，请先删掉不用的"}
            )
            return
        try:
            quota = max(0, int(body.get("quota_tokens") or 0))
            days = max(0, int(body.get("days") or 0))
            rpm = max(0, int(body.get("rpm") or 0))
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False, "error": "额度/有效期/RPM 必须是非负整数"})
            return
        models = [m.strip() for m in str(body.get("models_text") or "").split(",") if m.strip()]
        ips = [x.strip() for x in str(body.get("ip_text") or "").replace("\r", "\n").splitlines() if x.strip()]
        secret = generate_token()
        record = DownstreamToken(
            token_id=str(uuid.uuid4()),
            name=name,
            token=secret,
            models=tuple(models),
            quota_tokens=quota,
            rpm=rpm,
            expires_at=(time.time() + days * 86400) if days > 0 else 0.0,
            user_id=user.user_id,
            allow_self_query=bool(body.get("allow_self_query", True)),
            ip_allowlist=tuple(ips),
            note=f"panel:{user.username}",
        )
        self.tokens.mutate(lambda pool: pool.add(record))
        audit_module.record(
            "panel.token.add",
            path=paths.audit_path(),
            username=user.username,
            name=name,
            token_hint=_token_hint(secret),
            quota_tokens=quota,
        )
        # 明文只此一次；列表永远只有尾 4 位
        self._send_json(200, {"ok": True, "token": secret, "tokens": [self._token_view(t) for t in self._own_tokens(user)]})

    def _user_token_update(self, user: Any) -> None:
        body = self._read_json() or {}
        token_id = str(body.get("token_id") or "")
        own = {t.token_id for t in self._own_tokens(user)}
        if token_id not in own:
            # 别人的 key 与不存在给同一个回答：不泄露「这个 id 存在但不是你的」
            self._send_json(404, {"ok": False, "error": "找不到这把密钥"})
            return
        changed: list[str] = []

        def change(pool: Any) -> None:
            rec = next((x for x in pool.tokens if x.token_id == token_id), None)
            if rec is None:
                raise TokenError("找不到这把密钥")
            if "enabled" in body:
                rec.enabled = bool(body.get("enabled"))
                changed.append(f"enabled={rec.enabled}")
            if "quota_tokens" in body:
                rec.quota_tokens = max(0, int(body.get("quota_tokens") or 0))
                changed.append(f"quota_tokens={rec.quota_tokens}")
            if "allow_self_query" in body:
                rec.allow_self_query = bool(body.get("allow_self_query"))
                changed.append(f"allow_self_query={rec.allow_self_query}")
            if "models_text" in body:
                rec.models = tuple(
                    m.strip() for m in str(body.get("models_text") or "").split(",") if m.strip()
                )
                changed.append(f"models={len(rec.models)}")
            if "ip_text" in body:
                rec.ip_allowlist = tuple(
                    x.strip()
                    for x in str(body.get("ip_text") or "").splitlines()
                    if x.strip()
                )
                changed.append(f"ip_allowlist={len(rec.ip_allowlist)}")
            if "days" in body:
                days = max(0, int(body.get("days") or 0))
                rec.expires_at = (time.time() + days * 86400) if days > 0 else 0.0
                changed.append(f"days={days}")

        try:
            self.tokens.mutate(change)
        except (TokenError, ValueError) as exc:
            self._send_json(409, {"ok": False, "error": str(exc)})
            return
        if changed:
            audit_module.record(
                "panel.token.update", path=paths.audit_path(), username=user.username,
                token_hint=next((t.token_hint for t in self._own_tokens(user) if t.token_id == token_id), ""),
                changed=changed,
            )
        self._send_json(200, {"ok": True, "tokens": [self._token_view(t) for t in self._own_tokens(user)]})

    def _user_token_delete(self, user: Any) -> None:
        body = self._read_json() or {}
        token_id = str(body.get("token_id") or "")
        target = next((t for t in self._own_tokens(user) if t.token_id == token_id), None)
        if target is None:
            self._send_json(404, {"ok": False, "error": "找不到这把密钥"})
            return
        hint = target.token_hint
        self.tokens.mutate(lambda pool: pool.remove(token_id))
        audit_module.record(
            "panel.token.delete", path=paths.audit_path(),
            username=user.username, token_hint=hint,
        )
        self._send_json(200, {"ok": True, "tokens": [self._token_view(t) for t in self._own_tokens(user)]})

    def _user_usage(self) -> None:
        """用户自查各客户端用量。

        隐私边界：**强制 session 身份**——`?user=` 一律忽略，A 查不到 B 的明细。
        站长能看的最大粒度是「按用户」，客户端维度只属于用户本人。
        """
        user = self._session_user()
        if user is None:
            self._send_json(401, {"ok": False, "error": "请先登录"})
            return
        query = parse_qs(urlsplit(self.path).query)
        try:
            days = int((query.get("days") or ["7"])[0])
        except ValueError:
            days = 7
        days = max(1, min(90, days))

        names = {tok.name for tok in self.tokens.tokens_of(user.user_id)} if self.tokens else set()
        since = time.time() - days * 86400
        entries = []
        if names and self.request_log:
            for item in reqlog.tail(Path(self.request_log), limit=20000):
                if item.get("token") in names:
                    entries.append(item)
        report = usageagg_module.aggregate(entries, since=since, group_by="client")
        self._send_json(200, {"ok": True, "days": days, **report})

    def _note_unhandled(self, exc: BaseException) -> None:
        """500 必须可查：stderr（pythonw 下无处可去）+ errors.log + 请求日志。

        `self.path` 落盘前**去掉 query**：`?token=` 这类凭据不该进错误日志。
        """
        self.log_error("处理请求时未捕获异常：%r", exc)
        detail = traceback.format_exc()
        log_path = self.request_log
        if not log_path:
            return
        safe_path = self.path.split("?", 1)[0]
        error_log = Path(log_path).with_name("errors.log")
        try:
            if error_log.exists() and error_log.stat().st_size > 5_000_000:
                error_log.replace(error_log.with_suffix(".log.1"))
        except OSError:
            pass
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            header = f"===== {stamp} {safe_path} from {self._client_ip()} ====="
            with open(error_log, "a", encoding="utf-8") as fh:
                fh.write(chr(10) + header + chr(10) + detail)
        except OSError:
            pass
        try:
            reqlog.record(
                Path(log_path),
                status=500,
                ok=False,
                reason=f"unhandled {type(exc).__name__}: {str(exc)[:80]}",
                request_id=getattr(self, "_current_request_id", ""),
                ip=self._client_ip(),
            )
        except Exception:  # noqa: BLE001 - 记日志失败绝不能盖住原始错误
            pass

    def _post(self) -> None:
        path = self.path.split("?")[0].rstrip("/")
        if path == "/v1/pair":
            # 配对端点在鉴权之前：来配对的设备本来就还没有凭证。
            # 暴露面由配对窗口守着——没有活跃窗口时直接拒绝，常态下这个
            # 路径等价于不存在（pairing.py 的安全模型）。
            self._handle_pair()
            return
        if path in ("/v1/toip/join", "/v1/toip/enroll"):
            # TOIP 接入端点同样在鉴权之前：来接入的插件还没有令牌。
            # 两个路径的区别只是「拿什么换令牌」——join 收动态口令或登记口令，
            # enroll 只收一次性登记口令。暴露面由口令窗口 + 爆破闸门守着。
            self._handle_toip_join(path)
            return
        if path == "/v1/_peer/forward":
            # 站间直通：鉴权靠配对令牌（不是下游令牌），所以放在通用鉴权之前。
            # 内部仍会 loopback 调本站公开接口，令牌/额度/限流照常生效。
            self._peer_forward()
            return
        if path == "/v1/embeddings":
            self._handle_embeddings()
            return
        if path == "/mcp":
            self._handle_mcp()
            return
        if path == "/a2a":
            self._handle_a2a()
            return
        if path.startswith("/api/auth/") or path.startswith("/api/user/"):
            self._handle_panel_api(path)
            return
        gemini_path = re.match(
            r"^/v1beta/models/([^/:]+):(generateContent|streamGenerateContent)$", path
        )
        if path not in ("/v1/messages", "/v1/chat/completions", "/v1/responses") and not gemini_path:
            self._error(404, f"未知路径 {self.path.split('?')[0]}", "not_found_error")
            return
        started = time.monotonic()
        # 入站协议决定出站编码：各口共用同一套路由，只有「怎么说话」不同。
        dialect = ("openai" if path == "/v1/chat/completions"
                   else "responses" if path == "/v1/responses"
                   else "gemini" if gemini_path else "anthropic")

        # 单 IP 限流：放在鉴权之前——刷请求的人不带有效凭证也一样占带宽。
        # 鉴权失败的请求同样计入（这正是要防的：拿垃圾凭证打接口探测）。
        # 拉黑（IP）更靠前：被拉黑的来源连限流计数都不进，策略文件改完即生效。
        client_ip = self._client_ip()
        if self.policy is not None and self.policy.is_ip_blocked(client_ip):
            self._error(
                403,
                "来源已被中转站管理员拉黑，如有疑问请联系管理员",
                "permission_error",
                dialect,
            )
            self._log_request(
                started=started, dialect=dialect, identity=None,
                ok=False, status=403, reason="ip blacklisted",
            )
            return
        if self.ip_rpm > 0:
            if not self.ip_limiter.check(client_ip, self.ip_rpm):
                self._error(
                    429,
                    f"来源 {client_ip} 超过每分钟 {self.ip_rpm} 请求的 IP 限额，请稍后再试",
                    "rate_limit_error",
                    dialect,
                )
                self._log_request(
                    started=started, dialect=dialect, identity=None,
                    ok=False, status=429, reason="ip rate limit",
                )
                return

        # 链路标识（入口侧）：Via 含本站实例标记 或 Hops 已到上限 → 判环拒绝。
        # 比 LoopGuard 的内容指纹更强：就算上游改写了请求内容也拦得住；
        # 两者叠加是纵深防御（第三方网关会丢头，但不会改内容）。
        instance = self.server.instance_id  # type: ignore[attr-defined]
        inbound_via = self.headers.get("Via") or ""
        try:
            inbound_hops = int(self.headers.get("X-Relay-Hub-Hops") or 0)
        except ValueError:
            inbound_hops = 0
        request_id = self._current_request_id  # 入口已定好（见 _begin_request）
        # Hops 上限可配置（RELAYHUB_MAX_HOPS，默认 4）：覆盖「多家中转站级联」的
        # 合法长链。判环本身不靠 Hops 单打——Via 里的实例标记是每次启动随机生成
        # 的唯一 id（relayhub-<hex8>），部署之间不会撞；所以 A 站 → B 站（两家都用
        # relay-hub）是正常链路，B 只认自己的标记；只有链路真的绕回同一实例才判环。
        if inbound_hops >= MAX_HOPS or instance in inbound_via:
            self._error(
                RELAY_LOOP_STATUS,
                "检测到转发环路：Via/Hops 链路标识显示本请求已经过本站。"
                "请检查上游配置是否把本站指回了自己。",
                "api_error",
                dialect,
            )
            self._log_request(
                started=started, dialect=dialect, identity=None,
                ok=False, status=RELAY_LOOP_STATUS, reason="relay loop via headers",
            )
            return
        trace_headers = {
            "Via": (inbound_via + ", " + instance).strip(", "),
            "X-Relay-Hub-Hops": str(inbound_hops + 1),
            "X-Request-ID": request_id,
        }

        authorized, identity = self._authenticate(dialect)
        if not authorized:
            self._log_request(
                started=started, dialect=dialect, identity=None,
                ok=False, status=401, reason="unauthorized",
            )
            return
        # 客户端身份：鉴权成功后立即解——身份头的密钥是令牌哈希，
        # 必须先知道是哪枚令牌才验得了。解不出不拒请求（只标 bad）：
        # 管理是目的，不是新的墙。扩展包不在时整段跳过。
        self._ident: dict[str, str] = {}
        if identity is not None and ident_module is not None:
            self._ident = self._parse_identity(identity)
            # 拉黑（设备）：按 设备 ID，其次令牌名（后台管理两处都能填）。
            # 放在身份解析后：没有身份头的旧客户端仍有 IP 拉黑兜着。
            if self.policy is not None and self.policy.is_device_blocked(
                self._ident.get("did", ""), identity.name
            ):
                self._error(
                    403,
                    "该设备已被中转站管理员拉黑，如有疑问请联系管理员",
                    "permission_error",
                    dialect,
                )
                self._log_request(
                    started=started, dialect=dialect, identity=identity,
                    ok=False, status=403, reason="device blacklisted",
                )
                return
        # 优先队列判定：VIP 设备/IP 在并发闸门里插队（见 ConcurrencyGate.acquire）
        gate_priority = self.policy is not None and (
            self.policy.is_ip_priority(client_ip)
            or (
                identity is not None
                and self.policy.is_device_priority(
                    self._ident.get("did", ""), identity.name
                )
            )
        )
        # 角色加成：VIP 用户的令牌也进优先队列（与 policy 里的设备/IP 优先并列）。
        # 走权限矩阵而不是直接判 role ——「谁能插队」只在一处定义。
        if not gate_priority and identity is not None and identity.user_id and self.users:
            owner = self.users.pool.get(identity.user_id)
            if owner is not None and user_can(owner, "queue.priority"):
                gate_priority = True
        body = self._read_json(dialect)
        if body is None:
            self._log_request(
                started=started, dialect=dialect, identity=identity,
                ok=False, status=400, reason="invalid json",
            )
            return
        # body 身份块兜底：有身份头时也要把块摘掉（不漏给上游、不进缓存指纹）；
        # 没有身份头时它就是唯一身份来源。头部身份优先，body 只兜底。
        if ident_module is not None:
            body_block = ident_module.extract_body_identity(body)
            if body_block is not None and not self._ident:
                self._ident = body_block
        model = str(body.get("model") or "")
        # stream 要在协议翻译前取走：翻译后的 Anthropic 形态里没有这个字段。
        streaming = bool(body.get("stream"))
        if gemini_path:
            # 入站 Gemini：模型在路径里，请求体转成内部 Anthropic 形态再走同一条链路
            model = gemini_path.group(1)
            streaming = gemini_path.group(2) == "streamGenerateContent"
            body = gemini_api.from_gemini_request(body, model, streaming=streaming)
        if not model:
            self._error(400, "缺少 model", dialect=dialect)
            self._log_request(
                started=started, dialect=dialect, identity=identity,
                ok=False, status=400, reason="missing model",
            )
            return

        # 测试密钥走合成应答：任意模型名都接，不查清单、不受令牌模型白名单约束。
        is_test = identity is not None and identity.scope == SCOPE_TEST

        # 模型存在性在这一层拦。理由：这是「网关给不给这个模型」的契约，
        # 不该等发到上游才由上游报 404 —— 那时已经白烧一次配额，
        # 而且池子里的模型名和上游的模型名本来就不是一回事（要经过映射）。
        # 测试密钥例外：任意模型名都接（性能压测不在乎模型存不存在）。
        if not is_test:
            try:
                known = self.router.models(
                    group=(getattr(identity, "channel_group", "") or None)
                )
            except TypeError:
                # 第三方/测试路由器还是旧签名：不认 group 参数就退回全量清单，
                # 和 relay 调用点的降级同哲学——别让旧路由器把整个网关变 500。
                known = self.router.models()
            if model not in known:
                self._error(
                    404,
                    f"模型 {model} 不在中转站的可用列表里。可用：{sorted(known)}",
                    "not_found_error",
                    dialect,
                )
                self._log_request(
                    started=started, dialect=dialect, model=model, identity=identity,
                    ok=False, status=404, reason="unknown model",
                )
                return

        # 令牌级模型白名单：one-api 令牌的「可用模型」语义。
        # 放在 404 之后，这样「模型不存在」和「令牌无权用」的报错不会互相掩盖。
        if identity is not None and not is_test and not identity.allows(model):
            allowed = list(identity.models)
            self._error(
                403,
                f"令牌 {identity.name} 无权使用模型 {model}。该令牌可用：{allowed}",
                "permission_error",
                dialect,
            )
            self._log_request(
                started=started, dialect=dialect, model=model, identity=identity,
                ok=False, status=403, reason="token model restriction",
            )
            return

        # 限额准入（日配额 + RPM）。对所有令牌身份生效——公网上被打得最狠的
        # 恰恰是 test 令牌，它们最需要限流。放在模型校验之后：打错模型名的请求
        # 不该消耗配额；放在 relay 之前：超限请求不该触达上游。
        if identity is not None:
            ok_daily, reason = self.tokens.admit(identity) if self.tokens else (True, "")
            if not ok_daily:
                self._record(identity, False)
                self._error(429, f"令牌 {identity.name} {reason}", "rate_limit_error", dialect)
                self._log_request(
                    started=started, dialect=dialect, model=model, identity=identity,
                    ok=False, status=429, reason="daily quota exceeded",
                )
                return
            if not self.rate_limiter.check(
                identity.token_hash or identity.token, identity.rpm
            ):
                self._error(
                    429,
                    f"令牌 {identity.name} 超过每分钟 {identity.rpm} 请求的限额",
                    "rate_limit_error",
                    dialect,
                )
                self._log_request(
                    started=started, dialect=dialect, model=model, identity=identity,
                    ok=False, status=429, reason="rpm limit exceeded",
                )
                return

            # 用户额度与预扣费：绑定用户的令牌在 _finish_relay 里做
            # （预扣要在应答缓存查询之后——命中缓存不该被预扣冻结）。

        # 转发环路检测：内容指纹 + 在飞计数（对「上游又指回我们」的拓扑环兜底，
        # 中间哪怕隔着 new-api 这类不改内容的第三方网关也能命中）。
        # test 令牌走本地合成应答，不触上游，无环风险，不计数。
        fp = None
        if not is_test and self.loop_guard.enabled:
            fp = _request_fingerprint(model, body)
            relayed = bool(inbound_via or inbound_hops)
            if not self.loop_guard.acquire(fp, relayed=relayed):
                self._error(
                    RELAY_LOOP_STATUS,
                    "检测到转发环路：同一请求内容在飞次数超限。请检查上游是否指回了本站。",
                    "api_error",
                    dialect,
                )
                self._log_request(
                    started=started, dialect=dialect, model=model, identity=identity,
                    ok=False, status=RELAY_LOOP_STATUS, reason="relay loop detected",
                )
                return
        try:
            # 并发闸门：在飞满员时排队而不是硬拒（突发场景体验），队满/超时才 429。
            # 优先名单里的设备/IP 排 VIP 队，名额一释放先轮到他们。
            gate = self.conc_gate
            if gate is not None and not gate.acquire(priority=gate_priority):
                self._error(
                    429,
                    "系统繁忙：请求已进入排队但未能在限时内轮到，请稍后重试",
                    "rate_limit_error",
                    dialect,
                )
                self._log_request(
                    started=started, dialect=dialect, identity=identity,
                    ok=False, status=429, reason="concurrency queue timeout",
                )
                return
            try:
                self._finish_relay(
                    started, dialect, model, body, streaming, identity, is_test, trace_headers, fp
                )
            finally:
                if gate is not None:
                    gate.release()
        finally:
            if fp:
                self.loop_guard.release(fp)


    def _handle_a2a(self) -> None:
        """A2A 入口：message/send 转发到注册的下游 agent。

        鉴权与 LLM/MCP 同一面；params.agent 选择下游。转发带级联头（本站
        Via 标记 + Hops+1）——两站互指 = 真环，508；合法链路畅通（与 LLM
        中转同一套判环，测试钉在 test_relay_chain.py）。
        """
        dialect = "anthropic"
        started = time.time()
        authorized, identity = self._authenticate(dialect)
        if not authorized:
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "Content-Length 非法", dialect=dialect)
            return
        raw_body = self._read_body_bytes(length)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        request: dict[str, Any]
        if ctype == e2e_module.ENVELOPE_CONTENT_TYPE:
            request = self._open_envelope(raw_body, dialect)
            if request is None:
                return
        else:
            try:
                request = json.loads(raw_body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json(200, {"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32700, "message": f"请求体不是合法 JSON：{exc}"}})
                return
        method = str(request.get("method") or "")
        rpc_id = request.get("id", 1)
        params = request.get("params") or {}
        if method != "message/send":
            self._send_json(200, {"jsonrpc": "2.0", "id": rpc_id, "error": {
                "code": -32601,
                "message": f"未知方法 {method}（支持 message/send）"}})
            return
        agent_name = str(params.get("agent") or "")
        if not agent_name:
            self._send_json(200, {"jsonrpc": "2.0", "id": rpc_id, "error": {
                "code": -32602, "message": "params.agent 必填（目标 agent 名）"}})
            return
        inbound_via = self.headers.get("Via") or ""
        try:
            inbound_hops = int(self.headers.get("X-Relay-Hub-Hops") or 0)
        except ValueError:
            inbound_hops = 0
        task = {"jsonrpc": "2.0", "id": rpc_id, "method": "message/send",
                "params": params.get("message") or {}}
        response = a2a_module.send_task(
            paths.a2a_agents_path(), agent_name, task,
            cascade_headers={
                "Via": (inbound_via + ", " + self.server.instance_id).strip(", "),
                "X-Relay-Hub-Hops": str(inbound_hops + 1),
            },
        )
        ok = "error" not in response
        try:
            reqlog.record(
                self.path, token=identity.name if identity is not None else "master",
                model=f"a2a:{agent_name}", channel=agent_name, ok=ok,
                status=None if ok else 502, reason=None if ok else "upstream error",
                latency_ms=int((time.time() - started) * 1000),
            )
        except Exception:  # noqa: BLE001 - 记账失败不影响应答
            pass
        self._record(identity, ok)
        self._send_json(200, response)

    def _handle_mcp(self) -> None:
        """MCP 网关单入口：initialize / tools/list / tools/call（JSON-RPC 2.0）。

        鉴权与 LLM 端点同一面（下游令牌/master）；Content-Type 是信封时先解密
        ——E2E 保护层与协议无关。tools/call 按令牌 × 渠道记账（reqlog）。
        """
        dialect = "anthropic"
        started = time.time()
        authorized, identity = self._authenticate(dialect)
        if not authorized:
            return
        raw_body: bytes
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "Content-Length 非法", dialect=dialect)
            return
        raw_body = self._read_body_bytes(length)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        request: dict[str, Any]
        if ctype == e2e_module.ENVELOPE_CONTENT_TYPE:
            request = self._open_envelope(raw_body, dialect)
            if request is None:
                return
        else:
            try:
                request = json.loads(raw_body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json(200, {"jsonrpc": "2.0", "id": None, "error": {
                    "code": mcp_module.JSONRPC_PARSE_ERROR,
                    "message": f"请求体不是合法 JSON：{exc}"}})
                return
        method = str(request.get("method") or "")
        rpc_id = request.get("id", 1)
        params = request.get("params") or {}

        if method == "initialize":
            self._send_json(200, {"jsonrpc": "2.0", "id": rpc_id, "result": {
                "protocolVersion": mcp_module.MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "relay-hub", "version": relayhub_version},
            }})
            return
        if method == "tools/list":
            self._send_json(200, {"jsonrpc": "2.0", "id": rpc_id,
                                  "result": mcp_module.list_tools(paths.mcp_channels_path())})
            return
        if method == "tools/call":
            name = str((params or {}).get("name") or "")
            arguments = dict((params or {}).get("arguments") or {})
            response, channel = mcp_module.call_tool(paths.mcp_channels_path(), name, arguments)
            ok = "error" not in response
            try:
                reqlog.record(
                    self.path, token=identity.name if identity is not None else "master",
                    model=f"mcp:{name}", channel=channel or "mcp", ok=ok,
                    status=None if ok else 502, reason=None if ok else "upstream error",
                    latency_ms=int((time.time() - started) * 1000),
                )
            except Exception:  # noqa: BLE001 - 记账失败不影响应答
                pass
            self._record(identity, ok)
            self._send_json(200, response)
            return
        self._send_json(200, {"jsonrpc": "2.0", "id": rpc_id, "error": {
            "code": mcp_module.JSONRPC_METHOD_NOT_FOUND,
            "message": f"未知方法 {method}（支持 initialize / tools/list / tools/call）"}})

    def _handle_embeddings(self) -> None:
        """/v1/embeddings：RAG/文本工具的向量端点（OpenAI 协议形态）。

        中文：复用 chat 的鉴权 / 令牌模型白名单 / 日配额 / RPM 四道闸；
        非流式、请求体不进缓存与环路指纹（向量请求便宜且无环路拓扑价值）。
        test 令牌回本地合成向量，绝不触上游——与 chat 的测试密钥语义一致。

        English: /v1/embeddings with the same auth / model-whitelist / quota /
        RPM gates as chat. Non-streaming; bypasses cache & loop fingerprint.
        Test tokens get a locally synthesized embedding, never touching upstream.
        """
        started = time.monotonic()
        dialect = "openai"
        authorized, identity = self._authenticate(dialect)
        if not authorized:
            self._log_request(
                started=started, dialect=dialect, identity=None,
                ok=False, status=401, reason="unauthorized",
            )
            return
        body = self._read_json(dialect)
        if body is None:
            self._log_request(
                started=started, dialect=dialect, identity=identity,
                ok=False, status=400, reason="invalid json",
            )
            return
        model = str(body.get("model") or "")
        if not model:
            self._error(400, "缺少 model", dialect=dialect)
            self._log_request(
                started=started, dialect=dialect, identity=identity,
                ok=False, status=400, reason="missing model",
            )
            return
        is_test = identity is not None and identity.scope == SCOPE_TEST
        if identity is not None and not is_test and not identity.allows(model):
            self._error(
                403,
                f"令牌 {identity.name} 无权使用模型 {model}。该令牌可用：{list(identity.models)}",
                "permission_error",
                dialect,
            )
            self._log_request(
                started=started, dialect=dialect, model=model, identity=identity,
                ok=False, status=403, reason="token model restriction",
            )
            return
        if identity is not None:
            ok_daily, reason = self.tokens.admit(identity) if self.tokens else (True, "")
            if not ok_daily:
                self._record(identity, False)
                self._error(429, f"令牌 {identity.name} {reason}", "rate_limit_error", dialect)
                self._log_request(
                    started=started, dialect=dialect, model=model, identity=identity,
                    ok=False, status=429, reason="daily quota exceeded",
                )
                return
            if not self.rate_limiter.check(
                identity.token_hash or identity.token, identity.rpm
            ):
                self._error(
                    429,
                    f"令牌 {identity.name} 超过每分钟 {identity.rpm} 请求的限额",
                    "rate_limit_error",
                    dialect,
                )
                self._log_request(
                    started=started, dialect=dialect, model=model, identity=identity,
                    ok=False, status=429, reason="rpm limit exceeded",
                )
                return

        if is_test:
            inp = body.get("input")
            n = len(inp) if isinstance(inp, list) else 1
            if isinstance(inp, str):
                n = 1
            n = max(1, min(n, 2048))
            tokens_in = max(1, len(str(inp)) // 4)
            dim = 8
            data = {
                "object": "list",
                "model": model,
                "data": [
                    {
                        "object": "embedding",
                        "index": i,
                        "embedding": [round(((i * dim + j) % dim + 1) / dim, 4) for j in range(dim)],
                    }
                    for i in range(n)
                ],
                "usage": {"prompt_tokens": tokens_in, "total_tokens": tokens_in},
            }
            self._send_json(200, data)
            self._log_request(
                started=started, dialect=dialect, model=model, identity=identity,
                ok=True, tokens_in=tokens_in, channel=self.test_router.LABEL,
            )
            return

        try:
            data, label, tokens_in = self.router.relay_embeddings(model, body)
        except RouterError as exc:
            kind = "not_found_error" if exc.status == 404 else "api_error"
            self._record(identity, False)
            self._error(exc.status, str(exc), kind, dialect, code=getattr(exc, "code", None) or None,
                retry_after=getattr(exc, "retry_after", None),)
            self._log_request(
                started=started, dialect=dialect, model=model, identity=identity,
                ok=False, status=exc.status, reason=(getattr(exc, "detail", "") or str(exc))[:200],
            )
            return
        self._send_json(200, data)
        self._log_request(
            started=started, dialect=dialect, model=model, identity=identity,
            ok=True, tokens_in=tokens_in, channel=label,
        )

    def _finish_relay(
        self,
        started: float,
        dialect: str,
        model: str,
        body: dict[str, Any],
        streaming: bool,
        identity: DownstreamToken | None,
        is_test: bool,
        trace_headers: dict[str, str],
        fp: str | None,
    ) -> None:
        router = self.test_router if is_test else self.router

        if dialect == "responses":
            # Responses 请求先翻译成 OpenAI chat 形态，再统一归一化为 Anthropic IR。
            body = responses_to_chat(body)
        if dialect in ("openai", "responses"):
            # 网关内部只认 Anthropic 形态，OpenAI 请求先归一化，
            # 否则「OpenAI 客户端 + Anthropic 上游」会把错的请求体转过去。
            try:
                body = to_anthropic_request(body)
            except UpstreamError as exc:
                self._error(400, exc.detail, "invalid_request_error", dialect)
                self._log_request(
                    started=started, dialect=dialect, model=model, identity=identity,
                    ok=False, status=400, reason=exc.detail[:120],
                )
                return

        try:
            # 应答缓存（非流式）：命中即回缓存，不上游、不计费、不预扣（cache 是激励机制）
            pre = 0
            cache_key = None
            if not streaming and not is_test:
                if identity is not None and identity.user_id:
                    tenant = identity.user_id
                elif identity is not None or self.api_key:
                    tenant = "master"
                else:
                    tenant = "anon"
                cache_key = f"{tenant}:{fp}"
                cached = self.response_cache.get(cache_key)
                if cached is not None:
                    usage = cached.get("usage") or {}
                    self._record(
                        identity, True,
                        int(usage.get("input_tokens") or 0),
                        int(usage.get("output_tokens") or 0),
                    )
                    self._log_request(
                        started=started, dialect=dialect, model=model, identity=identity,
                        ok=True, stream=False, status=200,
                        tokens_in=int(usage.get("input_tokens") or 0),
                        tokens_out=int(usage.get("output_tokens") or 0),
                        channel="cache", reason="cache hit",
                    )
                    if dialect == "openai":
                        self._send_json(200, openai_completion(model, cached))
                    elif dialect == "responses":
                        self._send_json(200, responses_completion(model, cached))
                    else:
                        self._send_json(200, cached)
                    return

            # 用户额度预扣（对标 new-api 的 input pre-consume）：按输入体量估算先冻结，
            # 完工后按实际用量多退少补——堵「发大 prompt 中途断开白嫖上游」的洞。
            # 放在缓存查询之后：命中缓存不该被预扣冻结。
            if not is_test and identity is not None and identity.user_id and self.users:
                fresh = self.users.pool.get(identity.user_id)
                if fresh is None or not fresh.enabled:
                    self._error(
                        401, f"令牌所属用户 {identity.user_id[:8]}… 不存在或已禁用",
                        "authentication_error", dialect,
                    )
                    self._log_request(
                        started=started, dialect=dialect, model=model, identity=identity,
                        ok=False, status=401, reason="panel user disabled",
                    )
                    return
                est_input = len(extract_prompt(body)) // 4
                est_cost = self.users.pool.cost_of(fresh, est_input, 0, ts=started)
                if fresh.quota == 0 or (est_cost > 0 and fresh.quota < est_cost):
                    self._error(
                        429,
                        f"用户 {fresh.username} 额度不足：本请求预估 {est_cost} 点，"
                        f"余额 {fresh.quota} 点。请兑换或联系管理员",
                        "rate_limit_error",
                        dialect,
                    )
                    self._log_request(
                        started=started, dialect=dialect, model=model, identity=identity,
                        ok=False, status=429, reason="quota exhausted (pre-consume)",
                    )
                    return
                if self.users.pre_consume(identity.user_id, est_cost):
                    pre = est_cost

            try:
                outcome = router.relay(
                    model, body, streaming, trace_headers=trace_headers, fp=fp,
                    group=(getattr(identity, "channel_group", "") or None),
                    # 出口查重按「下游身份」分桶：一个用户反复发同一句
                    # （"继续"/重试）不该占掉另一个用户的额度。
                    guard_scope=(
                        getattr(identity, "token_hash", "")
                        or getattr(identity, "name", "")
                        or "anonymous"
                    ),
                )
            except TypeError as exc:
                # 第三方路由器（测试/扩展）可能还是旧签名：不认 trace 参数就退回旧调用
                if "unexpected keyword" not in str(exc):
                    raise
                outcome = router.relay(model, body, streaming)
        except RouterError as exc:
            # 走到这里说明还没往客户端写过字节，可以正常回错误码
            if pre and identity is not None and identity.user_id and self.users:
                self.users.settle(identity.user_id, pre, 0)  # 上游失败：预扣全退
            kind = "not_found_error" if exc.status == 404 else "api_error"
            self._record(identity, False)
            self._error(exc.status, str(exc), kind, dialect, code=getattr(exc, "code", None) or None,
                retry_after=getattr(exc, "retry_after", None),)
            self._log_request(
                started=started, dialect=dialect, model=model, identity=identity,
                ok=False, status=exc.status, reason=(getattr(exc, "detail", "") or str(exc))[:200],
            )
            return

        if streaming:
            ok = self._emit_stream(outcome, dialect)
            usage = outcome.usage
            # usage 在流尾才被填充，_emit_stream 消费完生成器后这里才读得到
            tokens_in, tokens_out = (usage.input, usage.output) if usage else (0, 0)
            cache_read = getattr(usage, "cache_read", 0) if usage else 0
            cache_creation = getattr(usage, "cache_creation", 0) if usage else 0
            cost = 0
            if identity is not None and identity.user_id and self.users:
                fresh = self.users.pool.get(identity.user_id)
                if fresh is not None:
                    if ok:
                        cost = self.users.pool.cost_of(fresh, tokens_in, tokens_out, ts=started, cache_read=cache_read,
                                 channel_ratio=getattr(getattr(outcome, "key", None), "cache_read_ratio", None),
                                 channel_cache_price=getattr(getattr(outcome, "key", None), "cache_read_price_per_1k", None),
                                 cache_write=cache_creation,
                                 channel_cache_write_price=getattr(getattr(outcome, "key", None), "cache_write_price_per_1k", None))
                        self.users.settle(identity.user_id, pre, cost)  # 多退少补
                    else:
                        # 流中断：上游已消费整段输入，按预扣额结清（不上浮也不退还）
                        # ——这才是预扣防刷的本意；真实网络抖动的损失由预扣上限封顶。
                        self.users.settle(identity.user_id, pre, pre)
                        cost = pre
            up_cost = (
                outcome.key.upstream_cost(tokens_in, tokens_out, ts=started, cache_read=cache_read)
                if getattr(outcome, "key", None) else 0
            )
            self._record(identity, ok, tokens_in, tokens_out)
            tool_search, tool_fetch, tool_image = getattr(usage, "tool_usage", (0, 0, 0))
            self._log_request(
                started=started, dialect=dialect, model=model, identity=identity,
                ok=ok, stream=True, status=200,
                tokens_in=tokens_in, tokens_out=tokens_out,
                cache_read=cache_read, cache_creation=cache_creation,
                cost=cost, upstream_cost=up_cost, channel=outcome.label,
                reason="" if ok else "stream interrupted",
                tool_search=tool_search, tool_fetch=tool_fetch, tool_image=tool_image,
            )
            return
        if outcome.message is None:
            if pre and identity is not None and identity.user_id and self.users:
                self.users.settle(identity.user_id, pre, 0)  # 空结果：预扣全退
            self._record(identity, False)
            self._error(502, "路由返回了空结果", "api_error", dialect)
            self._log_request(
                started=started, dialect=dialect, model=model, identity=identity,
                ok=False, status=502, reason="empty relay outcome",
            )
            return
        usage = (outcome.message.get("usage") or {}) if outcome.message else {}
        cache_read, cache_creation = cache_from_usage(usage)
        tokens_in = int(usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
        cost = 0
        if identity is not None and identity.user_id and self.users:
            fresh = self.users.pool.get(identity.user_id)
            if fresh is not None:
                cost = self.users.pool.cost_of(fresh, tokens_in, tokens_out, ts=started, cache_read=cache_read,
                                 channel_ratio=getattr(getattr(outcome, "key", None), "cache_read_ratio", None),
                                 channel_cache_price=getattr(getattr(outcome, "key", None), "cache_read_price_per_1k", None),
                                 cache_write=cache_creation,
                                 channel_cache_write_price=getattr(getattr(outcome, "key", None), "cache_write_price_per_1k", None))
                self.users.settle(identity.user_id, pre, cost)  # 多退少补
        # 记账赶在写响应之前：响应字节一出去，客户端就可能立刻断开，
        # 账还没落盘就被读到旧值（测试里表现为偶发丢一次）。
        up_cost = (
            outcome.key.upstream_cost(tokens_in, tokens_out, ts=started, cache_read=cache_read)
            if getattr(outcome, "key", None) else 0
        )
        self._record(identity, True, tokens_in, tokens_out)
        if cache_key:
            self.response_cache.put(cache_key, outcome.message)

            # 指纹观测：一次内存写 + 置脏标记，实际落盘交给 persist 线程
            try:
                changed = (self.drift_store.observe(
                    str(outcome.label or "unknown"), str(outcome.model or model), outcome.message
                ) if self.drift_store else [])
                if changed:
                    audit_module.record(
                        "drift.changed", path=self.server.audit_path,
                        channel=str(outcome.label or ""), model=str(outcome.model or model),
                        changed=",".join(changed),
                    )
                    persist.writer_for(
                        self.drift_store.path, lambda path: self.drift_store.save()
                    ).mark()
            except Exception:  # noqa: BLE001 - 观测失败绝不影响这次响应
                pass
        tool_search, tool_fetch, tool_image = tool_usage_from_usage(usage)
        self._log_request(
            started=started, dialect=dialect, model=model, identity=identity,
            ok=True, stream=False, status=200,
            tokens_in=tokens_in, tokens_out=tokens_out,
            cache_read=cache_read, cache_creation=cache_creation,
            cost=cost, upstream_cost=up_cost, channel=outcome.label,
            tool_search=tool_search, tool_fetch=tool_fetch, tool_image=tool_image,
        )
        if dialect == "openai":
            self._send_json(200, openai_completion(model, outcome.message))
        elif dialect == "responses":
            self._send_json(200, responses_completion(model, outcome.message))
        elif dialect == "gemini":
            self._send_json(200, gemini_api.to_gemini_response(outcome.message, model))
        else:
            self._send_json(200, outcome.message)

    def _emit_stream(self, outcome: RelayOutcome, dialect: str = "anthropic") -> bool:
        """写整条流。返回 False 表示中途出错（调用方据此记令牌失败，不再补状态码）。"""
        self._begin_sse()
        try:
            if dialect == "gemini":
                for chunk in gemini_api.to_gemini_chunks(outcome.events or ()):
                    self._sse_raw(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
            elif dialect == "openai":
                for item in openai_chunks(outcome.model, outcome.events or ()):
                    self._sse_raw(f"data: {json.dumps(item, ensure_ascii=False)}\n\n")
                self._sse_raw("data: [DONE]\n\n")
            elif dialect == "responses":
                for name, payload in responses_chunks(outcome.model, outcome.events or ()):
                    self._sse(name, payload)
            else:
                for event, payload in outcome.events or ():
                    self._sse(event, payload)
        except (RouterError, OSError, RuntimeError) as exc:
            # 响应头已发出，改不了状态码；只能发一个 error 事件再收尾。
            # 上游失败已在 router 里记过账，这里不重复计。
            message = str(exc)
            try:
                if dialect == "openai":
                    error = {"error": {"message": message, "type": "api_error", "code": None}}
                    self._sse_raw(f"data: {json.dumps(error, ensure_ascii=False)}\n\n")
                    self._sse_raw("data: [DONE]\n\n")
                elif dialect == "responses":
                    err = {"type": "error", "code": "api_error",
                           "message": message, "param": None, "sequence_number": -1}
                    self._sse_raw(
                        "event: error\ndata: "
                        + json.dumps(err, ensure_ascii=False) + "\n\n"
                    )
                else:
                    payload = {
                        "type": "error",
                        "error": {"type": "api_error", "message": message},
                    }
                    self._sse_raw(
                        f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    )
            except OSError:
                pass
            return False
        finally:
            try:
                self._end_chunks()
            except OSError:
                pass
        return True


class LoopGuard:
    """转发环路检测：同一内容指纹的在飞请求计数。

    为什么用内容指纹而不是跳数头：中间隔一层 new-api 这类第三方网关，
    自定义头会被丢掉；但**请求内容原样透传**是所有中转站的共性。

    **两条阈值**（这是修掉误伤的关键）：
      * `relayed=True`（请求带 Via / X-Relay-Hub-Hops，说明它穿过了中转）
        → 紧阈值 3。真环路必然经过中转，每跳 +1，第 4 个就该拦。
      * `relayed=False`（直连客户端，没有任何链路标识）
        → 宽阈值 32。八个客户端同时发一模一样的 prompt 是正常负载，
        旧版用同一个 3 会误伤第 4 个，客户端看到的是 429「检测到转发环路」。

    直连阈值可用 RELAYHUB_LOOP_DIRECT_LIMIT 调整；设 0 表示只靠 Via/Hops 判环。
    """

    def __init__(self, limit: int = 3, direct_limit: int | None = None, mode: str | None = None) -> None:
        # mode: on（默认，双阈值）/ strict（全部按紧阈值）/ off（**只关内容指纹**）
        self.mode = (mode or os.environ.get("RELAYHUB_LOOP_GUARD", "on")).strip().lower()
        self.enabled = self.mode != "off"
        self._relay_limit = max(2, limit)
        if direct_limit is None:
            try:
                direct_limit = int(os.environ.get("RELAYHUB_LOOP_DIRECT_LIMIT", "32"))
            except ValueError:
                direct_limit = 32
        self._direct_limit = max(0, direct_limit)
        self._lock = threading.Lock()
        self._inflight: dict[str, int] = {}

    def acquire(self, fingerprint: str, relayed: bool = False) -> bool:
        if not self.enabled:
            return True
        if self.mode == "strict":
            relayed = True
        limit = self._relay_limit if relayed else self._direct_limit
        if limit <= 0:
            return True                     # 直连不设限：只靠 Via/Hops 判环
        with self._lock:
            count = self._inflight.get(fingerprint, 0)
            if count >= limit:
                return False
            self._inflight[fingerprint] = count + 1
            return True

    def release(self, fingerprint: str) -> None:
        with self._lock:
            count = self._inflight.get(fingerprint, 0) - 1
            if count <= 0:
                self._inflight.pop(fingerprint, None)
            else:
                self._inflight[fingerprint] = count


def _request_fingerprint(model: str, body: dict[str, Any]) -> str:
    """请求内容指纹：环路检测与应答缓存共用的键。

    键设计规范（刻意为之）：
      * **包含**语义参数：模型、规范化 messages、tools/tool_choice、
        temperature/top_p/max_tokens/stop、stream——这些变了语义就变了；
      * **绝不包含**：X-Request-ID/Via/Hops 等链路标识、Authorization、
        时间戳、nonce、客户端 IP、User-Agent——它们每跳都变，进了键
        就永远不命中，环路检测也随之失效；
      * `v` 是键版本号：指纹算法升级时递增，旧缓存自然过期，
        升级不会造成语义错配，也不该让命中率永久归零。
    """
    material = json.dumps(
        {
            "v": 1,
            "model": model,
            "messages": body.get("messages"),
            "tools": body.get("tools"),
            "tool_choice": body.get("tool_choice"),
            "temperature": body.get("temperature"),
            "top_p": body.get("top_p"),
            "max_tokens": body.get("max_tokens"),
            "stop": body.get("stop"),
            "stream": bool(body.get("stream")),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ResponseCache:
    """非流式应答缓存：命中即免上游调用、免计费。

    隔离维度：键前缀带调用者身份（user_id / master / anon）——公网多用户
    场景下不做跨用户共享（A 付费的答案不能白给 B，也避免响应串户）。
    只缓存非流式 200 应答；流式的产出依赖逐事件透传，缓存收益为负。
    进程内存态，重启清零；TTL 300s、上限 512 条（超出淘汰最旧）。
    """

    TTL = 300.0
    MAX_ENTRIES = 512

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            ts, message = item
            if now - ts > self.TTL:
                self._items.pop(key, None)
                return None
            return message

    def put(self, key: str, message: dict[str, Any]) -> None:
        with self._lock:
            if len(self._items) >= self.MAX_ENTRIES:
                oldest = min(self._items, key=lambda k: self._items[k][0])
                self._items.pop(oldest, None)
            self._items[key] = (time.time(), message)


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # accept 队列深度。默认 5 在并发 100 下会直接丢连接（实测 9/300 失败、
    # QPS 被压到 18）；512 后同一压测 QPS 491（约 27 倍）。
    request_queue_size = 512

    def server_bind(self) -> None:
        """关掉 Nagle。SSE 每次只推几 KB 的小包，Nagle 攒包 + 延迟 ACK
        会叠出约 40ms 的首字延迟——流式体感全毁在这个默认值上。"""
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        super().server_bind()

    def handle_error(self, request: Any, client_address: tuple[str, int]) -> None:
        """客户端半途掐连接（探活/负载器常见）只值得一行日志，不值得整段堆栈刷屏。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            if getattr(self, "verbose", False):
                self.log_message("客户端断开 %s: %r", client_address, exc)
            return
        super().handle_error(request, client_address)


    def __init__(
        self,
        address: tuple[str, int],
        router: Any,
        api_key: str | None = None,
        event_delay: float = 0.0,
        verbose: bool = False,
        tokens: TokenStore | None = None,
        pairing: Any = None,
        request_log: Any = None,
        users: UserStore | None = None,
        redeem: RedeemStore | None = None,
        loop_limit: int = 3,
        loop_guard: str | None = None,
        ip_rpm: int = 600,
        max_concurrency: int = 16,
        queue_size: int = 16,
        queue_wait: float = 30.0,
        policy: Any = None,
        pair_mode: str = "code",
        toip: Any = None,
        plugin_log_home: Path | None = None,
    ) -> None:
        if event_delay < 0:
            raise ValueError("event_delay 不能为负")
        if pair_mode not in ("code", "auto-lan"):
            raise ValueError("pair_mode 只能是 code 或 auto-lan")
        # 先查接口再绑端口。漏了这步的后果很误导：请求打进来时在 handler 里抛
        # AttributeError，表现是「连接被直接掐断」（RemoteDisconnected），
        # 看起来像网络问题而不是「参数传错了」。
        for method in ("relay", "models"):
            if not callable(getattr(router, method, None)):
                raise TypeError(
                    f"router 必须是实现了 relay()/models() 的路由器，"
                    f"收到 {type(router).__name__}（缺 {method}）。"
                    "只想用演示渠道池的话，请包一层 DemoRouter。"
                )
        if pairing is not None and not callable(getattr(pairing, "redeem", None)):
            raise TypeError("pairing 必须实现 redeem()（用 pairing.PairingService）")
        super().__init__(address, RelayHandler)
        self.router = router
        self.api_key = api_key
        self.event_delay = event_delay
        self.verbose = verbose
        self.tokens = tokens
        self.pairing = pairing
        # 配对模式：code（默认，带外配对码）/ auto-lan（局域网免码，幂等复用）
        self.pair_mode = pair_mode
        self.request_log = request_log
        # 测试密钥路由与限流器：网关自持，无需外部注入
        self.test_router = TestRouter()
        self.rate_limiter = RateLimiter()
        # 单 IP 限流：防「一个出口 IP 上跑一堆设备刷请求」。与令牌 RPM 互补——
        # 令牌限的是每台设备的凭证，IP 限的是共享出口（NAT）整体频率。
        self.ip_limiter = RateLimiter()
        self.ip_rpm = max(0, ip_rpm)  # 0 = 关闭
        # 并发闸门 + 排队：突发并发时排队而非硬拒（见 ConcurrencyGate）。
        self.conc_gate = (
            ConcurrencyGate(max_concurrency, queue_size, queue_wait)
            if max_concurrency > 0
            else None
        )
        # 转发环路检测：内容指纹 + 在飞计数（阈值 3）
        self.loop_guard = LoopGuard(loop_limit, mode=loop_guard)
        # 上游响应特征观测：只在响应**已经拿到之后**做，不在请求路径阻塞
        # 观测开关：RELAYHUB_DRIFT=off 可关掉（压测 A/B 用，生产建议开着）
        self.drift_enabled = os.environ.get("RELAYHUB_DRIFT", "on").strip().lower() != "off"
        self.drift_store = drift_module.Store(paths.drift_path()) if self.drift_enabled else None
        # 链路标识：本站实例标记（进 Via 头，环路检测的最强信号）
        self.instance_id = f"relayhub-{uuid.uuid4().hex[:8]}"
        # E2E 信封：身份懒加载（首次用到才生成/读盘），重放去重内存态
        self._e2e_identity: e2e_module.E2eIdentity | None = None
        self.e2e_replay = e2e_module.ReplayGuard()
        # 接入策略（拉黑 + 优先名单）：后台管理改文件即时生效（指纹热加载）。
        # 传 None = 无策略文件（全放行、全普通队），测试与单机自用零负担。
        self.policy = policy
        # 非流式应答缓存（按调用者隔离，TTL 300s）
        self.response_cache = ResponseCache()
        # 用户体系（公网面板 + 额度计费）。默认挂标准数据根；
        # users.json 不存在时是「未开放注册的空池」，面板功能自然降级。
        self.users = users if users is not None else UserStore(paths.users_path())
        self.redeem = redeem if redeem is not None else RedeemStore(paths.redeem_codes_path())
        # 登录会话（内存态）：sid -> (user_id, expires)。重启全员下线，对面板是特性。
        self.sessions: dict[str, tuple[str, float]] = {}
        self.session_lock = threading.Lock()
        # TOIP 接入服务（toip.py）。None = 本网关未启用 TOIP，/v1/toip/* 全 404。
        # 服务本身按需从数据根读 station/tickets 文件，所以 CLI 只需传一个
        # ToipService 实例，不必把两个路径散到构造签名里。
        self.toip = toip
        # 插件日志根（pluginlogs/ 的父目录）。None = 用默认数据根；
        # 测试传 tmp_path 让插件日志不落进真实数据根。
        self.plugin_log_home = plugin_log_home

    def e2e_identity(self) -> e2e_module.E2eIdentity:
        """站点 X25519 静态身份：首次用到才生成/读盘（secretbox 加密）。"""
        if self._e2e_identity is None:
            self._e2e_identity = e2e_module.load_or_create(paths.e2e_identity_path())
        return self._e2e_identity

    @property
    def base_url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}"

    def toip_summary(self) -> dict[str, Any]:
        """给发现应答器用的 TOIP 能力块（放 UDP 包里的那一小份）。

        刻意比 `GET /v1/toip/station` 更瘦：UDP 应答要挤进 4096 字节的
        接收缓冲，而且发现阶段只需要「支不支持 + 往哪问」。细节让客户端
        发现之后再走 HTTP 拿——这也让应答包不会随端点增多而膨胀。
        """
        service = self.toip
        if service is None:
            return {"enabled": False}
        public = service.station_public()
        if not public.get("enabled"):
            return {"enabled": False}
        return {
            "enabled": True,
            "protocol": public.get("protocol", toip_module.PROTOCOL),
            "version": public.get("version", toip_module.PROTOCOL_VERSION),
            "station_id": public.get("station_id", ""),
            "join": "/v1/toip/join",
            "station": "/v1/toip/station",
        }


def demo_pool() -> ChannelPool:
    """演示用渠道池：一个坏渠道在前，用于验证故障切换。"""
    models = {"glm-5.2": 1000000, "deepseek-v4-pro": 1000000, "deepseek-v4-flash": 1000000}
    return ChannelPool(
        [
            Channel(name="primary-down", models=dict(models), broken=True),
            Channel(name="secondary", models=dict(models)),
        ]
    )


def demo_router() -> DemoRouter:
    return DemoRouter(demo_pool())