"""一键升级的关键行为：没签名不装、哈希不符不装、安装命令不带副作用。"""

from __future__ import annotations

import pytest

from relayhub.gateway import signing, updater


def test_pick_wheel_finds_version():
    manifest = f"{'a'*64}  hubrelay-0.11.1-py3-none-any.whl\n{'b'*64}  hubrelay-0.11.1.tar.gz\n"
    name, digest = updater.pick_wheel(manifest, "0.11.1")
    assert name == "hubrelay-0.11.1-py3-none-any.whl"
    assert digest == "a" * 64


def test_pick_wheel_missing_version_raises():
    with pytest.raises(signing.VerifyError):
        updater.pick_wheel("deadbeef  other.whl\n", "9.9.9")


def test_install_argv_is_isolated():
    """安装不许带依赖、不许交互——升级顺手改依赖是另一个量级的事故。"""
    argv = updater.install_argv("python", __file__)
    assert "--no-deps" in argv and "--force-reinstall" in argv and "--no-input" in argv
    assert "-U" not in argv


def test_force_allows_install_but_flags_unverified(tmp_path):
    """强制跳过验签：允许装，但必须把 verified=False 传出去（不能静默放过）。"""
    from relayhub.gateway import signing

    wheel = tmp_path / "w.whl"
    wheel.write_bytes(b"tampered")
    ok, verified, error = updater.verify_or_force(wheel, "0" * 64 + "  w.whl\n", "", "badkey", force=True)
    assert ok is True and verified is False and error


def test_without_force_verification_failure_blocks(tmp_path):
    wheel = tmp_path / "w.whl"
    wheel.write_bytes(b"tampered")
    ok, verified, _ = updater.verify_or_force(wheel, "0" * 64 + "  w.whl\n", "", "badkey", force=False)
    assert ok is False and verified is False


def test_history_records_and_previous(tmp_path):
    from pathlib import Path

    a = tmp_path / "hubrelay-0.11.1-py3-none-any.whl"
    a.write_bytes(b"v1")
    updater.record_install(tmp_path / "store", "0.11.1", a, "h1")
    assert updater.previous_install(tmp_path / "store") is None      # 只有一版，无回滚目标

    b = tmp_path / "hubrelay-0.12.0-py3-none-any.whl"
    b.write_bytes(b"v2")
    updater.record_install(tmp_path / "store", "0.12.0", b, "h2")
    prev = updater.previous_install(tmp_path / "store")
    assert prev["version"] == "0.11.1" and Path(prev["wheel"]).is_file()


def test_restart_flag_is_written_not_selfkill(tmp_path):
    flag = updater.request_restart(tmp_path / "restart.flag", "0.12.0")
    assert flag.is_file() and "0.12.0" in flag.read_text(encoding="utf-8")
