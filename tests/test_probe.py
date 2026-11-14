"""三题观察：题面必须是社区原话，且**不做自动判定**。"""

from __future__ import annotations

from pathlib import Path

from relayhub.gateway import probe


def test_three_probes_use_standard_wording():
    items = {p["id"]: p for p in probe.prompts()}
    assert set(items) == {"pelican_bike", "candy", "random"}
    # 鹈鹕题必须是原话，不能加「必须两个 circle」这类约束——加了就不是这道题了
    assert items["pelican_bike"]["prompt"] == "Draw a pelican riding a bicycle."
    assert items["candy"]["prompt"].startswith("Sally has 3 brothers")
    assert items["random"]["prompt"].startswith("Pick a random number between 1 and 100")


def test_random_probe_repeats_to_show_preference():
    items = {p["id"]: p for p in probe.prompts()}
    assert items["random"]["repeat"] == 5
    assert items["random"]["kind"] == "preference"


def test_no_verdict_field_anywhere_in_results(tmp_path: Path):
    """结果里不能出现 ok/suspect 这类判定字段——判定权在管理员。"""
    store = probe.Store(tmp_path / "probes.json")
    store.record("ch", "random", [{"text": "42", "elapsed_ms": 5}, {"text": "42", "elapsed_ms": 4}])
    store.save()
    summary = probe.Store(tmp_path / "probes.json").summary()
    assert summary["ch"]["random"]["answers"] == ["42", "42"]
    assert "verdict" not in str(summary)


def test_store_keeps_raw_answers_and_errors(tmp_path: Path):
    store = probe.Store(tmp_path / "probes.json")
    store.record("ch", "pelican_bike", [
        {"text": "<svg>...</svg>"},
        {"error": "HTTPError: 500"},
    ])
    out = store.summary()["ch"]["pelican_bike"]
    assert out["answers"][0].startswith("<svg")
    assert out["errors"] == ["HTTPError: 500"]
