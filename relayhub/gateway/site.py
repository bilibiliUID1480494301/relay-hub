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
import re
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
    announcement_public: bool = False  # 是否允许未认证访问（走 /public/announcements）
    # 主题：只收颜色（#rgb/#rrggbb）与可选 Logo URL，**不收整段 CSS**（那就是存储型 XSS）
    theme_primary: str = ""
    theme_bg: str = ""
    theme_logo: str = ""


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


COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def sanitize_color(raw: object) -> str:
    text = str(raw or "").strip()
    return text if COLOR_RE.match(text) else ""


def sanitize_logo(raw: object) -> str:
    text = str(raw or "").strip()
    return text if text.startswith(("https://", "http://")) and len(text) <= 300 else ""


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
        theme_primary=sanitize_color(raw.get("theme_primary")),
        theme_bg=sanitize_color(raw.get("theme_bg")),
        theme_logo=sanitize_logo(raw.get("theme_logo")),
    )


def save_site(
    path: Path,
    name: object,
    announcement: object | None = None,
    announcement_until: object | None = None,
    announcement_level: object | None = None,
    theme_primary: object | None = None,
    theme_bg: object | None = None,
    theme_logo: object | None = None,
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
        theme_primary=current.theme_primary if theme_primary is None else sanitize_color(theme_primary),
        theme_bg=current.theme_bg if theme_bg is None else sanitize_color(theme_bg),
        theme_logo=current.theme_logo if theme_logo is None else sanitize_logo(theme_logo),
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
            "announcement_public": site.announcement_public,
            "theme_primary": site.theme_primary,
            "theme_bg": site.theme_bg,
            "theme_logo": site.theme_logo,
        },
    )
    return site


def render(html_text: str, name: str) -> str:
    """把模板里的 `__SITE__` 换成 HTML 转义后的站点名。"""
    return html_text.replace(SITE_TOKEN, escape(sanitize_name(name)))
