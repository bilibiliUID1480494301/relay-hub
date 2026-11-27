"""面板壁纸：静态 / 多张轮换 / 从 API 拉取。

三条约束（面板是公网面，配置可能被手改，读侧不信任写侧）：

1. **只接受 http(s) 图片地址**，且不含空格、引号、括号、反斜杠——这些会
   直接拼进 CSS 的 `url('…')`，是注入点。
2. **数量与间隔有上下限**：最多 12 张、间隔 10~3600 秒。没有上限的轮换等于
   让浏览器持续下载，用户流量会被悄悄吃掉。
3. **API 模式只允许 https**：它返回的是 URL 或 JSON，走 http 等于允许中间人
   把壁纸换成任意内容；`javascript:` / `data:` 一律拒绝。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

MAX_IMAGES = 12
MIN_SECONDS = 10
MAX_SECONDS = 3600
DEFAULT_SECONDS = 60
MODES = ("static", "rotate", "api")

# 会破坏 CSS url() 或引入脚本的字符
_BAD_CHARS = " \t\r\n\"'<>\\()"
_HTTPS = re.compile(r"^https://[^\s\"'<>\\()]{4,}$", re.I)
_HTTP_OR_HTTPS = re.compile(r"^https?://[^\s\"'<>\\()]{4,}$", re.I)


def clean_image(url: object) -> str:
    """单张壁纸地址。不合法返回空串（调用方据此过滤，不报错）。"""
    text = str(url or "").strip()
    if not text or len(text) > 2000:
        return ""
    if any(ch in text for ch in _BAD_CHARS):
        return ""
    if not _HTTP_OR_HTTPS.match(text):
        return ""
    if text.lower().startswith(("javascript:", "data:", "vbscript:")):
        return ""
    return text


def clean_images(values: object) -> list[str]:
    """最多 MAX_IMAGES 张，去重保序。"""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        return []
    out: list[str] = []
    for item in values:
        url = clean_image(item)
        if url and url not in out:
            out.append(url)
        if len(out) >= MAX_IMAGES:
            break
    return out


def clamp_seconds(value: object) -> int:
    try:
        seconds = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_SECONDS
    return max(MIN_SECONDS, min(MAX_SECONDS, seconds))


def clean_api_url(url: object, allow_http: bool = False) -> str:
    """壁纸 API 默认只允许 https；allow_http 是显式开关（自建/内网接口用）。"""
    text = str(url or "").strip()
    if not text or len(text) > 2000:
        return ""
    if any(ch in text for ch in _BAD_CHARS):
        return ""
    if _HTTPS.match(text):
        return text
    return text if (allow_http and _HTTP_OR_HTTPS.match(text)) else ""


def clean_api_key(value: object) -> str:
    """上游壁纸 API 的 Key。只存服务端，**绝不下发给浏览器**。"""
    text = str(value or "").strip()
    if len(text) > 400 or any(c in text for c in (chr(13), chr(10))):
        return ""
    return text


def normalize(config: dict[str, Any] | None) -> dict[str, Any]:
    """把落盘配置归一成渲染层能直接用的形状。"""
    cfg = config or {}
    images = clean_images(cfg.get("bg_images") or [])
    single = clean_image(cfg.get("bg_image") or "")
    if single and single not in images:
        images.insert(0, single)
    allow_http = bool(cfg.get("bg_api_allow_http"))
    api_url = clean_api_url(cfg.get("bg_api_url") or "", allow_http=allow_http)

    mode = str(cfg.get("bg_mode") or "static").strip().lower()
    if mode not in MODES:
        mode = "static"
    # 没有可用素材就降级，别让页面出现空白背景
    if mode == "rotate" and len(images) < 2:
        mode = "static"
    if mode == "api" and not api_url:
        mode = "static" if images else "static"

    raw_opacity = cfg.get("bg_opacity", 1.0)
    if raw_opacity is None or raw_opacity == "":
        raw_opacity = 1.0
    try:
        opacity = float(raw_opacity)
    except (TypeError, ValueError):
        opacity = 1.0
    opacity = min(1.0, max(0.05, opacity))

    return {
        "opacity": round(opacity, 2),
        "mode": mode,
        "images": images[:MAX_IMAGES],
        "seconds": clamp_seconds(cfg.get("bg_rotate_seconds") or DEFAULT_SECONDS),
        "api_url": api_url,
        "api_key": clean_api_key(cfg.get("bg_api_key")),
        "api_header": str(cfg.get("bg_api_header") or "Authorization").strip() or "Authorization",
        "allow_http": allow_http,
    }


def rotate_script(wallpaper: dict[str, Any]) -> str:
    """轮换 / API 两种模式的注入脚本。

    注意：这里拼的是**会被浏览器解析的 JS**，拼接结果必须能被 node --check 通过
    （tests/test_panel_js.py 守着这条）。不要再往里塞 try/catch 之类需要配平的结构。
    """
    mode = wallpaper.get("mode")
    if mode not in ("rotate", "api"):
        return ""
    seconds = int(wallpaper.get("seconds") or DEFAULT_SECONDS) * 1000
    if mode == "rotate":
        images = wallpaper.get("images") or []
        if len(images) < 2:
            return ""
        payload = ",".join(f'"{u}"' for u in images)
        js = (
            "(function(){"
            f"var list=[{payload}],i=0,body=document.body;"
            "body.style.transition='background-image .8s ease-in-out';"
            "function apply(){body.style.backgroundImage=\"url('\"+list[i]+\"')\";"
            "i=(i+1)%list.length;}"
            "apply();"
            "if(window.__rhWpTimer){clearInterval(window.__rhWpTimer);}"
            f"window.__rhWpTimer=setInterval(apply,{seconds});"
            "})();"
        )
    else:
        js = (
            "(function(){"
            "var url='/panel/wallpaper',body=document.body;"
            "body.style.transition='background-image .8s ease-in-out';"
            "function apply(u){"
            r"if(!/^https?:\/\//i.test(u)){return;}"
            "body.style.backgroundImage=\"url('\"+u+\"')\";}"
            "function tick(){"
            "fetch(url,{cache:'no-store',credentials:'omit'})"
            ".then(function(r){return r.json();})"
            ".then(function(j){apply(String((j&&j.url)||''));})"
            ".catch(function(){});}"
            "tick();"
            "if(window.__rhWpTimer){clearInterval(window.__rhWpTimer);}"
            f"window.__rhWpTimer=setInterval(tick,{seconds});"
            "})();"
        )
    return "<script>" + js + "</script>"


def pick_url(raw: str, allow_http: bool = False) -> str:
    """从壁纸接口的响应里取 URL：支持 {"url": …} / {"image": …} / 纯文本。"""
    import json as _json

    text = (raw or "").strip()
    if not text:
        return ""
    try:
        payload = _json.loads(text)
    except ValueError:
        return clean_api_url(text, allow_http=allow_http)
    if isinstance(payload, dict):
        candidate = str(payload.get("url") or payload.get("image") or "")
    elif isinstance(payload, list) and payload:
        first = payload[0]
        candidate = str(first.get("url") or "") if isinstance(first, dict) else str(first)
    else:
        candidate = str(payload)
    return clean_api_url(candidate, allow_http=allow_http)


def body_css(config: dict[str, Any] | None) -> str:
    """给 body 用的背景声明（管理面与控制台共用同一份配置）。"""
    wp = normalize(config)
    fit = str((config or {}).get("bg_fit") or "cover").strip().lower()
    if fit not in ("cover", "contain"):
        fit = "cover"
    color = str((config or {}).get("bg_color") or "")
    css = ""
    import re as _re
    if _re.fullmatch(r"#[0-9a-fA-F]{3,8}", color):
        css += f"background-color:{color};"
    image = wp["images"][0] if wp["images"] else ""
    if image:
        css += f"background-image:url('{image}');"
    if wp["mode"] != "static" or image:
        css += (
            f"background-size:{fit};background-repeat:no-repeat;"
            "background-attachment:fixed;background-position:center;"
        )
    # 透明度：壁纸太抢眼时用它压淡。用 z-index:-1 的固定层盖在背景上，
    # 不参与内容层叠，所以表格文字不受影响。
    if image and wp["opacity"] < 1.0:
        fade = round(1.0 - wp["opacity"], 2)
        css += (
            "body::after{content:\"\";position:fixed;inset:0;z-index:-1;pointer-events:none;"
            f"background:#f6f7f9;opacity:{fade};}}"
            "@media(prefers-color-scheme:dark){body::after{background:#101113}}"
        )
    return css
