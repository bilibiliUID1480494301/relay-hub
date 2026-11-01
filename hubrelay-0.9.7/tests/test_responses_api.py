# -*- coding: utf-8 -*-
"""OpenAI Responses ⇄ Anthropic 翻译层的纯函数测试（不走 HTTP）。

OSS 树的活跃路由是两跳：`responses_to_chat`（Responses→OpenAI chat） →
`to_anthropic_request`（chat→IR）。每一跳都可能丢东西，所以除了单跳用例，
还必须有**全链**用例：图片与档位要活着走到 IR 里。
"""
from __future__ import annotations

import json

from relayhub.gateway.responses_api import (
    to_anthropic_request,
    to_response_events,
    to_response_object,
)
from relayhub.gateway.service import responses_to_chat
from relayhub.gateway.upstream import to_anthropic_request as chat_to_ir


def test_string_input_and_instructions() -> None:
    out = to_anthropic_request({
        "model": "m", "input": "你好",
        "instructions": "只答一句话",
        "max_output_tokens": 64, "temperature": 0.3,
    })
    assert out["system"] == "只答一句话"
    assert out["messages"] == [{"role": "user", "content": "你好"}]
    assert out["max_tokens"] == 64
    assert out["temperature"] == 0.3


def test_message_list_and_tool_roundtrip() -> None:
    body = {
        "model": "m",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "查天气"}]},
            {"type": "function_call", "call_id": "call_1", "name": "weather",
             "arguments": '{"city": "北京"}'},
            {"type": "function_call_output", "call_id": "call_1", "output": "晴"},
        ],
        "tools": [{"type": "function", "name": "weather",
                   "description": "查天气", "parameters": {"type": "object"}}],
    }
    out = to_anthropic_request(body)
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "user"]
    assert out["messages"][1]["content"][0]["type"] == "tool_use"
    assert out["messages"][1]["content"][0]["input"] == {"city": "北京"}
    assert out["messages"][2]["content"][0]["tool_use_id"] == "call_1"
    assert out["tools"][0]["name"] == "weather"


def test_reasoning_effort_maps_through() -> None:
    out = to_anthropic_request({"model": "m", "input": "hi",
                                "reasoning": {"effort": "HIGH"}})
    assert out["reasoning_effort"] == "high"
    # 站点自己广告的词也透传：上游不认会自己报错，比静默吞掉好排查
    site_word = to_anthropic_request({"model": "m", "input": "hi",
                                      "reasoning": {"effort": "max"}})
    assert site_word["reasoning_effort"] == "max"
    # 没有 reasoning 字段时不出键
    assert "reasoning_effort" not in to_anthropic_request({"model": "m", "input": "hi"})


def test_input_image_becomes_anthropic_blocks() -> None:
    """Responses 的 input_image → Anthropic image 块：图片不能静默丢。"""
    out = to_anthropic_request({"model": "m", "input": [
        {"role": "user", "content": [
            {"type": "input_text", "text": "这张是什么"},
            {"type": "input_image", "image_url": "https://example.com/a.png"},
        ]},
    ]})
    blocks = out["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "这张是什么"}
    assert blocks[1] == {"type": "image",
                         "source": {"type": "url", "url": "https://example.com/a.png"}}


def test_input_image_data_url_splits_into_base64_source() -> None:
    out = to_anthropic_request({"model": "m", "input": [
        {"role": "user", "content": [
            {"type": "input_image", "image_url": "data:image/png;base64,aGk="},
        ]},
    ]})
    blocks = out["messages"][0]["content"]
    assert blocks == [{"type": "image",
                       "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}}]


def test_text_only_content_stays_a_plain_string() -> None:
    """纯文本保持字符串形状：最小惊动，历史用例不变。"""
    out = to_anthropic_request({"model": "m", "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "甲"},
                                     {"type": "input_text", "text": "乙"}]},
    ]})
    assert out["messages"][0]["content"] == "甲乙"


def test_builtin_tools_are_dropped_not_forwarded() -> None:
    """内置工具需要服务端持久化状态，本站是无状态转发——丢弃并说明，不假装支持。"""
    out = to_anthropic_request({"model": "m", "input": "hi",
                                "tools": [{"type": "web_search"}]})
    assert "tools" not in out


