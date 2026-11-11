"""迁移包：默认不带密钥、带密钥必须加密、合并只增不改。"""

from __future__ import annotations

import pytest

from relayhub.gateway import migrate


def test_export_default_carries_no_upstream_keys():
    bundle = migrate.export_bundle(
        [{"username": "alice"}], [{"token_hash": "h1"}], {"name": "站"}, pool=[{"api_key": "sk-secret"}]
    )
    assert bundle["include_secrets"] is False
    assert "pool_sealed" not in bundle
    assert "sk-secret" not in str(bundle)   # 密钥绝不能出现在默认包里


def test_export_with_secrets_requires_password():
    with pytest.raises(migrate.MigrateError):
        migrate.export_bundle([], [], {}, include_secrets=True, pool=[], password="short")


def test_sealed_pool_roundtrips_with_password():
    pytest.importorskip("cryptography")
    pool = [{"key_id": "k1", "api_key": "sk-secret"}]
    bundle = migrate.export_bundle([], [], {}, include_secrets=True, pool=pool, password="correct-horse")
    assert "sk-secret" not in bundle["pool_sealed"]          # 密文里看不到明文
    assert migrate.unseal(bundle["pool_sealed"], "correct-horse") == pool


def test_wrong_password_is_rejected_not_silently_empty():
    pytest.importorskip("cryptography")
    bundle = migrate.export_bundle([], [], {}, include_secrets=True, pool=[{"a": 1}], password="right-password")
    with pytest.raises(migrate.MigrateError):
        migrate.unseal(bundle["pool_sealed"], "wrong-password")


def test_validate_rejects_foreign_schema():
    assert migrate.validate_bundle({"schema": "other/9"})
    assert migrate.validate_bundle(None)
    ok = migrate.export_bundle([], [], {})
    assert migrate.validate_bundle(ok) == []


def test_merge_users_never_overwrites_existing():
    merged, skipped = migrate.merge_users(
        [{"username": "alice", "quota": 5}], [{"username": "alice", "quota": 999}, {"username": "bob"}]
    )
    assert [u["username"] for u in merged] == ["alice", "bob"]
    assert merged[0]["quota"] == 5        # 已存在的额度没被覆盖
    assert skipped == ["alice"]


def test_merge_tokens_skips_duplicates_and_plaintext():
    merged, skipped = migrate.merge_tokens(
        [{"token_hash": "h1", "name": "old"}],
        [{"token_hash": "h1", "name": "dup"}, {"name": "plaintext"}, {"token_hash": "h2", "name": "new"}],
    )
    assert [t["name"] for t in merged] == ["old", "new"]
    assert "dup" in skipped and "plaintext" in skipped
