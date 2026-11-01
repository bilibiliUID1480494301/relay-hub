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
