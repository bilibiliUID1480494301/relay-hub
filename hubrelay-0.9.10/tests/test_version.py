"""版本号单一来源守卫。

历史上这里有四处漂移：__init__ 硬编码 0.1.0、pyproject 写 0.2.0、
service 的 server_version 写 "relayhub/0.2"、admin 写 "relayhub-admin/0.1"。
现在 __version__ 由 pyproject 探测，server_version 一律 f-string 拼接。
这两条断言把「再手写一个字面量版本号」挡在门外。
"""

from __future__ import annotations

import re
from pathlib import Path

import relayhub
from relayhub.gateway import admin as admin_module
from relayhub.gateway import service as service_module


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    match = re.search(
        r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M
    )
    assert match, "pyproject.toml 必须有 version"
    assert relayhub.__version__ == match.group(1)


def test_server_version_strings_derive_from_version() -> None:
    v = relayhub.__version__
    assert service_module.RelayHandler.server_version == f"relayhub/{v}"
    assert admin_module.AdminHandler.server_version == f"relayhub-admin/{v}"
