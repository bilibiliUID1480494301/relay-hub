"""TOTP 恢复码：一次性、只存哈希、重新生成即作废旧码。"""

from __future__ import annotations

from pathlib import Path

from relayhub.gateway import totpauth


def test_codes_are_human_friendly_and_unique(tmp_path: Path):
    store = totpauth.Store(tmp_path / "recovery.json")
    codes = store.regenerate(10)
    assert len(codes) == 10
    assert len(set(codes)) == 10
    for code in codes:
        head, tail = code.split("-")
        assert len(head) == 4 and len(tail) == 4
        assert all(ch in "abcdefghjkmnpqrstuvwxyz23456789" for ch in head + tail)


def test_plaintext_never_lands_on_disk(tmp_path: Path):
    path = tmp_path / "recovery.json"
    store = totpauth.Store(path)
    codes = store.regenerate(3)
    blob = path.read_text(encoding="utf-8")
    for code in codes:
        assert code not in blob and code.replace("-", "") not in blob


def test_consume_is_single_use(tmp_path: Path):
    store = totpauth.Store(tmp_path / "recovery.json")
    codes = store.regenerate(2)
    assert store.consume(codes[0]) is True
    assert store.consume(codes[0]) is False        # 第二次无效
    assert store.remaining() == 1


def test_regenerate_invalidates_old_codes(tmp_path: Path):
    path = tmp_path / "recovery.json"
    store = totpauth.Store(path)
    old = store.regenerate(2)
    store.regenerate(2)
    fresh = totpauth.Store(path)
    assert fresh.consume(old[0]) is False


def test_consume_tolerates_formatting(tmp_path: Path):
    store = totpauth.Store(tmp_path / "recovery.json")
    code = store.regenerate(1)[0]
    assert store.consume(code.upper().replace("-", "")) is True


def test_store_survives_restart(tmp_path: Path):
    path = tmp_path / "recovery.json"
    totpauth.Store(path).regenerate(2)
    assert totpauth.Store(path).remaining() == 2
