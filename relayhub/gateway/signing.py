"""发布物强校验：Ed25519 签名 + SHA256 双保险。

为什么必须**失败关闭**：一键升级等于让服务端执行任意代码。只要有一项校验
无法完成（缺 crypto 库、缺签名、签名不匹配），就必须拒绝，不能降级成
「TLS 可信所以算通过」——那和没有校验没区别。
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path


class VerifyError(RuntimeError):
    """校验未通过。调用方必须放弃安装，不得吞掉异常继续。"""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _ed25519_verify(public_key_b64: str, signature_b64: str, message: bytes) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # 没 crypto 库 = 无法强校验 = 不许升级
        raise VerifyError("缺少 cryptography，无法校验签名（拒绝升级）") from exc

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    except Exception as exc:  # noqa: BLE001
        raise VerifyError("公钥格式不正确") from exc
    try:
        key.verify(base64.b64decode(signature_b64), message)
    except InvalidSignature as exc:
        raise VerifyError("签名与内容不匹配") from exc
    except Exception as exc:  # noqa: BLE001
        raise VerifyError(f"签名校验异常：{type(exc).__name__}") from exc


def verify_manifest(
    text: str, signature_b64: str, public_key_b64: str, expected_sha256: str
) -> str:
    """校验清单文本的签名，并确认目标文件的 sha256 在清单里。返回匹配到的哈希。"""
    if not signature_b64 or not public_key_b64:
        raise VerifyError("没有签名或公钥（发布方未签名）")
    _ed25519_verify(public_key_b64, signature_b64, text.encode("utf-8"))

    wanted = expected_sha256.strip().lower()
    if len(wanted) != 64:
        raise VerifyError("sha256 长度不对")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == wanted:
            return wanted
    raise VerifyError("该文件的 sha256 不在已签名清单里")


def verify_wheel(path: Path, manifest_text: str, signature_b64: str, public_key_b64: str) -> str:
    digest = sha256_of(path)
    verify_manifest(manifest_text, signature_b64, public_key_b64, digest)
    return digest
