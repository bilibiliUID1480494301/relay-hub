"""各家服务商的「模型端点 + 余额端点」探测。

管理面「账单」页用它做一键测试：勾选服务商 → 填 Key → 同时查模型与余额。
每家端点不同，这里只做**声明式描述 + 宽松解析**，不假装每家都有余额接口。

注意：余额接口常受账号类型/套餐限制（如 OpenAI 的 dashboard billing 对部分 key 直接 404），
因此 `balance` 取不到不算失败，只标 `balance_error`；`models` 取不到才算该家不可用。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

TIMEOUT = 12.0
UA = "relayhub-admin/providers"

# auth: bearer / x-api-key / query
# models: (path, 解析器名)   balance: (path, 解析器名) 或 None
CATALOG: list[dict[str, Any]] = [
    {
        "protocols": ["openai-chat"],
        "key": "openai",
        "label": "OpenAI",
        "base": "https://api.openai.com",
        "auth": "bearer",
        "models": "/v1/models",
        "balance": "/v1/dashboard/billing/subscription",
        "balance_kind": "openai",
        "note": "dashboard billing 仅部分账号可用",
    },
    {
        "protocols": ["anthropic-messages"],
        "key": "anthropic",
        "label": "Anthropic",
        "base": "https://api.anthropic.com",
        "auth": "x-api-key",
        "models": "/v1/models",
        "balance": None,
        "balance_kind": "",
        "note": "无公开余额接口",
    },
    {
        "protocols": ["gemini-api"],
        "key": "gemini",
        "label": "Google Gemini",
        "base": "https://generativelanguage.googleapis.com",
        "auth": "query",
        "models": "/v1beta/models",
        "balance": None,
        "balance_kind": "",
        "note": "无公开余额接口",
    },
    {
        "protocols": ["openai-chat", "anthropic-messages"],
        "key": "deepseek",
        "label": "DeepSeek",
        "base": "https://api.deepseek.com",
        "auth": "bearer",
        "models": "/v1/models",
        "balance": "/user/balance",
        "balance_kind": "deepseek",
        "note": "",
    },
    {
        "protocols": ["openai-chat", "anthropic-messages"],
        "key": "moonshot",
        "label": "Kimi（月之暗面）",
        "base": "https://api.moonshot.cn",
        "auth": "bearer",
        "models": "/v1/models",
        "balance": "/v1/users/me/balance",
        "balance_kind": "moonshot",
        "note": "",
    },
    {
        "protocols": ["openai-chat"],
        "key": "kimi-intl",
        "label": "Kimi 国际站",
        "base": "https://api.moonshot.ai",
        "auth": "bearer",
        "models": "/v1/models",
        "balance": "/v1/users/me/balance",
        "balance_kind": "moonshot",
        "note": "api.moonshot.ai",
    },
    {
        "protocols": ["openai-chat"],
        "key": "siliconflow",
        "label": "硅基流动",
        "base": "https://api.siliconflow.cn",
        "auth": "bearer",
        "models": "/v1/models",
        "balance": "/v1/user/info",
        "balance_kind": "siliconflow",
        "note": "",
    },
    {
        "protocols": ["openai-chat"],
        "key": "openrouter",
        "label": "OpenRouter",
        "base": "https://openrouter.ai",
        "auth": "bearer",
        "models": "/api/v1/models",
        "balance": "/api/v1/credits",
        "balance_kind": "openrouter",
        "note": "",
    },
    {
        "protocols": ["openai-chat"],
        "key": "groq",
        "label": "Groq",
        "base": "https://api.groq.com/openai",
        "auth": "bearer",
        "models": "/v1/models",
        "balance": None,
        "balance_kind": "",
        "note": "无公开余额接口",
    },
    {
        "protocols": ["openai-chat"],
        "key": "mistral",
        "label": "Mistral",
        "base": "https://api.mistral.ai",
        "auth": "bearer",
        "models": "/v1/models",
        "balance": None,
        "balance_kind": "",
        "note": "",
    },
    {
        "protocols": ["openai-chat"],
        "key": "together",
        "label": "Together",
        "base": "https://api.together.xyz",
        "auth": "bearer",
        "models": "/v1/models",
        "balance": None,
        "balance_kind": "",
        "note": "",
    },
    {
        "protocols": ["openai-chat"],
        "key": "zhipu",
        "label": "智谱 GLM",
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "auth": "bearer",
        "models": "/models",
        "balance": None,
        "balance_kind": "",
        "note": "",
    },
    {
        "protocols": ["openai-chat", "anthropic-messages"],
        "key": "newapi",
        "label": "new-api / one-api",
        "base": "",
        "auth": "bearer",
        "models": "/v1/models",
        "balance": "/v1/dashboard/billing/subscription",
        "balance_kind": "newapi",
        "note": "自建站，需填域名",
    },
]

BY_KEY = {item["key"]: item for item in CATALOG}


class ProbeError(RuntimeError):
    pass


def catalog() -> list[dict[str, Any]]:
    """给管理面的清单（不含任何密钥）。"""
    return [
        {
            "key": item["key"],
            "label": item["label"],
            "base": item["base"],
            "has_balance": bool(item["balance"]),
            "protocols": list(item.get("protocols") or []),
            "note": item["note"],
        }
        for item in CATALOG
    ]


def _norm_base(base: str) -> str:
    text = str(base or "").strip()
    if not text:
        raise ProbeError("缺少 base_url")
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    return text.rstrip("/")


def _get(url: str, api_key: str, auth: str, timeout: float) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": UA}
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth == "x-api-key":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif auth == "query":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode({'key': api_key})}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:160]
        except OSError:
            pass
        raise ProbeError(f"HTTP {exc.code}{('：' + detail) if detail else ''}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ProbeError(f"{type(exc).__name__}: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProbeError("返回不是 JSON") from exc
    return data if isinstance(data, dict) else {"data": data}


def _model_ids(payload: dict[str, Any]) -> list[str]:
    """兼容 OpenAI(`data[].id`)、Gemini(`models[].name`)、Anthropic(`data[].id`)。"""
    out: list[str] = []
    rows = payload.get("data")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                out.append(str(row["id"]))
    models = payload.get("models")
    if isinstance(models, list):
        for row in models:
            if isinstance(row, dict) and row.get("name"):
                out.append(str(row["name"]).removeprefix("models/"))
    return out


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _balance(kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """把各家余额返回归一成 {amount, currency, detail}；认不出给 None。"""
    if kind == "deepseek":
        infos = (payload.get("balance_infos") or [])
        if infos:
            row = infos[0]
            return {
                "amount": _num(row.get("total_balance")),
                "currency": str(row.get("currency") or "CNY"),
                "detail": row,
            }
    elif kind == "moonshot":
        data = payload.get("data") or {}
        return {
            "amount": _num(data.get("available_balance")),
            "currency": "CNY",
            "detail": data,
        }
    elif kind == "siliconflow":
        data = payload.get("data") or {}
        return {
            "amount": _num(data.get("totalBalance") or data.get("balance")),
            "currency": "CNY",
            "detail": data,
        }
    elif kind == "openrouter":
        data = payload.get("data") or {}
        total = _num(data.get("total_credits"))
        used = _num(data.get("total_usage"))
        if total is not None:
            return {
                "amount": round(total - (used or 0), 6),
                "currency": "USD",
                "detail": data,
            }
    elif kind in ("openai", "newapi"):
        hard = _num(payload.get("hard_limit_usd"))
        if hard is not None:
            return {"amount": hard, "currency": "USD", "detail": payload}
    return None


def probe(
    provider: str,
    api_key: str,
    base_url: str | None = None,
    *,
    timeout: float = TIMEOUT,
) -> dict[str, Any]:
    """查一家服务商：模型端点必查，余额端点有就查。"""
    spec = BY_KEY.get(str(provider or "").strip())
    if spec is None:
        raise ProbeError(f"未知服务商：{provider}")
    key = str(api_key or "").strip()
    if not key:
        raise ProbeError("缺少 API Key")
    base = _norm_base(base_url or spec["base"])

    started = time.perf_counter()
    try:
        models_payload = _get(base + spec["models"], key, spec["auth"], timeout)
    except ProbeError as exc:
        return {
            "ok": False,
            "provider": spec["key"],
            "label": spec["label"],
            "base_url": base,
            "models": 0,
            "model_ids": [],
            "error": f"模型端点：{exc}",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    ids = _model_ids(models_payload)

    balance: dict[str, Any] | None = None
    balance_error = ""
    if spec["balance"]:
        try:
            payload = _get(base + spec["balance"], key, spec["auth"], timeout)
            balance = _balance(spec["balance_kind"], payload)
            if balance is None:
                balance_error = "余额字段未识别"
        except ProbeError as exc:
            balance_error = str(exc)

    return {
        "ok": True,
        "provider": spec["key"],
        "label": spec["label"],
        "base_url": base,
        "models": len(ids),
        "model_ids": ids[:60],
        "balance": balance,
        "balance_error": balance_error,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
