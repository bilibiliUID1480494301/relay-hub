"""站点迁移：导出/导入用户、令牌、站点设置。

**默认不导出上游 Key**：迁移便利换不来密钥泄露的代价。要带密钥必须显式
`include_secrets=True` 且提供口令——导出的是 scrypt+AES-GCM 密文，不是明文 JSON。

令牌只导出 SHA-256 哈希：迁移后旧的 `rht_…` 明文仍然有效（校验比的是哈希），
但迁移包里**拿不到可用于登录的凭证**。
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

SCHEMA = "relayhub-migrate/1"
SALT_LEN = 16
NONCE_LEN = 12


class MigrateError(RuntimeError):
    pass


def export_bundle(
    users: list[dict[str, Any]],
    tokens: list[dict[str, Any]],
    site: dict[str, Any],
    *,
    include_secrets: bool = False,
    pool: list[dict[str, Any]] | None = None,
    password: str = "",
) -> dict[str, Any]:
    if include_secrets and len(password) < 8:
        raise MigrateError("导出密钥必须提供至少 8 位口令")
    bundle: dict[str, Any] = {
        "schema": SCHEMA,
        "exported_at": int(time.time()),
        "include_secrets": include_secrets,
        "site": site,
        "users": users,
        "tokens": tokens,
    }
    if include_secrets and pool is not None:
        bundle["pool_sealed"] = _seal(json.dumps(pool, ensure_ascii=False), password)
    return bundle


def _seal(plain: str, password: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    salt, nonce = os.urandom(SALT_LEN), os.urandom(NONCE_LEN)
    key = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode())
    blob = AESGCM(key).encrypt(nonce, plain.encode(), SCHEMA.encode())
    return base64.b64encode(salt + nonce + blob).decode("ascii")


def unseal(sealed: str, password: str) -> list[dict[str, Any]]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    try:
        raw = base64.b64decode(sealed)
    except ValueError as exc:
        raise MigrateError("密文不是合法 base64") from exc
    salt, nonce, blob = raw[:SALT_LEN], raw[SALT_LEN : SALT_LEN + NONCE_LEN], raw[SALT_LEN + NONCE_LEN :]
    key = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode())
    try:
        plain = AESGCM(key).decrypt(nonce, blob, SCHEMA.encode())
    except Exception as exc:  # noqa: BLE001
        raise MigrateError("口令错误或密文被篡改") from exc
    return json.loads(plain.decode("utf-8"))


def validate_bundle(bundle: Any) -> list[str]:
    """返回问题列表；空列表表示可以导入。"""
    if not isinstance(bundle, dict):
        return ["包不是 JSON 对象"]
    problems: list[str] = []
    if bundle.get("schema") != SCHEMA:
        problems.append(f"schema 不是 {SCHEMA}（拿到的是 {bundle.get('schema')!r}）")
    for key, kind in (("users", list), ("tokens", list), ("site", dict)):
        if not isinstance(bundle.get(key), kind):
            problems.append(f"{key} 类型应为 {kind.__name__}")
    return problems


def merge_users(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """按用户名合并：已存在就跳过（不覆盖密码与额度，避免导入把现有账号清空）。"""
    seen = {str(u.get("username")) for u in existing}
    merged = list(existing)
    skipped: list[str] = []
    for user in incoming:
        name = str(user.get("username") or "")
        if not name:
            skipped.append("<空用户名>")
            continue
        if name in seen:
            skipped.append(name)
            continue
        merged.append(user)
        seen.add(name)
    return merged, skipped


def merge_tokens(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """按 token 哈希合并：同一枚令牌不重复插入。"""
    seen = {str(t.get("token_hash")) for t in existing if t.get("token_hash")}
    merged = list(existing)
    skipped: list[str] = []
    for token in incoming:
        digest = str(token.get("token_hash") or "")
        if not digest:
            skipped.append(str(token.get("name") or "<无哈希>"))   # 明文令牌不导入
            continue
        if digest in seen:
            skipped.append(str(token.get("name") or digest[:8]))
            continue
        merged.append(token)
        seen.add(digest)
    return merged, skipped
