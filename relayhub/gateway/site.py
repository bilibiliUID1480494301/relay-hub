"""站点个性化命名（对标 new-api 的 SystemName / 站点名称）。

只负责一件事：站点对外显示的名字。存 `<home>/site.json`（原子落盘），
用户面板 / 落地页 / 管理面在渲染时把 `__SITE__` 占位符换成它。

为什么单独一个文件而不是塞进 users.json：改站点名是「运维随手改」的动作，
与用户计费无关；混在一起会让两者的指纹热加载互相触发。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path

from .atomicio import write_json_atomic

DEFAULT_SITE_NAME = "relay-hub"
MAX_SITE_NAME = 48
SITE_TOKEN = "__SITE__"


@dataclass
class SiteConfig:
    name: str = DEFAULT_SITE_NAME


def sanitize_name(raw: object) -> str:
    """折叠空白、去掉不可见控制字符、限长；空则回落默认名。"""
    text = " ".join(str(raw or "").split())
    text = "".join(ch for ch in text if ch.isprintable())
    if not text:
        return DEFAULT_SITE_NAME
    return text[:MAX_SITE_NAME]


def load_site(path: Path) -> SiteConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SiteConfig()
    if not isinstance(raw, dict):
        return SiteConfig()
    return SiteConfig(name=sanitize_name(raw.get("name")))


def save_site(path: Path, name: object) -> SiteConfig:
    site = SiteConfig(name=sanitize_name(name))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {"schemaVersion": 1, "name": site.name})
    return site


def render(html_text: str, name: str) -> str:
    """把模板里的 `__SITE__` 换成 HTML 转义后的站点名。"""
    return html_text.replace(SITE_TOKEN, escape(sanitize_name(name)))
