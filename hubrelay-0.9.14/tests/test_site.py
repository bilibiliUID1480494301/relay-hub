"""站点个性化命名：配置存取 + 模板注入 + 防回归。"""

from __future__ import annotations

import json
from pathlib import Path

from relayhub.gateway import admin as admin_module
from relayhub.gateway import service as service_module
from relayhub.gateway import site


def test_sanitize_defaults_and_truncates() -> None:
    assert site.sanitize_name("") == site.DEFAULT_SITE_NAME
    assert site.sanitize_name("   ") == site.DEFAULT_SITE_NAME
    assert site.sanitize_name("  My   Hub  ") == "My Hub"
    assert len(site.sanitize_name("x" * 200)) == site.MAX_SITE_NAME


def test_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "site.json"
    site.save_site(path, "  星海 中转站 ")
    assert site.load_site(path).name == "星海 中转站"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["name"] == "星海 中转站"


def test_load_missing_is_default(tmp_path: Path) -> None:
    assert site.load_site(tmp_path / "absent.json").name == site.DEFAULT_SITE_NAME


def test_render_escapes_html() -> None:
    out = site.render("<title>__SITE__</title>", "A&B<script>")
    assert "__SITE__" not in out
    assert "A&amp;B&lt;script&gt;" in out


def test_templates_use_site_token() -> None:
    """防回归：标题必须走 __SITE__ 占位符，而不是又写死 relay-hub。"""
    assert "__SITE__" in service_module.PANEL_HTML
    assert "__SITE__" in admin_module.PAGE_HTML
    assert "__SITE__" in admin_module.LOGIN_HTML


def test_panel_render_injects_name() -> None:
    out = site.render(service_module.PANEL_HTML, "MyHub")
    assert "__SITE__" not in out
    assert "MyHub 用户面板" in out
