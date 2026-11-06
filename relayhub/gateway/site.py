"""站点个性化命名（对标 new-api 的 SystemName / 站点名称）。

只负责一件事：站点对外显示的名字。存 `<home>/site.json`（原子落盘），
用户面板 / 落地页 / 管理面在渲染时把 `__SITE__` 占位符换成它。

为什么单独一个文件而不是塞进 users.json：改站点名是「运维随手改」的动作，
与用户计费无关；混在一起会让两者的指纹热加载互相触发。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path

from .atomicio import write_json_atomic

DEFAULT_SITE_NAME = "relay-hub"
MAX_SITE_NAME = 48
SITE_TOKEN = "__SITE__"


MAX_ANNOUNCEMENT = 280


@dataclass
class SiteConfig:
    name: str = DEFAULT_SITE_NAME
    # 全站公告：空串表示没有；until=0 表示长期有效（epoch 秒，到点自动失效）
    announcement: str = ""
    announcement_until: int = 0
    announcement_level: str = "info"   # info | warning | critical


def sanitize_name(raw: object) -> str:
    """折叠空白、去掉不可见控制字符、限长；空则回落默认名。"""
    text = " ".join(str(raw or "").split())
    text = "".join(ch for ch in text if ch.isprintable())
    if not text:
        return DEFAULT_SITE_NAME
    return text[:MAX_SITE_NAME]


def sanitize_announcement(raw: object) -> str:
    text = " ".join(str(raw or "").split())
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:MAX_ANNOUNCEMENT]


def announcement_active(config: SiteConfig, now: int | None = None) -> bool:
    if not config.announcement:
        return False
    if not config.announcement_until:
        return True
    return int(config.announcement_until) > int(now if now is not None else time.time())


def load_site(path: Path) -> SiteConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SiteConfig()
    if not isinstance(raw, dict):
        return SiteConfig()
    level = str(raw.get("announcement_level") or "info")
    return SiteConfig(
        name=sanitize_name(raw.get("name")),
        announcement=sanitize_announcement(raw.get("announcement")),
        announcement_until=int(raw.get("announcement_until") or 0),
        announcement_level=level if level in ("info", "warning", "critical") else "info",
    )


def save_site(
    path: Path,
    name: object,
    announcement: object | None = None,
    announcement_until: object | None = None,
    announcement_level: object | None = None,
) -> SiteConfig:
    """改名 / 改公告共用一处落盘；未传的字段沿用现有值（改名不该清掉公告）。"""
    path = Path(path)
    current = load_site(path)
    level = str(announcement_level if announcement_level is not None else current.announcement_level)
    site = SiteConfig(
        name=sanitize_name(name),
        announcement=(
            current.announcement
            if announcement is None
            else sanitize_announcement(announcement)
        ),
        announcement_until=(
            current.announcement_until
            if announcement_until is None
            else max(0, int(announcement_until or 0))
        ),
        announcement_level=level if level in ("info", "warning", "critical") else "info",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        path,
        {
            "schemaVersion": 1,
            "name": site.name,
            "announcement": site.announcement,
            "announcement_until": site.announcement_until,
            "announcement_level": site.announcement_level,
        },
    )
    return site


def render(html_text: str, name: str) -> str:
    """把模板里的 `__SITE__` 换成 HTML 转义后的站点名。"""
    return html_text.replace(SITE_TOKEN, escape(sanitize_name(name)))
