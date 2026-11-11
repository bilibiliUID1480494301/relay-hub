"""服务器状态：缺 psutil 必须降级而不是报错，且绝不能带出凭据。"""

from __future__ import annotations

import sys
from pathlib import Path

from relayhub.gateway import serverstat


def test_status_has_core_fields(tmp_path: Path):
    data = serverstat.status(tmp_path, started_at=0)
    for key in ("pid", "python", "platform", "uptime_s", "threads", "memory", "disk", "data"):
        assert key in data
    assert data["data"]["home"] == str(tmp_path)
    assert data["disk"]["total_gb"] is not None or data["disk"]["total_gb"] is None


def test_memory_degrades_without_psutil(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)   # 令 import psutil 抛 ImportError
    memory = serverstat._memory()
    assert memory["source"] == "none"
    assert memory["rss_mb"] is None


def test_no_credentials_leak_into_status(tmp_path: Path):
    blob = str(serverstat.status(tmp_path, started_at=0)).lower()
    for secret in ("api_key", "sk-", "password", "token_hash", "secret"):
        assert secret not in blob


def test_admin_panel_has_server_and_migrate_tabs():
    """面板新页签必须有入口、有容器、有加载函数——三者缺一就是半成品。"""
    from relayhub.gateway import admin as admin_mod

    html = admin_mod.PAGE_HTML
    for needle in ('data-tab="server"', 'data-tab="migrate"',
                   'id="pane-server"', 'id="pane-migrate"',
                   "function loadServer", "function mgRun"):
        assert needle in html, needle
