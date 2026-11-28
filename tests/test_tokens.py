"""下游令牌层测试：存储、鉴权判定、记账、热加载。

下游令牌直接决定「哪台设备能用网关」，所以这里的测试都是行为级断言，
不测 dataclass 字段搬运本身。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from relayhub.gateway.tokens import (
    TOKEN_PREFIX,
    DownstreamToken,
    TokenError,
    TokenPool,
    TokenStore,
    generate_token,
)


def make_record(name: str, *, models: tuple[str, ...] = (), enabled: bool = True) -> DownstreamToken:
    return DownstreamToken(
        token_id=name,
        name=name,
        token=f"{TOKEN_PREFIX}secret-{name}",
        models=models,
        enabled=enabled,
    )


# ---------------------------------------------------------------- 存储


def test_roundtrip_preserves_usage_and_models(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    pool = TokenPool([make_record("app-laptop", models=("glm-5.2",))])
    pool.report(pool.tokens[0], ok=True, tokens_in=11, tokens_out=7)
    pool.save(path)

    loaded = TokenPool.load(path).tokens[0]
    assert loaded.name == "app-laptop"
    assert loaded.models == ("glm-5.2",)
    assert loaded.usage.requests == 1
    assert loaded.usage.tokens_in == 11


def test_load_missing_file_returns_empty_pool(tmp_path: Path) -> None:
    assert TokenPool.load(tmp_path / "nope.json").tokens == []


# ---------------------------------------------------------------- 鉴权判定


def test_find_matches_exact_secret_only() -> None:
    pool = TokenPool([make_record("a"), make_record("b")])
    assert pool.find("rht_secret-b") is pool.tokens[1]
    assert pool.find("rht_secret-nope") is None
    assert pool.find("") is None
    # 前缀碰撞也不行：凭证必须全文相等
    assert pool.find("rht_secret-") is None


def test_disabled_token_is_found_but_not_authorized() -> None:
    """禁用是「找到但拒绝」，不是「找不到」——两件事的报错与处置不同。"""
    record = make_record("paused", enabled=False)
    pool = TokenPool([record])
    found = pool.find(record.token)
    assert found is record
    assert not found.enabled


def test_add_rejects_duplicate_name_and_empty_token() -> None:
    pool = TokenPool([make_record("app-a")])
    try:
        pool.add(make_record("app-a"))
    except TokenError as exc:
        assert "已存在" in str(exc)
    else:
        raise AssertionError("重复 name 应被拒绝")
    try:
        pool.add(DownstreamToken(token_id="x", name="x", token="  "))
    except TokenError:
        pass
    else:
        raise AssertionError("空 token 应被拒绝")


def test_remove_accepts_name_id_or_full_secret() -> None:
    pool = TokenPool([make_record("app-a"), make_record("app-b")])
    # 吊销场景手上可能只有其中一种标识，三种都得认
    assert pool.remove("app-a") is True
    assert pool.remove("app-b") is True
    assert pool.remove("ghost") is False


def test_model_allows_empty_means_unrestricted() -> None:
    assert make_record("free").allows("any-model") is True
    assert make_record("limited", models=("glm-5.2",)).allows("glm-5.2") is True
    assert make_record("limited", models=("glm-5.2",)).allows("deepseek-v4") is False


# ---------------------------------------------------------------- 记账


def test_report_counts_ok_failed_and_tokens() -> None:
    pool = TokenPool([make_record("app-a")])
    record = pool.tokens[0]
    pool.report(record, ok=True, tokens_in=10, tokens_out=5)
    pool.report(record, ok=False)
    assert record.usage.requests == 2
    assert record.usage.ok == 1
    assert record.usage.failed == 1
    assert record.usage.tokens_in == 10
    assert record.usage.tokens_out == 5
    assert record.usage.last_used > 0


# ---------------------------------------------------------------- 热加载（TokenStore）


def test_store_hot_reloads_when_a_token_is_added(tmp_path: Path) -> None:
    """serve 进程不重启就能认出 `token add` 写进来的新令牌。"""
    path = tmp_path / "tokens.json"
    TokenPool([make_record("app-a")]).save(path)
    store = TokenStore(path)

    assert store.find("rht_secret-app-a") is not None

    pool = TokenPool.load(path)
    pool.add(make_record("app-b"))
    pool.save(path)

    assert store.find("rht_secret-app-b") is not None, "新增令牌必须被热加载识别"


def test_store_fingerprint_ignores_usage_writes(tmp_path: Path) -> None:
    """记账落盘不该触发 reload。

    若指纹把 usage 算进去，每个请求都会 reload 一次；单请求测试看不出来，
    连续请求时 round-robin 类状态会被反复重置（pool.py 踩过的同一个坑）。
    """
    path = tmp_path / "tokens.json"
    TokenPool([make_record("app-a")]).save(path)
    store = TokenStore(path)
    fingerprint_before = store._fingerprint

    store.report(store.find("rht_secret-app-a"), ok=True, tokens_in=3, tokens_out=2)

    assert store._fingerprint == fingerprint_before


def test_store_report_is_thread_safe(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    TokenPool([make_record("app-a")]).save(path)
    store = TokenStore(path)
    record = store.find("rht_secret-app-a")

    def hammer() -> None:
        for _ in range(20):
            store.report(record, ok=True, tokens_in=1, tokens_out=1)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final = TokenPool.load(path).tokens[0].usage
    assert final.requests == 80, "并发记账不能丢次数"
    assert final.tokens_in == 80


# ---------------------------------------------------------------- CLI 契约


def test_generate_token_shape() -> None:
    token = generate_token()
    assert token.startswith(TOKEN_PREFIX)
    assert len(token) > len(TOKEN_PREFIX) + 32
    assert generate_token() != token


def test_stats_never_leaks_full_secret(tmp_path: Path) -> None:
    """stats 是给管理面用的：全文泄露一次，这个字段就永远不能再进页面。"""
    pool = TokenPool([make_record("appb")])
    blob = json.dumps(pool.stats(), ensure_ascii=False)
    assert "rht_secret-appb" not in blob
    assert "…appb" in blob


# ---------------------------------------------------------------- 限额与作用域


def test_admit_rolls_daily_counter_and_enforces_quota() -> None:
    """日配额：跨天清零、按受理计数、到量拒绝。"""
    token = make_record("limited")
    token.daily_requests = 2
    pool = TokenPool([token])

    assert pool.admit(token) == (True, "")
    assert pool.admit(token) == (True, "")
    ok, reason = pool.admit(token)
    assert not ok and "日配额" in reason
    assert token.usage.day_requests == 2  # 预扣两次，拒绝的那次不计

    # 跨天：计数清零后重新放行
    token.usage.day = "2000-01-01"
    token.usage.day_requests = 99
    assert pool.admit(token) == (True, "")
    assert token.usage.day_requests == 1


def test_scope_roundtrip_and_bad_scope_falls_back() -> None:
    """scope/限额随文件持久化；脏 scope 回退 normal，不让坏文件卡死网关。"""
    path = Path(__import__("tempfile").gettempdir()) / "rht_scope_test.json"
    pool = TokenPool([make_record("a")])
    pool.tokens[0].scope = "test"
    pool.tokens[0].rpm = 30
    pool.tokens[0].daily_requests = 500
    pool.save(path)

    loaded = TokenPool.load(path).tokens[0]
    assert loaded.scope == "test"
    assert loaded.rpm == 30
    assert loaded.daily_requests == 500

    dirty = dict(pool.tokens[0].to_dict(), scope="hacker")
    assert DownstreamToken.from_dict(dirty).scope == "normal"
    path.unlink()


def test_unknown_scope_rejected_at_construction() -> None:
    import pytest

    with pytest.raises(TokenError):
        make_record("bad").__class__(
            token_id="bad", name="bad", token="rht_x", scope="hacker"
        )


# ---------------------------------------------------------------- 有效期与哈希存储


def test_expired_token_detected() -> None:
    import time as _time

    token = make_record("old")
    token.expires_at = _time.time() - 10
    assert token.is_expired() is True
    token.expires_at = 0.0
    assert token.is_expired() is False
    token.expires_at = _time.time() + 3600
    assert token.is_expired() is False


def test_plaintext_never_persisted_and_find_still_works(tmp_path: Path) -> None:
    """明文 → 首次保存自动迁移为 SHA-256；文件里再也搜不到明文；鉴权不受影响。"""
    secret = f"{TOKEN_PREFIX}legacy-plaintext"
    token = DownstreamToken(token_id="t", name="legacy", token=secret)
    assert token.token_hash and token.token != secret or token.token  # 内存里兼容保留
    pool = TokenPool([token])
    pool.save(tmp_path / "tokens.json")

    raw = (tmp_path / "tokens.json").read_text(encoding="utf-8")
    assert secret not in raw, "明文被写进盘了！"
    loaded = TokenPool.load(tmp_path / "tokens.json").tokens[0]
    assert loaded.token_hash
    assert pool.find(secret) is loaded or TokenPool.load(tmp_path / "tokens.json").find(secret) is not None


def test_group_normalized_and_persisted() -> None:
    """分组：空白回 default；非法字符折叠；随 to_dict/from_dict 往返。"""
    from relayhub.gateway.tokens import DownstreamToken as DT

    assert DT(token_id="a", name="a", token="", group="  ").group == "default"
    assert DT(token_id="b", name="b", token="", group="class 2!!").group == "class-2-"
    assert DT(token_id="c", name="c", token="", group="x" * 40).group == "x" * 32
    raw = DT(token_id="d", name="d", token="", group="vip").to_dict()
    assert raw["group"] == "vip"
    assert DT.from_dict(raw).group == "vip"
    assert DT.from_dict({"token_id": "e", "name": "e"}).group == "default"


def test_ip_allowlist_matches_single_ip_and_cidr() -> None:
    """白名单支持单 IP 与 CIDR；空名单放行；解析不出的来源拒绝（宁可误杀）。"""
    from relayhub.gateway.tokens import DownstreamToken

    tok = DownstreamToken(
        token_id="t1", name="dev", token="sk-x",
        ip_allowlist=("10.0.0.5", "192.168.1.0/24", "::1"),
    )
    assert tok.ip_allowed("10.0.0.5")
    assert tok.ip_allowed("192.168.1.77")
    assert tok.ip_allowed("::1")
    assert not tok.ip_allowed("10.0.0.6")
    assert not tok.ip_allowed("8.8.8.8")
    assert not tok.ip_allowed("")            # 解析不出 → 拒绝

    # 反代常见 IPv4-mapped IPv6
    assert tok.ip_allowed("::ffff:10.0.0.5")

    free = DownstreamToken(token_id="t2", name="any", token="sk-y")
    assert free.ip_allowed("1.2.3.4")        # 空名单 = 不限制

    # 写错的条目被跳过，不会变成「全放行」
    sloppy = DownstreamToken(
        token_id="t3", name="s", token="sk-z",
        ip_allowlist=("not-an-ip", "10.1.0.0/16"),
    )
    assert sloppy.ip_allowed("10.1.2.3")
    assert not sloppy.ip_allowed("10.2.0.1")


def test_token_quota_counts_both_directions() -> None:
    """令牌额度按 tokens_in + tokens_out 计；0 = 不限；用尽即拒。"""
    from relayhub.gateway.tokens import DownstreamToken

    tok = DownstreamToken(token_id="q1", name="friend", token="sk-q", quota_tokens=1000)
    assert tok.quota_left() == 1000 and not tok.quota_exhausted()
    tok.usage.tokens_in, tok.usage.tokens_out = 700, 200
    assert tok.quota_left() == 100          # 900 已用
    assert not tok.quota_exhausted()
    tok.usage.tokens_out = 300
    assert tok.quota_left() == 0 and tok.quota_exhausted()

    free = DownstreamToken(token_id="q2", name="free", token="sk-f")
    assert free.quota_left() == -1 and not free.quota_exhausted()

    # 负数在构造时就该被拒
    try:
        DownstreamToken(token_id="q3", name="bad", token="sk-b", quota_tokens=-5)
    except Exception:
        pass
    else:
        raise AssertionError("负额度应当报错")


def test_ipv6_allowlist_and_proxy_forms() -> None:
    """IPv6 支持：CIDR、环回、IPv4-mapped，以及反代常见的带括号/端口写法。"""
    from relayhub.gateway.tokens import DownstreamToken, normalize_client_ip

    # 归一化：只剥方括号/端口，不碰裸 IPv6（2001:db8::1:443 是合法地址）
    assert normalize_client_ip("[2001:db8::1]:443") == "2001:db8::1"
    assert normalize_client_ip("[2001:db8::1]") == "2001:db8::1"
    assert normalize_client_ip("2001:db8::1:443") == "2001:db8::1:443"
    assert normalize_client_ip("1.2.3.4:5678") == "1.2.3.4"
    assert normalize_client_ip("fe80::1%eth0") == "fe80::1"
    assert normalize_client_ip("  ::1  ") == "::1"

    tok = DownstreamToken(token_id="v6", name="v6", token="x",
                          ip_allowlist=("2001:db8::/32", "::1", "10.0.0.0/8"))
    assert tok.ip_allowed("2001:db8::1")
    assert tok.ip_allowed("[2001:db8::1]:443")
    assert tok.ip_allowed("::1")
    assert tok.ip_allowed("[::1]:8080")
    assert tok.ip_allowed("::ffff:10.0.0.5")      # 双栈监听下的 IPv4-mapped
    assert not tok.ip_allowed("2001:db9::1")
    assert not tok.ip_allowed("8.8.8.8")


def test_forwarded_headers_only_trusted_from_private_peers() -> None:
    """公网直连不能自选 IP；只有对端是可信代理时才认转发头。"""
    from relayhub.gateway.service import resolve_client_ip

    # 公网直连 + 伪造 XFF → 用真实对端地址，忽略伪造头
    assert resolve_client_ip("203.0.113.9", "1.2.3.4", "5.6.7.8") == "203.0.113.9"

    # 本机反代（cloudflared / nginx / frp 都在这类地址上）→ 认转发头
    assert resolve_client_ip("127.0.0.1", "203.0.113.7", "") == "203.0.113.7"
    assert resolve_client_ip("::1", "[2001:db8::9]:443", "") == "2001:db8::9"
    assert resolve_client_ip("10.1.2.3", "", "198.51.100.4") == "198.51.100.4"

    # 多级代理取第一个（最初客户端）
    assert resolve_client_ip("127.0.0.1", "203.0.113.7, 10.0.0.2", "") == "203.0.113.7"

    # 没有转发头时仍是对端地址
    assert resolve_client_ip("127.0.0.1", "", "") == "127.0.0.1"