def test_to_response_object_exposes_text_and_usage() -> None:
    message = {
        "content": [{"type": "text", "text": "你好"}],
        "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 8},
    }
    obj = to_response_object(message, "m", "resp_x")
    assert obj["id"] == "resp_x"
    assert obj["status"] == "completed"
    assert obj["output_text"] == "你好"
    assert obj["output"][0]["type"] == "message"
    assert obj["output"][0]["content"][0]["type"] == "output_text"
    assert obj["usage"]["input_tokens"] == 10
    assert obj["usage"]["input_tokens_details"]["cached_tokens"] == 8


def test_stream_events_minimal_closed_set() -> None:
    events = [
        ("message_start", {"message": {"usage": {"input_tokens": 5}}}),
        ("content_block_delta", {"delta": {"type": "text_delta", "text": "你"}}),
        ("content_block_delta", {"delta": {"type": "text_delta", "text": "好"}}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 2}}),
    ]
    pairs = list(to_response_events(iter(events), "m"))
    names = [name for name, _ in pairs]
    assert names == ["response.created", "response.output_text.delta",
                     "response.output_text.delta", "response.completed"]
    final = pairs[-1][1]["response"]
    assert final["output_text"] == "你好"
    assert final["usage"]["total_tokens"] == 7


def test_empty_stream_still_completes() -> None:
    """上游一个字都没给也要正常收尾——不能让客户端等到超时。"""
    pairs = list(to_response_events(iter([]), "m"))
    assert [name for name, _ in pairs] == ["response.completed"]
    assert pairs[-1][1]["response"]["output_text"] == ""


def test_output_text_field_matches_output_content() -> None:
    message = {"content": [
        {"type": "text", "text": "a"},
        {"type": "tool_use", "id": "call_1", "name": "t", "input": {"x": 1}},
        {"type": "text", "text": "b"},
    ], "usage": {}}
    obj = to_response_object(message, "m")
    kinds = [item["type"] for item in obj["output"]]
    assert kinds == ["message", "function_call"]
    assert obj["output_text"] == "ab"
    assert json.loads(obj["output"][1]["arguments"]) == {"x": 1}


# ---------------- 活跃路由：responses_to_chat → to_anthropic_request ----------------


def test_responses_to_chat_keeps_input_image_as_image_url_parts() -> None:
    """第一跳（Responses→chat）不许把图吞了。"""
    chat = responses_to_chat({"model": "m", "input": [
        {"role": "user", "content": [
            {"type": "input_text", "text": "这张是什么"},
            {"type": "input_image", "image_url": "https://example.com/a.png"},
        ]},
    ]})
    content = chat["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "这张是什么"}
    assert content[1] == {"type": "image_url",
                          "image_url": {"url": "https://example.com/a.png"}}


def test_responses_to_chat_maps_reasoning_effort() -> None:
    chat = responses_to_chat({"model": "m", "input": "hi",
                              "reasoning": {"effort": "HIGH"}})
    assert chat["reasoning_effort"] == "high"
    # 站点自己广告的词也透传，不查表卡死
    chat2 = responses_to_chat({"model": "m", "input": "hi",
                               "reasoning": {"effort": "max"}})
    assert chat2["reasoning_effort"] == "max"


def test_responses_to_chat_text_only_stays_a_string() -> None:
    chat = responses_to_chat({"model": "m", "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "甲"},
                                     {"type": "input_text", "text": "乙"}]},
    ]})
    assert chat["messages"][0]["content"] == "甲乙"


def test_full_chain_image_and_effort_reach_the_ir() -> None:
    """全链 money test：两跳之后，IR 里必须同时有图片块和档位。"""
    body = {"model": "m", "input": [
        {"role": "user", "content": [
            {"type": "input_text", "text": "看这张"},
            {"type": "input_image", "image_url": "data:image/png;base64,aGk="},
        ]},
    ], "reasoning": {"effort": "high"}}
    ir = chat_to_ir(responses_to_chat(body))
    blocks = ir["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "看这张"}
    assert blocks[1] == {"type": "image",
                         "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}}
    assert ir["reasoning_effort"] == "high"


def test_openai_inbound_reasoning_effort_reaches_the_ir() -> None:
    """/v1/chat/completions 入站：reasoning_effort 不能在第一跳被扔。"""
    ir = chat_to_ir({"model": "m", "messages": [{"role": "user", "content": "hi"}],
                     "reasoning_effort": "Medium"})
    assert ir["reasoning_effort"] == "medium"
