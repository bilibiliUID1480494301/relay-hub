"""上游响应特征监测：模型"换血/降智"的早期信号。

采的是**响应里稳定、便宜、与语义无关**的几个维度：

| 维度 | 来源 | 变化意味着什么 |
|---|---|---|
| `system_fingerprint` | 响应顶层（DeepSeek/OpenAI 系） | 上游后端换了版本 |
| `id_prefix` | `id` 里第一个 `-` 之前 | 提供商/实现换了 |
| `usage_keys` | `usage` 的键集合 | 计费口径变了（可能多算钱） |
| `finish_reason` | 首个 choice | 截断策略变了 |
| `model` | 响应回显的模型名 | 上游偷偷换了个便宜的模型 |

**只做观测，不参与决策**：不改号池、不自动熔断、不影响转发。
写入走 `persist` 的延迟落盘，请求路径只多一次内存赋值。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SCHEMA = "relayhub-drift/1"
HISTORY = 20          # 每个渠道+模型保留最近多少条变化记录


def signals(payload: dict[str, Any]) -> dict[str, Any]:
    """从一次响应里抽特征。缺失的维度记 None，不参与比较。"""
    if not isinstance(payload, dict):
        return {}
    raw_id = str(payload.get("id") or "")
    prefix = raw_id.split("-", 1)[0] if "-" in raw_id else raw_id[:12]
    usage = payload.get("usage")
    usage_keys = sorted(usage.keys()) if isinstance(usage, dict) else []
    choices = payload.get("choices") or []
    finish = ""
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish = str(choices[0].get("finish_reason") or "")
    return {
        "system_fingerprint": payload.get("system_fingerprint"),
        "id_prefix": prefix or None,
        "usage_keys": usage_keys,
        "finish_reason": finish or None,
        "model": payload.get("model"),
    }


def compare(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    """返回发生变化的维度名（只比较双方都非空的字段）。"""
    if not previous:
        return []
    changed: list[str] = []
    for key, value in current.items():
        if value in (None, "", []):
            continue
        before = previous.get(key)
        if before in (None, "", []):
            continue
        if before != value:
            changed.append(key)
    return changed


class Store:
    """按 `渠道|模型` 记录最近一次特征与变化历史。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.state: dict[str, Any] = {"schema": SCHEMA, "entries": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("schema") == SCHEMA:
                    self.state = loaded
            except (OSError, ValueError):
                pass

    def observe(self, channel: str, model: str, payload: dict[str, Any]) -> list[str]:
        """记录一次响应，返回变化的维度（无变化返回空列表）。"""
        current = signals(payload)
        if not current:
            return []
        key = f"{channel or 'unknown'}|{model or 'unknown'}"
        entry = self.state["entries"].setdefault(key, {"current": None, "history": []})
        changed = compare(entry.get("current"), current)
        if changed:
            entry.setdefault("history", []).append(
                {
                    "at": int(time.time()),
                    "changed": changed,
                    "before": {k: (entry.get("current") or {}).get(k) for k in changed},
                    "after": {k: current.get(k) for k in changed},
                }
            )
            entry["history"] = entry["history"][-HISTORY:]
        entry["current"] = current
        entry["at"] = int(time.time())
        return changed

    def save(self) -> None:
        from .atomicio import write_json_atomic

        write_json_atomic(self.path, self.state)

    def summary(self) -> dict[str, Any]:
        """给管理面看的视图：只列有过变化的渠道+模型。"""
        out: dict[str, Any] = {}
        for key, entry in self.state.get("entries", {}).items():
            history = entry.get("history") or []
            out[key] = {
                "current": entry.get("current"),
                "at": entry.get("at", 0),
                "changes": len(history),
                "last": history[-1] if history else None,
            }
        return out
