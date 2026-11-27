# -*- coding: utf-8 -*-
"""邮箱验证码：存储语义 + 注册/重置链路（SMTP 用桩替代，不真的发信）。"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from relayhub.gateway import mailer
from relayhub.gateway.service import RelayServer, demo_router
from relayhub.gateway.tokens import TokenStore
from relayhub.gateway.users import UserStore


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{base}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


# ---------------------------------------------------------------- 存储语义


def test_code_is_single_use_and_hashed_on_disk(tmp_path: Path) -> None:
    store = mailer.CodeStore(tmp_path / "codes.json")
    code = store.issue("a@example.com", "register")
    raw = (tmp_path / "codes.json").read_text(encoding="utf-8")
    assert code not in raw, "落盘必须是哈希，不能出现明文码"

    ok, reason = store.verify("a@example.com", "register", code)
    assert ok, reason
    # 重放同一码无效
    ok2, reason2 = store.verify("a@example.com", "register", code)
    assert not ok2 and "不存在" in reason2


def test_wrong_code_counts_attempts_then_burns(tmp_path: Path) -> None:
    store = mailer.CodeStore(tmp_path / "codes.json")
    code = store.issue("b@example.com", "register")
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(mailer.MAX_ATTEMPTS):
        ok, _reason = store.verify("b@example.com", "register", wrong)
        assert not ok
    # 次数用尽后正确码也不放行（记录被清掉，或直接判次数超限）
    ok, reason = store.verify("b@example.com", "register", code)
    assert not ok and ("不存在" in reason or "次数过多" in reason)


def test_resend_is_rate_limited(tmp_path: Path) -> None:
    store = mailer.CodeStore(tmp_path / "codes.json")
    store.issue("c@example.com", "register")
    with pytest.raises(mailer.MailError) as exc:
        store.issue("c@example.com", "register")
    assert "频繁" in str(exc.value)


def test_expired_code_rejected(tmp_path: Path) -> None:
    store = mailer.CodeStore(tmp_path / "codes.json")
    code = store.issue("d@example.com", "register")
    path = tmp_path / "codes.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in data:
        data[key]["exp"] = time.time() - 1
    path.write_text(json.dumps(data), encoding="utf-8")
    ok, reason = store.verify("d@example.com", "register", code)
    assert not ok and "过期" in reason


# ---------------------------------------------------------------- 服务链路


@pytest.fixture
def mail_relay(tmp_path: Path, monkeypatch):
    import relayhub.paths as paths

    original = paths.users_path
    paths.users_path = lambda: tmp_path / "users.json"
    # 邮件配置与验证码也必须落在 tmp：默认走 relayhub_home()，那是真实 HOME
    original_mail = paths.mail_path
    original_codes = paths.verify_codes_path
    paths.mail_path = lambda: tmp_path / "mail.json"
    paths.verify_codes_path = lambda: tmp_path / "email_codes.json"
    store = UserStore(tmp_path / "users.json")
    store.mutate(lambda p: setattr(p, "allow_register", True))

    sent: list[tuple[str, str]] = []

    def fake_send(cfg, to, code, purpose):
        sent.append((to, code))

    monkeypatch.setattr("relayhub.gateway.service.mailer_module.send_code", fake_send)

    server = RelayServer(("127.0.0.1", 0), demo_router(), api_key=None,
                         event_delay=0.0, tokens=TokenStore(tmp_path / "tokens.json"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, tmp_path, sent
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        paths.users_path = original
        paths.mail_path = original_mail
        paths.verify_codes_path = original_codes


def _enable_mail(home: Path) -> None:
    (home / "mail.json").write_text(json.dumps({
        "enabled": True, "host": "localhost", "port": 465,
        "sender": "no-reply@example.com", "tls": "ssl", "user": "", "password": "",
    }), encoding="utf-8")


def test_no_mail_config_returns_503(mail_relay) -> None:
    server, _home, _sent = mail_relay
    status, body = _post(server.base_url, "/api/auth/email/code",
                         {"email": "x@example.com", "purpose": "register"})
    assert status == 503 and "未开启邮件服务" in body["error"]


def test_register_requires_code_once_mail_enabled(mail_relay) -> None:
    server, home, sent = mail_relay
    _enable_mail(home)
    base = server.base_url

    status, body = _post(base, "/api/auth/email/code",
                         {"email": "n@example.com", "purpose": "register"})
    assert status == 200 and body["ok"], body
    assert sent and sent[-1][0] == "n@example.com"
    code = sent[-1][1]

    # 没填码 → 拒
    status, body = _post(base, "/api/auth/register",
                         {"username": "nina", "password": "password8", "email": "n@example.com"})
    assert status == 400 and "验证码" in body["error"]

    # 填错码 → 拒
    status, body = _post(base, "/api/auth/register",
                         {"username": "nina", "password": "password8",
                          "email": "n@example.com", "code": "000000" if code != "000000" else "111111"})
    assert status == 400

    # 正确码 → 成功，且邮箱写进账号
    status, body = _post(base, "/api/auth/register",
                         {"username": "nina", "password": "password8",
                          "email": "n@example.com", "code": code})
    assert status == 200 and body["ok"], body
    users = json.loads((home / "users.json").read_text(encoding="utf-8"))["users"]
    assert any(u.get("email") == "n@example.com" for u in users)


def test_reset_with_email_code(mail_relay) -> None:
    server, home, sent = mail_relay
    base = server.base_url

    # 先建一个带邮箱的账号（邮件未开时注册不需要码）
    status, _ = _post(base, "/api/auth/register", {"username": "olga", "password": "password8"})
    assert status == 200
    uid = next(u["user_id"] for u in json.loads((home / "users.json").read_text(encoding="utf-8"))["users"]
               if u["username"] == "olga")
    store = UserStore(home / "users.json")
    store.mutate(lambda pool: pool.set_email(uid, "olga@example.com"))

    _enable_mail(home)
    status, body = _post(base, "/api/auth/email/code",
                         {"email": "olga@example.com", "purpose": "reset"})
    assert status == 200, body
    code = sent[-1][1]

    status, body = _post(base, "/api/auth/reset",
                         {"email": "olga@example.com", "code": code, "new_password": "brandnew99"})
    assert status == 200 and body["ok"], body

    status, _ = _post(base, "/api/auth/login", {"username": "olga", "password": "password8"})
    assert status == 401
    status, _ = _post(base, "/api/auth/login", {"username": "olga", "password": "brandnew99"})
    assert status == 200


def test_domain_whitelist_gates_registration_not_reset(mail_relay) -> None:
    """白名单只拦注册；找回密码不受限（域名被移除的人也要能登回去）。"""
    server, home, sent = mail_relay
    _enable_mail(home)
    cfg_path = home / "mail.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["allowed_domains"] = ["school.edu.cn"]
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    base = server.base_url
    # 不在白名单 → 拒，且不真发信
    status, body = _post(base, "/api/auth/email/code",
                         {"email": "x@qq.com", "purpose": "register"})
    assert status == 400 and "仅允许" in body["error"]
    assert not sent

    # 子域放行
    status, body = _post(base, "/api/auth/email/code",
                         {"email": "a@cs.school.edu.cn", "purpose": "register"})
    assert status == 200, body
    code = sent[-1][1]

    # 绕过发码直接提交也被拦
    status, body = _post(base, "/api/auth/register",
                         {"username": "peter", "password": "password8",
                          "email": "p@qq.com", "code": code})
    assert status == 400 and "域名" in body["error"]

    status, body = _post(base, "/api/auth/register",
                         {"username": "peter", "password": "password8",
                          "email": "a@cs.school.edu.cn", "code": code})
    assert status == 200 and body["ok"], body

    # 重置用途即使域名不在名单里也允许发码（账号可能早先就有）
    status, body = _post(base, "/api/auth/email/code",
                         {"email": "old@qq.com", "purpose": "reset"})
    assert status == 200, body
