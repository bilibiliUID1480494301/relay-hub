"""模型能力探测：三个社区常见的「试金石」题目。

**只做记录与启发式提示，不构成质量承诺。** 一个模型答不出鹈鹕骑车，不等于它不可用；
答得出来也不等于它就稳。这是给管理员看的参考信号，不是评分。

设计要点：
  * **不参与请求路径**：只有管理员显式跑 `relayhub.gateway probe` 才会发请求，
    正常转发的开销为零。
  * 结果落 `probes.json`（0600），管理端只读展示，不自动重试、不自动改号池。
  * 每题都要求模型**用文字或 SVG 明确交付**，便于离线复核，而不是靠主观印象。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

SCHEMA = "relayhub-probe/1"

# 三题：画图 / 算术推理 / 随机性（防缓存与模板化回答）
PROBES: list[dict[str, Any]] = [
    {
        "id": "pelican_bike",
        "kind": "draw",
        "title": "鹈鹕骑自行车（画图）",
        "prompt": (
            "请用 SVG 画一只鹈鹕骑自行车。要求："
            "1) 只有一个 <svg> 根元素，viewBox 自定；"
            "2) 必须出现两个圆形车轮（circle 元素）；"
            "3) 必须有鹈鹕的长喙与喉囊（用一个细长三角形 + 一个椭圆表示）；"
            "只输出 SVG 代码，不要解释。"
        ),
    },
    {
        "id": "candy",
        "kind": "reason",
        "title": "糖果题（算术推理）",
        "prompt": (
            "小明有 5 颗糖，吃掉 2 颗，妈妈又给了 3 颗，他把其中 4 颗分给同学。"
            "最后他还有几颗糖？请只回答一个阿拉伯数字。"
        ),
        "expected": "2",
    },
    {
        "id": "random",
        "kind": "random",
        "title": "随机数题（防模板化）",
        "prompt": "请给我一个随机的 8 位数字（00000000-99999999）。只输出这 8 位数字，不要解释。",
    },
]


def prompts() -> list[dict[str, Any]]:
    """给调用方用的题面副本（不含期望答案的实现细节）。"""
    return [
        {"id": p["id"], "kind": p["kind"], "title": p["title"], "prompt": p["prompt"]}
        for p in PROBES
    ]


def evaluate(probe_id: str, text: str) -> dict[str, Any]:
    """启发式判定。返回 {verdict, score, reasons}，score 0~2，**不保证准确**。"""
    body = text or ""
    if probe_id == "pelican_bike":
        has_svg = "<svg" in body.lower()
        wheels = len(re.findall(r"<circle\b", body, re.I))
        has_beak = bool(re.search(r"<polygon|<path|<ellipse", body, re.I))
        reasons = []
        if not has_svg:
            reasons.append("没有 <svg> 根元素")
        if wheels < 2:
            reasons.append(f"圆形车轮只有 {wheels} 个")
        if not has_beak:
            reasons.append("看不到喙/喉囊一类的图元")
        score = 2 if has_svg and wheels >= 2 and has_beak else (1 if has_svg else 0)
        return {"verdict": "ok" if score == 2 else "suspect", "score": score, "reasons": reasons}

    if probe_id == "candy":
        digits = re.findall(r"-?\d+", body)
        hit = bool(digits) and digits[0] == "2"
        return {
            "verdict": "ok" if hit else "suspect",
            "score": 2 if hit else 0,
            "reasons": [] if hit else [f"期望 2，实际回答 {digits[0] if digits else '（没有数字）'}"],
        }

    if probe_id == "random":
        match = re.search(r"\b(\d{8})\b", body)
        if not match:
            return {"verdict": "suspect", "score": 0, "reasons": ["没有输出 8 位数字"]}
        value = match.group(1)
        # 常见模板化/被缓存的表现：连续重复、00000000、12345678
        degenerate = value in {"00000000", "12345678", "11111111"} or len(set(value)) == 1
        return {
            "verdict": "suspect" if degenerate else "ok",
            "score": 1 if degenerate else 2,
            "reasons": ["疑似模板化输出（重复位或常见序列）"] if degenerate else [],
        }

    return {"verdict": "unknown", "score": 0, "reasons": ["未知题号"]}


class Store:
    """探测结果存储：按渠道 -> 题号 -> 最近一次结果。"""

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

    def record(self, channel: str, probe_id: str, result: dict[str, Any]) -> None:
        bucket = self.state["channels"].setdefault(channel, {})
        bucket[probe_id] = result

    def save(self) -> None:
        from .atomicio import write_json_atomic

        write_json_atomic(self.path, self.state)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for channel, bucket in self.state.get("channels", {}).items():
            verdicts = {pid: rec.get("verdict") for pid, rec in bucket.items()}
            out[channel] = {
                "probes": bucket,
                "suspect": [pid for pid, v in verdicts.items() if v == "suspect"],
                "at": max((rec.get("at", 0) for rec in bucket.values()), default=0),
            }
        return out


def run_one(
    probe: dict[str, Any],
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 60.0,
    max_tokens: int = 700,
) -> dict[str, Any]:
    """跑一题。**只在管理员显式调用时发生**，不进转发路径。"""
    import urllib.request

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
        text = ""
        choices = raw.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            text = message.get("content") or ""
        verdict = evaluate(probe["id"], text)
        return {
            "probe": probe["id"],
            "at": int(started),
            "elapsed_ms": int((time.time() - started) * 1000),
            "ok": True,
            "text": text[:4000],
            "system_fingerprint": raw.get("system_fingerprint"),
            **verdict,
            "disclaimer": "启发式判断，仅供参考，不构成质量承诺",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "probe": probe["id"],
            "at": int(started),
            "elapsed_ms": int((time.time() - started) * 1000),
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "verdict": "error",
            "score": 0,
            "reasons": ["请求失败"],
        }
