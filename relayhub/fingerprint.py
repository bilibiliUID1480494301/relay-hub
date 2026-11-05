"""模型指纹：同一渠道的模型清单变了（含能力/价格字段），提示管理员。

为什么需要：上游经常「同名换后端」——模型 id 不变，但上下文、模态、倍率悄悄变了；
网关上的路由与计费还按旧认知走，用户体感是「同一个模型突然变笨/变贵」。

做法：对 `/v1/models`（若有定价表则并入倍率）做**规范化哈希**，按渠道持久化，
下次扫描比对，输出 added / removed / changed 三类差异。不调用模型本身，
所以零推理成本。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

UA = "relayhub-fingerprint"
KEYS = ("id", "context_window", "max_output_tokens", "modalities", "ratio", "completion_ratio")


def _canonical(entry: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in KEYS:
        value = entry.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            value = sorted(str(v) for v in value)
        out[key] = value
    return out


def catalog_fingerprint(models: list[dict[str, Any]]) -> str:
    """对模型清单做稳定哈希（排序后序列化，字段缺失不参与）。"""
    rows = sorted((_canonical(m) for m in models), key=lambda m: str(m.get("id")))
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def diff(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, list[str]]:
    before = {str(m.get("id")): _canonical(m) for m in previous}
    after = {str(m.get("id")): _canonical(m) for m in current}
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(k for k in set(before) & set(after) if before[k] != after[k]),
    }


def fetch_models(base_url: str, api_key: str, timeout: float = 12.0) -> list[dict[str, Any]]:
    """拉上游 /v1/models 并归一成指纹用的字段。"""
    base = str(base_url or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    request = urllib.request.Request(
        base + "/v1/models",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc

    rows = payload.get("data") if isinstance(payload, dict) else payload
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id") or row.get("model_name")
        if not model_id:
            continue
        out.append(
            {
                "id": str(model_id),
                "context_window": row.get("context_window") or row.get("context_length"),
                "max_output_tokens": row.get("max_output_tokens"),
                "modalities": row.get("input_modalities") or row.get("modalities"),
                "ratio": row.get("model_ratio"),
                "completion_ratio": row.get("completion_ratio"),
            }
        )
    return out


class Store:
    """按渠道记录上一次的指纹与清单（JSON，原子落盘由调用方保证）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.state: dict[str, Any] = {}
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.state = {}

    def get(self, key_id: str) -> dict[str, Any] | None:
        return self.state.get(key_id)

    def record(self, key_id: str, models: list[dict[str, Any]]) -> dict[str, Any]:
        entry = {
            "fingerprint": catalog_fingerprint(models),
            "count": len(models),
            "models": [_canonical(m) for m in sorted(models, key=lambda m: str(m.get("id")))],
            "at": int(time.time()),
        }
        self.state[key_id] = entry
        return entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def scan(store: Store, targets: list[dict[str, str]], timeout: float = 12.0) -> list[dict[str, Any]]:
    """逐个渠道拉清单比对。targets: [{key_id, base_url, api_key}]。

    返回每条的 {key_id, ok, fingerprint, added, removed, changed, error}；
    只有 changes 非空才值得提醒管理员。
    """
    report: list[dict[str, Any]] = []
    for target in targets:
        key_id = str(target.get("key_id") or "")
        try:
            models = fetch_models(target.get("base_url", ""), target.get("api_key", ""), timeout)
        except RuntimeError as exc:
            report.append({"key_id": key_id, "ok": False, "error": str(exc), "changed": []})
            continue
        previous = store.get(key_id) or {}
        old = previous.get("models") or []
        changes = diff(old, models) if old else {"added": [], "removed": [], "changed": []}
        entry = store.record(key_id, models)
        report.append(
            {
                "key_id": key_id,
                "ok": True,
                "fingerprint": entry["fingerprint"],
                "count": entry["count"],
                "first_seen": not old,
                **changes,
            }
        )
    return report
