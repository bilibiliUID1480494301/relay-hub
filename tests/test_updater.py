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


def _fake_download(blocked_host: str, files: dict[str, bytes], json_urls: dict[str, dict]):
    """造一个「直链被墙、API 通」的下载器；返回 (函数, 调用过的 URL 列表)。"""
    import json
    import urllib.error

    calls: list[str] = []

    def fake(url: str, dest, timeout: float = 30.0, accept: str | None = None):
        calls.append(url)
        if blocked_host in url and "api." not in url:
            raise urllib.error.URLError("blocked")
        if url in json_urls:
            dest.write_bytes(json.dumps(json_urls[url]).encode("utf-8"))
            return dest
        for name, blob in files.items():
            if url.endswith("/" + name) or url.endswith("/assets/" + name):
                dest.write_bytes(blob)
                return dest
        raise urllib.error.HTTPError(url, 404, "not found", None, None)

    return fake, calls


def test_download_falls_back_to_api_when_github_blocked(tmp_path, monkeypatch):
    """github.com:443 被挡时改走 api.github.com 的资产端点，行为和直连一致。"""
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from relayhub.gateway import signing

    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    wheel_bytes = b"wheel-bytes"
    import hashlib

    digest = hashlib.sha256(wheel_bytes).hexdigest()
    manifest = f"{digest}  hubrelay-0.12.32-py3-none-any.whl\n".encode()
    sig = base64.b64encode(key.sign(manifest))

    files = {
        "SHA256SUMS": manifest,
        "SHA256SUMS.sig": sig,
        "hubrelay-0.12.32-py3-none-any.whl": wheel_bytes,
    }
    api = "https://api.github.com/repos/o/r/releases/tags/v0.12.32"
    fake, calls = _fake_download(
        "github.com", files,
        {api: {"assets": [
            {"name": "SHA256SUMS", "url": "https://api.github.com/assets/SHA256SUMS"},
            {"name": "SHA256SUMS.sig", "url": "https://api.github.com/assets/SHA256SUMS.sig"},
            {"name": "hubrelay-0.12.32-py3-none-any.whl",
             "url": "https://api.github.com/assets/hubrelay-0.12.32-py3-none-any.whl"},
        ]}},
    )
    monkeypatch.setattr(updater, "_download", fake)
    monkeypatch.setattr(updater, "PUBLIC_KEY_B64", pub)

    result = updater.stage("0.12.32", tmp_path / "cache", repo="o/r")
    assert result["ok"] is True and result["verified"] is True, result
    assert result["sha256"] == digest
    assert any("api.github.com/assets" in c for c in calls), "没走到 API 回落"
    # 直连只该试一次：被挡之后还挨个等超时，升级会被拖成一分半
    blocked = [c for c in calls if "github.com/" in c and "api." not in c]
    assert len(blocked) == 1, f"直连被反复重试了 {len(blocked)} 次：{blocked}"


def test_fallback_does_not_weaken_verification(tmp_path, monkeypatch):
    """回落拿回来的字节照样要验签：清单被换掉必须拒绝，不能因为绕路就放行。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import base64

    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    good = b"0000  hubrelay-0.12.32-py3-none-any.whl\n"
    tampered = b"1111  hubrelay-0.12.32-py3-none-any.whl\n"
    sig = base64.b64encode(key.sign(good))  # 签的是 good，API 却给 tampered

    files = {"SHA256SUMS": tampered, "SHA256SUMS.sig": sig,
             "hubrelay-0.12.32-py3-none-any.whl": b"x"}
    api = "https://api.github.com/repos/o/r/releases/tags/v0.12.32"
    fake, _ = _fake_download(
        "github.com", files,
        {api: {"assets": [
            {"name": "SHA256SUMS", "url": "https://api.github.com/assets/SHA256SUMS"},
            {"name": "SHA256SUMS.sig", "url": "https://api.github.com/assets/SHA256SUMS.sig"},
            {"name": "hubrelay-0.12.32-py3-none-any.whl",
             "url": "https://api.github.com/assets/hubrelay-0.12.32-py3-none-any.whl"},
        ]}},
    )
    monkeypatch.setattr(updater, "_download", fake)
    monkeypatch.setattr(updater, "PUBLIC_KEY_B64", pub)

    result = updater.stage("0.12.32", tmp_path / "cache", repo="o/r")
    assert result["ok"] is False and result["verified"] is False
    assert "签名" in result["error"]
