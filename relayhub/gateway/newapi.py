"""new-api 站点的额度 / 账单 / 单价 / 单次成本查询。

对标 new-api 的 OpenAI 兼容 Billing 接口。**不是所有上游都是 new-api**——
只有 `/api/status` 或 `/api/pricing` 探通、且 billing 端点返回预期字段才当真。

两个必须记住的陷阱（new-api 自己文档里也强调）：
1. 字段名都叫 `*_usd`，但**实际单位**由站点的 `quota_display_type` 决定
   （USD / CNY / tokens）。这里不换算，只把单位一起返回，让人自己看。
2. `/dashboard/billing/usage` 的 `total_usage` 单位是「**分**」（= amount*100），
   所以金额 = total_usage / 100。硬额度字段是 `hard_limit_usd`。

零第三方依赖（urllib）。所有失败抛 `NewApiError`，由管理面转成 JSON 错误。
"""

from __future__ import annotations

import json
import ssl
import time
from datetime import date, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TIMEOUT = 8.0
USER_AGENT = "relayhub-admin/newapi"


class NewApiError(RuntimeError):
    """查询失败（网络 / 鉴权 / 返回非 JSON / 字段缺失）。message 直接回管理面。"""


# ---------------------------------------------------------------- 基础请求


def _root(base_url: str) -> str:
    """站点根：去空白、补 scheme、去末尾 `/` 与末尾 `/v1`（后面按需自己拼）。"""
    text = str(base_url or "").strip()
    if not text:
        raise NewApiError("缺少站点地址")
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    text = text.rstrip("/")
    if text.endswith("/v1"):
        text = text[:-3]
    return text


def _get_json(url: str, key: str = "", *, timeout: float = TIMEOUT) -> Any:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except OSError:
            pass
        raise NewApiError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except (URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        raise NewApiError(f"连接失败：{exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise NewApiError("返回不是 JSON —— 这个地址可能不是 new-api 站点") from exc


def _first_json(base: str, paths: list[str], key: str) -> Any:
    """按顺序试多个路径（带 / 不带 /v1），返回第一个成功的 JSON。"""
    last: NewApiError | None = None
    for path in paths:
        try:
            return _get_json(base + path, key)
        except NewApiError as exc:
            last = exc
    raise last or NewApiError("没有可用的接口路径")


def _unwrap(payload: Any) -> dict[str, Any]:
    """new-api 有些端点包在 {success, data} 里，有些是裸对象；两种都吃。"""
    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, dict):
            return inner
        return payload
    return {}


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _default_window(start_date: str | None, end_date: str | None) -> tuple[str, str]:
    end = (end_date or "").strip() or date.today().isoformat()
    start = (start_date or "").strip() or (
        date.today() - timedelta(days=29)
    ).isoformat()
    return start, end


# ---------------------------------------------------------------- 各接口


def status(base_url: str) -> dict[str, Any]:
    """站点公开状态（含 quota_display_type / quota_per_unit / display_in_currency）。"""
    data = _first_json(_root(base_url), ["/api/status"], "")
    return _unwrap(data)


def subscription(base_url: str, key: str) -> dict[str, Any]:
    """额度上限：hard_limit_usd / soft_limit_usd / access_until。"""
    data = _first_json(
        _root(base_url),
        [
            "/v1/dashboard/billing/subscription",
            "/dashboard/billing/subscription",
        ],
        key,
    )
    return _unwrap(data)


def usage(
    base_url: str, key: str, start_date: str | None = None, end_date: str | None = None
) -> dict[str, Any]:
    """已用额度：total_usage（单位「分」）。"""
    start, end = _default_window(start_date, end_date)
    query = "?" + urlencode({"start_date": start, "end_date": end})
    data = _first_json(
        _root(base_url),
        [
            "/v1/dashboard/billing/usage" + query,
            "/dashboard/billing/usage" + query,
        ],
        key,
    )
    return _unwrap(data)


def pricing(base_url: str) -> dict[str, Any]:
    """价格表：模型倍率 / 分组倍率 / 计费表达式。"""
    data = _first_json(_root(base_url), ["/api/pricing"], "")
    return _unwrap(data)


def request_cost(base_url: str, key: str, request_id: str) -> Any:
    """单次请求的精确消耗（request_id 来自响应头 X-Oneapi-Request-Id）。"""
    rid = str(request_id or "").strip()
    if not rid:
        raise NewApiError("缺少 request_id（从响应头 X-Oneapi-Request-Id 取）")
    data = _first_json(
        _root(base_url),
        [f"/api/cost/request/{rid}", f"/api/request/{rid}/cost"],
        key,
    )
    return data


# ---------------------------------------------------------------- 汇总


def snapshot(
    base_url: str,
    key: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """一次拿全：总额度、已用、剩余、单位、分组倍率。

    - `hard_limit` / `used` / `remaining` 都是**未换算**的数值，单位见 `unit`。
    - `used` 已按 new-api 的「分」口径除以 100。
    - `groups` 是分组倍率表（如 {"default":1,"vip":0.15}），取不到为 None。
    """
    root = _root(base_url)
    key = str(key or "").strip()

    st: dict[str, Any] = {}
    try:
        st = status(root)
    except NewApiError:
        st = {}

    sub = subscription(root, key)
    use = usage(root, key, start_date, end_date)

    price: dict[str, Any] = {}
    try:
        price = pricing(root)
    except NewApiError:
        price = {}

    hard = _num(sub.get("hard_limit_usd"))
    used_cents = _num(use.get("total_usage"))
    used = (used_cents / 100.0) if used_cents is not None else None
    remaining = (hard - used) if (hard is not None and used is not None) else None

    groups = None
    if isinstance(price, dict):
        raw_groups = price.get("group_ratio") or price.get("groupRatio")
        if isinstance(raw_groups, dict):
            groups = {str(k): v for k, v in raw_groups.items()}
    models = price.get("data") if isinstance(price.get("data"), list) else None

    return {
        "ok": True,
        "base_url": root,
        # 单位以站点 quota_display_type 为准（USD / CNY / tokens）
        "unit": str(
            st.get("quota_display_type") or st.get("quotaDisplayType") or "USD"
        ),
        "display_in_currency": bool(st.get("display_in_currency", False)),
        "quota_per_unit": st.get("quota_per_unit"),
        "hard_limit": hard,
        "used": used,
        "remaining": remaining,
        "used_cents": used_cents,
        "groups": groups,
        "model_count": len(models) if models is not None else None,
        "subscription": sub,
        "usage": use,
        "checked_at": time.time(),
    }
