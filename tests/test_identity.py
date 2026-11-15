"""站点客户端识别策略：只为识别自家 App，不构成对下游用户的窥探。"""

from __future__ import annotations

from pathlib import Path

from relayhub.gateway import identity


def test_defaults_when_file_missing(tmp_path: Path):
    p = identity.load(tmp_path / "identity.json")
    assert p["enabled"] is True
    assert p["headers"]["plugin"] == ["X-Client-Plugin"]
    assert p["unidentified"] == "direct"


def test_bad_header_names_are_dropped(tmp_path: Path):
    path = tmp_path / "identity.json"
    path.write_text('{"headers": {"plugin": ["X Bad Name", "X-Plugin"]}}', encoding="utf-8")
    p = identity.load(path)
    assert p["headers"]["plugin"] == ["X-Plugin"]     # 含空格的头名被丢掉
    assert p["headers"]["user"] == ["X-Client-User"]  # 未配置的沿用默认


def test_extract_only_configured_headers(tmp_path: Path):
    path = tmp_path / "identity.json"
    path.write_text('{"headers": {"plugin": ["X-Client-Plugin"]}}', encoding="utf-8")
    policy = identity.load(path)
    got = identity.extract(policy, {"x-client-plugin": "deepseekharness", "x-other": "1"})
    assert got == {"plugin": "deepseekharness"}


def test_disabled_policy_returns_nothing():
    policy = {"enabled": False, "headers": {"plugin": ["X-Client-Plugin"]}}
    assert identity.extract(policy, {"X-Client-Plugin": "x"}) == {}


def test_no_identity_headers_means_empty_not_fake(tmp_path: Path):
    policy = identity.load(tmp_path / "nope.json")
    assert identity.extract(policy, {}) == {}         # 直连就是直连，不编名字


def test_bad_unidentified_mode_falls_back(tmp_path: Path):
    path = tmp_path / "identity.json"
    path.write_text('{"unidentified": "whatever"}', encoding="utf-8")
    assert identity.load(path)["unidentified"] == "direct"
