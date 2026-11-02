"""`relayhub.gateway users` CLI：初始站长密码 / 强制重置 / 列用户。

覆盖用户点名的三件事：
  * 初始站长密码随机生成、只打印一次（不落盘明文）；
  * 密码 >=8 位（弱密码拒绝）；
  * 服务端可用指令强制重置密码（面板进不去也能救）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from relayhub.gateway import useradmin
from relayhub.gateway.users import UserPool, verify_password


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RELAYHUB_HOME", str(tmp_path))
    return tmp_path


def _users(home: Path) -> dict:
    return json.loads((home / "users.json").read_text(encoding="utf-8"))


def _printed_password(out: str) -> str | None:
    for line in out.splitlines():
        if "密码" in line and "：" in line:
            return line.split("：", 1)[1].strip()
    return None


def test_init_creates_admin_with_random_password(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert useradmin.main(["init"]) == 0
    out = capsys.readouterr().out

    users = _users(home)["users"]
    assert len(users) == 1
    admin = users[0]
    assert admin["role"] == "admin"
    assert admin["enabled"] is True
    assert admin["quota"] == -1, "站长应不限量"
    assert admin["password_hash"].startswith("pbkdf2$")

    pw = _printed_password(out)
    assert pw is not None, out
    assert len(pw) >= 8, "初始密码至少 8 位"
    assert verify_password(pw, admin["password_hash"]), "打印的密码必须能登录"


def test_init_refuses_when_admin_exists(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert useradmin.main(["init"]) == 0
    capsys.readouterr()
    assert useradmin.main(["init"]) == 1
    assert "已存在管理员" in capsys.readouterr().err


def test_reset_password_rejects_short(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    useradmin.main(["init"])
    capsys.readouterr()
    assert useradmin.main(["reset-password", "--password", "123"]) == 2
    assert "至少 8 位" in capsys.readouterr().err


def test_reset_password_sets_given_password(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    useradmin.main(["init"])
    capsys.readouterr()
    assert useradmin.main(["reset-password", "--password", "Str0ngPass!"]) == 0
    admin = _users(home)["users"][0]
    assert verify_password("Str0ngPass!", admin["password_hash"])
    assert not verify_password("wrong-pass", admin["password_hash"])


def test_reset_password_random_when_omitted(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    useradmin.main(["init"])
    capsys.readouterr()
    assert useradmin.main(["reset-password"]) == 0
    pw = _printed_password(capsys.readouterr().out)
    assert pw and len(pw) >= 8
    assert verify_password(pw, _users(home)["users"][0]["password_hash"])


def test_reset_unknown_user_fails(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert useradmin.main(["reset-password", "--username", "ghost"]) == 1
    assert "找不到用户" in capsys.readouterr().err
