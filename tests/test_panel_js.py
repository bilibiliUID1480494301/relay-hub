"""内联 JS 必须语法正确。

教训：`PANEL_HTML` 里一处引号写错（`applyBg(''+c+'')`）导致整块脚本解析失败、
面板变全白——而所有 pytest 断言（「HTML 里有某个字符串」）都通过了。
Python 看不见 JS 语法，只能交给 node 检查。没装 node 就跳过，不阻塞 CI。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from relayhub.gateway import admin as admin_mod
from relayhub.gateway import service as service_mod
from relayhub.gateway import wallpaper as wallpaper_mod

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="需要 node 校验 JS 语法")


def _check(js: str) -> tuple[int, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js)
        path = fh.name
    try:
        proc = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
        return proc.returncode, proc.stderr
    finally:
        Path(path).unlink(missing_ok=True)


@needs_node
def test_user_panel_inline_script_is_valid_js():
    blocks = re.findall(r"<script>(.*?)</script>", service_mod.PANEL_HTML, re.S)
    assert blocks, "用户面板没有内联脚本？"
    for i, js in enumerate(blocks):
        code, err = _check(js)
        assert code == 0, f"PANEL_HTML 第 {i} 段脚本语法错误：{err[:300]}"


@needs_node
def test_admin_panel_inline_scripts_are_valid_js():
    blocks = re.findall(r"<script>(.*?)</script>", admin_mod.PAGE_HTML, re.S)
    assert blocks, "管理面没有内联脚本？"
    for i, js in enumerate(blocks):
        code, err = _check(js)
        assert code == 0, f"PAGE_HTML 第 {i} 段脚本语法错误：{err[:300]}"


@needs_node
@pytest.mark.parametrize("cfg", [
    {"bg_mode": "api", "bg_api_url": "https://a.example/w"},
    {"bg_mode": "rotate", "bg_images": ["https://a/1.jpg", "https://a/2.jpg"]},
])
def test_generated_wallpaper_script_is_valid_js(cfg):
    js = wallpaper_mod.rotate_script(wallpaper_mod.normalize(cfg))
    body = re.sub(r"^<script>|</script>$", "", js)
    code, err = _check(body)
    assert code == 0, err[:300]
