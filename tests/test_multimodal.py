"""多模态与联网搜索的方言互译测试（upstream 的图片/搜索透传）。

多模态链路：App 发带图消息 → 网关翻成上游方言 → 上游识图/搜索。
任何一侧把图片块弄丢或翻错，表现出来都是「模型看不到图」这类静默错误，
所以这里全部做结构级断言。
"""

from __future__ import annotations

import pytest

from relayhub.gateway import upstream

ANTHROPIC_IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}}
OPENAI_IMAGE_URL = {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="}}


# ---------------------------------------------------------------- Anthropic → OpenAI


def test_anthropic_image_block_becomes_openai_image_url() -> None:
    out = upstream.to_openai_request(
        {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": [{"type": "text", "text": "看图回答"}, ANTHROPIC_IMAGE]}]}
    )
    (msg,) = out["messages"]
    assert isinstance(msg["content"], list)
    kinds = [p["type"] for p in msg["content"]]
    assert kinds == ["text", "image_url"]
    assert msg["content"][1]["image_url"]["url"] == "data:image/png;base64,aGk="


def test_anthropic_url_image_source_passes_through_as_url() -> None:
    out = upstream.to_openai_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}
                    ],
                }
            ],
        }
    )
    (msg,) = out["messages"]
    assert msg["content"][0]["image_url"]["url"] == "https://x/y.png"


def test_anthropic_web_search_tool_maps_to_openai_options() -> None:
    out = upstream.to_openai_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "查一下"}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        }
    )
    # 服务端工具不翻成假 function，映射成原生搜索开关
    assert "tools" not in out
    assert out["web_search_options"] == {}


# ---------------------------------------------------------------- OpenAI → Anthropic


def test_openai_data_url_image_becomes_base64_block() -> None:
    out = upstream.to_anthropic_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "看图"}, OPENAI_IMAGE_URL]}],
        }
    )
    (msg,) = out["messages"]
    assert isinstance(msg["content"], list)
    assert msg["content"][0] == {"type": "text", "text": "看图"}
    assert msg["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": "aGk="}


def test_openai_http_image_becomes_url_source() -> None:
    out = upstream.to_anthropic_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://x/y.jpg"}}
                    ],
                }
            ],
        }
    )
    (msg,) = out["messages"]
    assert isinstance(msg["content"], list)
    assert msg["content"][0] == {"type": "text", "text": ""}  # 自动补前导空文本
    assert msg["content"][1]["source"] == {"type": "url", "url": "https://x/y.jpg"}


def test_openai_plain_text_user_message_stays_string() -> None:
    out = upstream.to_anthropic_request(
        {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": "纯文本"}]}
    )
    assert out["messages"][0]["content"] == "纯文本"


def test_openai_web_search_options_maps_to_server_tool() -> None:
    out = upstream.to_anthropic_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "查一下"}],
            "web_search_options": {"max_uses": 2},
        }
    )
    tools = out["tools"]
    assert tools[-1]["type"].startswith("web_search")
    assert tools[-1]["max_uses"] == 2


def test_openai_audio_still_rejected() -> None:
    with pytest.raises(upstream.UpstreamError):
        upstream.to_anthropic_request(
            {
                "model": "m",
                "max_tokens": 8,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "input_audio", "input_audio": {"data": "x", "format": "wav"}}],
                    }
                ],
            }
        )


# ---------------------------------------------------------------- tool_result 图片


def test_tool_result_image_survives_anthropic_to_openai() -> None:
    """截图类工具的结果图：tool_result content 带图时翻成块数组，不再被摊平。"""
    out = upstream.to_openai_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": [{"type": "text", "text": "截图："}, ANTHROPIC_IMAGE],
                        }
                    ],
                }
            ],
        }
    )
    (tool,) = out["messages"]
    assert tool["role"] == "tool"
    assert tool["tool_call_id"] == "tu_1"
    assert isinstance(tool["content"], list)
    kinds = [p["type"] for p in tool["content"]]
    assert kinds == ["text", "image_url"]
    assert tool["content"][1]["image_url"]["url"] == "data:image/png;base64,aGk="


def test_tool_result_text_only_stays_a_plain_string() -> None:
    """没图的 tool_result 保持字符串——绝大多数上游只认这个形状。"""
    out = upstream.to_openai_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": [{"type": "text", "text": "晴 25℃"}],
                        }
                    ],
                }
            ],
        }
    )
    (tool,) = out["messages"]
    assert tool["content"] == "晴 25℃"


def test_openai_tool_message_image_url_becomes_anthropic_block() -> None:
    """反向：OpenAI 入站的 tool 消息带 image_url → Anthropic tool_result 图片块。"""
    out = upstream.to_anthropic_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": [{"type": "text", "text": "截图："}, OPENAI_IMAGE_URL],
                }
            ],
        }
    )
    (msg,) = out["messages"]
    (tr,) = msg["content"]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "call_1"
    assert tr["content"][0] == {"type": "text", "text": "截图："}
    assert tr["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": "aGk="}


def test_unknown_server_tool_passes_through_instead_of_vanishing() -> None:
    """上游自定义的服务端工具开关（如 deepseek_web_search）不能静默丢掉。

    丢掉的话客户端以为开了联网搜索，模型只是"没搜"，没有任何报错可查。
    """
    out = upstream.to_openai_request(
        {
            "model": "deepseek-flash",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "今天有什么新闻"}],
            "tools": [
                {"type": "deepseek_web_search"},
                {"type": "deepseek_read_url"},
                {"name": "get_time", "input_schema": {"type": "object", "properties": {}}},
            ],
        }
    )
    types = [t.get("type") for t in out["tools"]]
    assert "deepseek_web_search" in types, f"未知工具被吃掉了：{types}"
    assert "deepseek_read_url" in types
    assert "function" in types, "普通 function 工具不该受影响"


def test_anthropic_native_server_tool_still_maps_to_options() -> None:
    """Anthropic 自家的 web_search 仍走 web_search_options，不能被当成未知工具透传。"""
    out = upstream.to_openai_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        }
    )
    assert out.get("web_search_options") == {}
    assert "tools" not in out or not out["tools"]


def test_openai_side_server_tool_not_faked_as_function() -> None:
    """OpenAI 侧的非 function 条目不能变成 name=null 的假工具。"""
    out = upstream.to_anthropic_request(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "deepseek_web_search"}],
        }
    )
    tools = out.get("tools") or []
    assert tools and tools[0].get("type") == "deepseek_web_search"
    assert "name" not in tools[0]


def test_tool_usage_counts_billable_not_display_fields() -> None:
    """计费次数取 tool_search/tool_fetch，不能取 web_search_requests（那是结果块数）。"""
    usage = {
        "input_tokens": 10,
        "output_tokens": 20,
        "server_tool_use": {
            "web_search_requests": 1,
            "web_fetch_requests": 4,
            "tool_search": 3,
            "tool_fetch": 0,
            "tool_image_search": 1,
        },
    }
    assert upstream.tool_usage_from_usage(usage) == (3, 0, 1)


def test_tool_usage_absent_is_zero() -> None:
    assert upstream.tool_usage_from_usage({}) == (0, 0, 0)
    assert upstream.tool_usage_from_usage({"server_tool_use": None}) == (0, 0, 0)
    assert upstream.tool_usage_from_usage(None) == (0, 0, 0)
