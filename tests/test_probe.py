"""三题探测：判定是启发式，但必须可复现、不误报成功。"""

from __future__ import annotations

from pathlib import Path

from relayhub.gateway import probe


def test_three_probes_include_drawing():
    ids = [p["id"] for p in probe.prompts()]
    assert ids == ["pelican_bike", "candy", "random"]
    draw = next(p for p in probe.prompts() if p["id"] == "pelican_bike")
    assert "<svg" in draw["prompt"]           # 画图题必须要求 SVG 交付
    assert draw["kind"] == "draw"


def test_pelican_ok_and_suspect():
    good = '<svg viewBox="0 0 100 100"><circle cx="20" cy="80" r="10"/><circle cx="60" cy="80" r="10"/><polygon points="1,1 9,3 1,5"/></svg>'
    assert probe.evaluate("pelican_bike", good)["verdict"] == "ok"
    bad = "抱歉，我无法画图。"
    result = probe.evaluate("pelican_bike", bad)
    assert result["verdict"] == "suspect" and result["score"] == 0


def test_candy_answer_checked():
    assert probe.evaluate("candy", "2")["verdict"] == "ok"
    wrong = probe.evaluate("candy", "答案是 6")
    assert wrong["verdict"] == "suspect" and "6" in wrong["reasons"][0]


def test_random_rejects_template_output():
    assert probe.evaluate("random", "48392017")["verdict"] == "ok"
    assert probe.evaluate("random", "12345678")["verdict"] == "suspect"
    assert probe.evaluate("random", "没有数字")["verdict"] == "suspect"


def test_store_roundtrip_and_summary(tmp_path: Path):
    path = tmp_path / "probes.json"
    store = probe.Store(path)
    store.record("ch-a", "candy", {"verdict": "ok", "score": 2, "at": 1})
    store.record("ch-a", "random", {"verdict": "suspect", "score": 1, "at": 2})
    store.save()

    again = probe.Store(path)
    summary = again.summary()
    assert summary["ch-a"]["suspect"] == ["random"]
    assert summary["ch-a"]["at"] == 2


def test_every_result_carries_disclaimer_field_name():
    """结果里必须能看出「仅供参考」——别让 UI 把它当评分展示。"""
    src = Path(probe.__file__).read_text(encoding="utf-8")
    assert "不构成质量承诺" in src
