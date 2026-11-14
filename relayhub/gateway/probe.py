"""模型行为观察：三道社区流传的「试金石」题。

**这里不做评分。** 判定权在管理员：脚本与界面只负责把模型的原话原样带回来。

三条刻意的设计：

1. **题面保持原样，不加约束。** 鹈鹕骑车就是「画一只鹈鹕骑自行车」，一旦加上
   「必须两个 circle、必须有喙的 polygon」，考的就变成「能不能按要求输出 SVG」，
   而不是这道题原本要看的「模型怎么组合荒诞意象」。加了细节就不是同一道题。
2. **随机数题看的是「数字偏好」，不是随机性。** 同一模型连问多次会反复给出同一类数
   （42、7、生日型），这才是要观察的东西。所以默认连问 5 次看分布，不判断「够不够随机」。
3. **不自动判分。** 原始回答落盘 + 管理面/CLI 展示，由人判断。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SCHEMA = "relayhub-probe/2"

PROBES: list[dict[str, Any]] = [
    {
        "id": "pelican_bike",
        "kind": "draw",
        "title": "鹈鹕骑自行车",
        "prompt": "Draw a pelican riding a bicycle.",
        "note": "看模型如何把两个不相干的事物组合起来；SVG / ASCII / 纯文字描述都算作答。",
    },
    {
        "id": "candy",
        "kind": "reason",
        "title": "兄弟姐妹题",
        "prompt": "Sally has 3 brothers. Each brother has 2 sisters. How many sisters does Sally have?",
        "note": "陷阱在视角：每个兄弟的 2 个姐妹里包含 Sally，所以答案是 3。",
    },
    {
        "id": "random",
        "kind": "preference",
        "title": "随机数题（数字偏好）",
        "prompt": "Pick a random number between 1 and 100. Reply with the number only.",
        "repeat": 5,
        "note": "同一模型反复问，看它是否总给同一类数。这是偏好，不是随机性。",
    },
]


def prompts() -> list[dict[str, Any]]:
    return [
        {
            "id": p["id"],
            "kind": p["kind"],
            "title": p["title"],
            "prompt": p["prompt"],
            "note": p.get("note", ""),
            "repeat": p.get("repeat", 1),
        }
        for p in PROBES
    ]


class Store:
    """按渠道 → 题号保存**原始回答**，不做判定。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.state: dict[str, Any] = {"schema": SCHEMA, "channels": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("schema") == SCHEMA:
                    self.state = loaded
            except (OSError, ValueError):
                pass

    def record(self, channel: str, probe_id: str, answers: list[dict[str, Any]]) -> None:
        bucket = self.state["channels"].setdefault(channel, {})
        bucket[probe_id] = {"at": int(time.time()), "answers": answers}

    def save(self) -> None:
        from .atomicio import write_json_atomic

        write_json_atomic(self.path, self.state)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for channel, bucket in self.state.get("channels", {}).items():
            out[channel] = {
                probe: {
                    "at": rec.get("at", 0),
                    "answers": [a.get("text", "") for a in rec.get("answers", [])],
                    "errors": [a.get("error") for a in rec.get("answers", []) if a.get("error")],
                }
                for probe, rec in bucket.items()
            }
        return out


def run_one(
    probe: dict[str, Any],
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 90.0,
    max_tokens: int = 1200,
) -> list[dict[str, Any]]:
    """跑一题（随机数题连问 `repeat` 次）。返回原始结果，不含任何判定。"""
    import urllib.request

    rounds = int(probe.get("repeat", 1) or 1)
    answers: list[dict[str, Any]] = []
    for _ in range(rounds):
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": probe["prompt"]}],
        }
        request = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json", "authorization": f"Bearer {api_key}"},
            method="POST",
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
            choices = raw.get("choices") or []
            text = ""
            if choices:
                text = (choices[0].get("message") or {}).get("content") or ""
            answers.append(
                {
                    "at": int(started),
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "text": text,
                    "system_fingerprint": raw.get("system_fingerprint"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            answers.append(
                {
                    "at": int(started),
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }
            )
    return answers
