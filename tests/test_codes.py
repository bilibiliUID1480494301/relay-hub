"""兑换码：批量生成已有，批量销毁必须「不误删已核销」的记录。"""

from __future__ import annotations

from pathlib import Path

import pytest

from relayhub.gateway.users import RedeemStore, UserError


def _store(tmp_path: Path) -> RedeemStore:
    return RedeemStore(tmp_path / "codes.json")


def test_batch_generate_is_unique_and_capped(tmp_path: Path):
    store = _store(tmp_path)
    fresh = store.generate(120, 50)          # 超过上限按 100 截断
    assert len(fresh) == 100
    assert len({c.code for c in fresh}) == 100


def test_destroy_removes_only_named_codes(tmp_path: Path):
    store = _store(tmp_path)
    fresh = store.generate(5, 10)
    result = store.destroy([fresh[0].code, fresh[1].code])
    assert result["removed"] == 2
    assert result["remaining"] == 3
    assert {c.code for c in store.list()} == {c.code for c in fresh[2:]}


def test_destroy_refuses_used_codes_by_default(tmp_path: Path):
    store = _store(tmp_path)
    fresh = store.generate(2, 10)
    store.redeem(fresh[0].code, "alice")

    result = store.destroy([fresh[0].code, fresh[1].code])
    assert result["removed"] == 1                 # 只删未用的那个
    assert result["refused_used"] == [fresh[0].code]
    assert {c.code for c in store.list()} == {fresh[0].code}   # 已用记录仍在


def test_destroy_can_force_used_when_explicitly_asked(tmp_path: Path):
    store = _store(tmp_path)
    fresh = store.generate(1, 10)
    store.redeem(fresh[0].code, "alice")
    result = store.destroy([fresh[0].code], include_used=True)
    assert result["removed"] == 1 and store.list() == []


def test_destroy_all_unused_keeps_history(tmp_path: Path):
    store = _store(tmp_path)
    fresh = store.generate(3, 10)
    store.redeem(fresh[0].code, "bob")
    result = store.destroy([], all_unused=True)
    assert result["removed"] == 2
    assert [c.code for c in store.list()] == [fresh[0].code]


def test_destroy_reports_missing_and_rejects_empty_request(tmp_path: Path):
    store = _store(tmp_path)
    store.generate(1, 10)
    result = store.destroy(["rhx-does-not-exist"])
    assert result["missing"] == ["rhx-does-not-exist"] and result["removed"] == 0
    with pytest.raises(UserError):
        store.destroy([])
