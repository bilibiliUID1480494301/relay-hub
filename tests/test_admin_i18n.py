"""管理面 i18n：中文键必须都有英文值，且每个页签都有翻译。"""

from __future__ import annotations

import re

from relayhub.gateway import admin as admin_mod

CJK = re.compile(r"[\u4e00-\u9fff]")
# JSON 风格的键值对；值里可能有转义引号，故用「非引号或转义序列」匹配。
PAIR = re.compile(r'"([^"]{1,120})"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _dict_block() -> str:
    html = admin_mod.PAGE_HTML
    start = html.index("var DICT = {")
    end = html.index("};", start)
    return html[start:end]


def _dict_pairs() -> list[tuple[str, str]]:
    return PAIR.findall(_dict_block())


def test_dictionary_has_enough_entries():
    pairs = _dict_pairs()
    assert len(pairs) >= 150, f"字典只有 {len(pairs)} 条，覆盖不足"


def test_every_key_has_non_cjk_english_value():
    bad = [k for k, v in _dict_pairs() if not v.strip() or CJK.search(v)]
    assert not bad, f"这些键没有真正的英文翻译：{bad[:5]}"


def test_every_tab_label_is_translated():
    html = admin_mod.PAGE_HTML
    labels = re.findall(r'<button data-tab="[a-z]+">([^<]+)</button>', html)
    keys = {k for k, _ in _dict_pairs()}
    missing = [label for label in labels if label not in keys]
    assert not missing, f"页签缺翻译：{missing}"


def test_language_choice_persists_across_reload():
    html = admin_mod.PAGE_HTML
    assert "localStorage.getItem('rh_lang')" in html
    assert "localStorage.setItem('rh_lang'" in html


def test_sidebar_nav_and_status_dots_present():
    """布局：左侧竖排导航 + 每项状态灯 + 窄屏折叠。"""
    html = admin_mod.PAGE_HTML
    assert "grid-template-columns:200px minmax(0,1fr)" in html
    assert "#tabs button.active{background:var(--accent-soft)" in html
    assert "function navDots" in html and "setInterval(navDots" in html
    assert "@media(max-width:820px)" in html


def test_migrate_tab_lives_in_desktop_console_not_web():
    """迁移是运维动作，属于 Rust 管理端；网页端只保留只读能力。"""
    html = admin_mod.PAGE_HTML
    assert 'data-tab="migrate"' not in html
    assert 'id="pane-migrate"' not in html
    assert "mg-export" not in html          # 不能留下悬空的 DOM 引用
