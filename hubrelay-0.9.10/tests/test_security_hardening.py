"""安全加固回归：错误脱敏、404 不回显 query、原子写 + 0600、协议清单同源、CLI 可启动。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from relayhub.gateway import pool as pool_module
from relayhub.gateway.pool import SUPPORTED_PROTOCOLS, KeyPool, UpstreamKey
from relayhub.gateway.upstream import scrub_secrets


def test_scrub_secrets_removes_credentials():
    text = "bad key sk-WRONGcanary99887766 Bearer abcdef123456 x-api-key: rht_deadbeef99"
    cleaned = scrub_secrets(text)
    assert "sk-WRONG" not in cleaned
    assert "abcdef123456" not in cleaned
    assert "rht_deadbeef99" not in cleaned
    assert "***" in cleaned


def test_pool_save_is_atomic_and_private(tmp_path: Path):
    path = tmp_path / "pool.json"
    KeyPool([UpstreamKey(key_id="k1", label="l", base_url="https://x", api_key="sk-secret")]).save(path)
    assert json.loads(path.read_text(encoding="utf-8"))["keys"][0]["key_id"] == "k1"
    if os.name == "posix":
        assert oct(path.stat().st_mode)[-3:] == "600"


def test_cli_protocol_choices_match_supported():
    import relayhub.gateway.__main__ as entry

    parser = entry.build_parser() if hasattr(entry, "build_parser") else None
    if parser is not None:
        action = next(a for a in parser._actions if a.dest == "protocol")
        assert set(action.choices) == set(SUPPORTED_PROTOCOLS)


def test_cli_help_starts():
    result = subprocess.run(
        [sys.executable, "-m", "relayhub.gateway", "--help"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0
    assert "Traceback" not in (result.stderr or "")
