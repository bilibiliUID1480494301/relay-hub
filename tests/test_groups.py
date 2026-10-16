# -*- coding: utf-8 -*-
"""渠道分组（#16）：上游 group / 令牌 channel_group 的语义与全链路过滤。

组语义铁律：组内全灭就明说，绝不跨组自动 failover。
"""
from __future__ import annotations

import uuid

from relayhub.gateway.pool import KeyPool
from relayhub.gateway.router import KeyPoolRouter, ReloadingRouter, RouterError
from relayhub.gateway.tokens import DownstreamToken

from support import make_key


def test_key_group_default_and_roundtrip() -> None:
    key = make_key("a", "http://a")
    assert key.group == "default"
    restored = UpstreamKey_from_dict(key.to_dict())
    assert restored.group == "default"
    # 旧号池文件没有 group 键 → 迁移到 default，别让脏数据卡加载
    legacy = dict(key.to_dict())
    legacy.pop("group")
    assert UpstreamKey_from_dict(legacy).group == "default"


def UpstreamKey_from_dict(raw: dict) -> object:
    from relayhub.gateway.pool import UpstreamKey

    return UpstreamKey.from_dict(raw)


def test_candidates_group_isolation() -> None:
    free = make_key("free-1", "http://free", group="free")
    paid = make_key("paid-1", "http://paid", group="paid")
    pool = KeyPool([free, paid])

    assert {k.group for k in pool.candidates("glm-5.2")} == {"free", "paid"}
    assert [k.group for k in pool.candidates("glm-5.2", group="free")] == ["free"]
    assert [k.group for k in pool.candidates("glm-5.2", group="paid")] == ["paid"]
    # 不存在的组 → 空列表（路由层翻译成 503），不悄悄落回别的组
    assert pool.candidates("glm-5.2", group="nope") == []
    # 组内全灭（禁用）→ 空，不跨组
    pool.set_enabled("free-1", False)
    assert pool.candidates("glm-5.2", group="free") == []
    assert {k.group for k in pool.candidates("glm-5.2")} == {"paid"}


def test_groups_and_all_models() -> None:
    free = make_key("free-1", "http://free", group="free")
    paid = make_key("paid-1", "http://paid", group="paid")
    paid.models = ("glm-5.2", "glm-5.3")
    paid.model_windows = {"glm-5.2": 1, "glm-5.3": 2}
    pool = KeyPool([free, paid])
    assert pool.groups() == ["free", "paid"]
    assert set(pool.all_models()) == {"glm-5.2", "glm-5.3"}
    assert pool.all_models(group="paid") == ["glm-5.2", "glm-5.3"]
    assert pool.all_models(group="free") == ["glm-5.2"]
    assert pool.all_models(group="nope") == []


def test_token_channel_group_roundtrip() -> None:
    tok = DownstreamToken(
        token_id=str(uuid.uuid4()), name="t", token="rh_x", channel_group="free"
    )
    assert tok.group == "default"  # 用户分组标签不受影响
    restored = DownstreamToken.from_dict(tok.to_dict())
    assert restored.channel_group == "free"
    legacy = dict(tok.to_dict())
    legacy.pop("channel_group")
    assert DownstreamToken.from_dict(legacy).channel_group == ""


def test_router_relay_passes_group_to_pool() -> None:
    pool = KeyPool([make_key("a", "http://127.0.0.1:1", group="free")])
    router = KeyPoolRouter(pool)
    seen: list = []
    orig = pool.candidates

    def spy(model: str, strategy=None, group=None):
        seen.append(group)
        return orig(model, strategy, group)

    pool.candidates = spy  # type: ignore[method-assign]
    try:
        router.relay("glm-5.2", {}, False, group="free")
    except RouterError:
        pass  # 上游连不上没关系；我们只验证分组参数被透传
    assert seen == ["free"]


def test_reloading_router_forwards_group(tmp_path) -> None:
    """D4 回归：ReloadingRouter 必须把 group 透传给内层 KeyPoolRouter。

    这条曾经是坏的：包装类签名漏了 group，service.py 的 `except TypeError`
    降级链于是在生产上静默触发——**不只**丢掉 group（渠道组隔离失效），
    还连带丢掉 trace_headers（链路追踪）和 fp（语义指纹/缓存键），
    而请求看起来完全正常。所以这里连 trace_headers/fp 一起钉死。
    """
    path = tmp_path / "pool.json"
    pool = KeyPool([make_key("free-1", "http://free", group="free")])
    pool.save(path)
    router = ReloadingRouter(path)

    assert set(router.models()) == {"glm-5.2"}
    assert set(router.models(group="free")) == {"glm-5.2"}
    assert router.models(group="paid") == {}

    # relay 的参数必须原样到达内层（spy 拦 pool.candidates 拿得到 group）
    inner = router._current()
    seen: list = []
    orig = inner.pool.candidates

    def spy(model: str, strategy=None, group=None):
        seen.append(group)
        return orig(model, strategy, group)

    inner.pool.candidates = spy  # type: ignore[method-assign]
    try:
        router.relay(
            "glm-5.2", {}, False,
            trace_headers={"Via": "relayhub-x", "X-Relay-Hub-Hops": "1"},
            fp="fp-1",
            group="free",
        )
    except RouterError:
        pass  # 上游连不上无所谓，只看参数是否透传
    assert seen == ["free"], f"group 未透传到内层：{seen!r}"
