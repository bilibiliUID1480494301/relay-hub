"""上游响应特征：只报「变了什么」，不判断好坏。"""

from __future__ import annotations

import json
from pathlib import Path

from relayhub.gateway import drift


def _payload(**over):
    base = {
        "id": "chatcmpl-abc123",
        "model": "deepseek-chat",
        "system_fingerprint": "fp_1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "choices": [{"finish_reason": "stop"}],
    }
    base.update(over)
    return base


def test_signals_extract_five_dimensions():
    s = drift.signals(_payload())
    assert s["system_fingerprint"] == "fp_1"
    assert s["id_prefix"] == "chatcmpl"
    assert s["usage_keys"] == ["completion_tokens", "prompt_tokens"]
    assert s["finish_reason"] == "stop"
    assert s["model"] == "deepseek-chat"


def test_compare_ignores_missing_dimensions():
    prev = drift.signals(_payload())
    cur = drift.signals(_payload(system_fingerprint=None))
    assert drift.compare(prev, cur) == []          # 缺失不算变化


def test_compare_reports_each_changed_dimension():
    prev = drift.signals(_payload())
    cur = drift.signals(_payload(system_fingerprint="fp_2", model="deepseek-chat-v2"))
    assert set(drift.compare(prev, cur)) == {"system_fingerprint", "model"}


def test_store_tracks_history_and_caps_it(tmp_path: Path):
    store = drift.Store(tmp_path / "drift.json")
    store.observe("ch", "m", _payload())
    for i in range(drift.HISTORY + 5):
        store.observe("ch", "m", _payload(system_fingerprint=f"fp_{i}"))
    entry = store.state["entries"]["ch|m"]
    assert len(entry["history"]) == drift.HISTORY
    assert entry["current"]["system_fingerprint"] == f"fp_{drift.HISTORY + 4}"


def test_first_observation_is_not_a_change(tmp_path: Path):
    store = drift.Store(tmp_path / "drift.json")
    assert store.observe("ch", "m", _payload()) == []
    assert store.summary()["ch|m"]["changes"] == 0


def test_store_roundtrips_and_new_instance_sees_history(tmp_path: Path):
    path = tmp_path / "drift.json"
    store = drift.Store(path)
    store.observe("ch", "m", _payload())
    store.observe("ch", "m", _payload(system_fingerprint="fp_9"))
    store.save()
    again = drift.Store(path)
    assert again.summary()["ch|m"]["changes"] == 1
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == drift.SCHEMA
