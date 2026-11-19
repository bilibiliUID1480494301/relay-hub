"""new-api / relay-hub 的定价表解析（GET /api/pricing）。

返回结构按「模型 → 倍率/分组/端点」整理，不试图重算 billing_expr——
表达式是站点自己的计费口径，照抄展示即可。
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any


class PricingError(RuntimeError):
    pass


def parse(payload: dict[str, Any]) -> dict[str, Any]:
    vendors = {int(v.get("id") or 0): str(v.get("name") or "") for v in payload.get("vendors") or []}
    models: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        models.append(
            {
                "model": item.get("model_name"),
                "ratio": item.get("model_ratio"),
                "completion_ratio": item.get("completion_ratio"),
                "price": item.get("model_price"),
                "groups": list(item.get("enable_groups") or []),
                "endpoints": list(item.get("supported_endpoint_types") or []),
                "billing_mode": item.get("billing_mode") or "",
                "billing_expr": item.get("billing_expr") or "",
                "vendor": vendors.get(int(item.get("vendor_id") or 0), ""),
            }
        )
    models.sort(key=lambda m: str(m.get("model")))
    return {
        "count": len(models),
        "models": models,
        "group_ratio": payload.get("group_ratio") or {},
        "endpoints": sorted((payload.get("supported_endpoint") or {}).keys()),
        "vendors": [v for v in vendors.values() if v],
        "pricing_version": payload.get("pricing_version") or "",
    }


def fetch(base_url: str, api_key: str, timeout: float = 12.0) -> dict[str, Any]:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise PricingError("缺少 base_url")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    request = urllib.request.Request(
        base + "/api/pricing",
        headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise PricingError(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict):
        raise PricingError("返回不是对象")
    return parse(data)


# 价格接口的约定格式（已换算成每 1k token，不再做币种/倍率换算）：
#   {"in_per_1k": 1.0, "out_per_1k": 2.0,
#    "peak_in_per_1k": 2.0, "peak_out_per_1k": 4.0,   // 可选
#    "peak_hours": [9,10,11,14,15,16,17],             // 可选
#    "currency": "CNY"}
# 各家原始接口（new-api 的 model_ratio、官方价目表）格式不同，换算交给适配器，
# 网关只认这一层已经对齐的数字，避免把「猜比例」写进计费。
def parse_unit_prices(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PricingError("价格接口返回的不是对象")

    def num(name: str, default: float = 0.0) -> float:
        raw = payload.get(name, default)
        if raw in (None, ""):
            return default
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise PricingError(f"{name} 不是数字：{raw!r}") from exc
        if value < 0:
            raise PricingError(f"{name} 不能为负：{value}")
        return value

    hours: list[int] = []
    for item in payload.get("peak_hours") or []:
        try:
            hour = int(item)
        except (TypeError, ValueError):
            raise PricingError(f"peak_hours 里有非整数：{item!r}")
        if 0 <= hour <= 23:
            hours.append(hour)

    out = {
        "price_in_per_1k": num("in_per_1k"),
        "price_out_per_1k": num("out_per_1k"),
        "peak_hours": hours,
        "currency": str(payload.get("currency") or ""),
    }
    peak_in = num("peak_in_per_1k")
    peak_out = num("peak_out_per_1k")
    if peak_in or peak_out:
        # 峰时价折算成「相对标价的倍率」：取两个比值里较大的那个，宁可贵不便宜。
        ratios = [r for r in (
            peak_in / out["price_in_per_1k"] if out["price_in_per_1k"] else 0.0,
            peak_out / out["price_out_per_1k"] if out["price_out_per_1k"] else 0.0,
        ) if r > 0]
        out["peak_multiplier"] = round(max(ratios), 4) if ratios else 1.0
    return out


def fetch_unit_prices(url: str, api_key: str = "", timeout: float = 12.0) -> dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        raise PricingError("价格接口必须是 http(s) 地址")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except OSError as exc:
        raise PricingError(f"拉取价格失败：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise PricingError("价格接口返回的不是 JSON") from exc
    return parse_unit_prices(payload)
