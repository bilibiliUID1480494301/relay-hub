"""发布物强校验：签名对、签名错、没签名、清单里没这个文件。"""

from __future__ import annotations

import base64

import pytest

from relayhub.gateway import signing

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


def _keypair():
    private = Ed25519PrivateKey.generate()
    pub = base64.b64encode(
        private.public_key().public_bytes_raw()
    ).decode()
    return private, pub


def test_signed_manifest_accepts_matching_hash(tmp_path):
    private, pub = _keypair()
    wheel = tmp_path / "hubrelay-0.12.0-py3-none-any.whl"
    wheel.write_bytes(b"fake wheel")
    digest = signing.sha256_of(wheel)
    manifest = f"{digest}  {wheel.name}\n"
    sig = base64.b64encode(private.sign(manifest.encode())).decode()

    assert signing.verify_wheel(wheel, manifest, sig, pub) == digest


def test_tampered_file_is_rejected(tmp_path):
    private, pub = _keypair()
    wheel = tmp_path / "w.whl"
    wheel.write_bytes(b"original")
    manifest = f"{signing.sha256_of(wheel)}  w.whl\n"
    sig = base64.b64encode(private.sign(manifest.encode())).decode()
    wheel.write_bytes(b"tampered")

    with pytest.raises(signing.VerifyError):
        signing.verify_wheel(wheel, manifest, sig, pub)


def test_missing_signature_fails_closed(tmp_path):
    wheel = tmp_path / "w.whl"
    wheel.write_bytes(b"x")
    with pytest.raises(signing.VerifyError):
        signing.verify_wheel(wheel, "deadbeef", "", "")


def test_hash_not_in_manifest_rejected(tmp_path):
    private, pub = _keypair()
    wheel = tmp_path / "w.whl"
    wheel.write_bytes(b"x")
    manifest = "0" * 64 + "  other.whl\n"
    sig = base64.b64encode(private.sign(manifest.encode())).decode()
    with pytest.raises(signing.VerifyError):
        signing.verify_wheel(wheel, manifest, sig, pub)


def test_crlf_manifest_still_verifies(tmp_path):
    """清单被写成 CRLF 也要验得过：行尾不是安全边界，两侧归一规则一致即可。

    0.12.16~0.12.30 的发布资产就是「签 LF、存 CRLF」——按原始字节验的
    控制台全军覆没，靠 read_text 隐式归一的 Python 侧却侥幸通过。
    """
    private, pub = _keypair()
    wheel = tmp_path / "w.whl"
    wheel.write_bytes(b"payload")
    digest = signing.sha256_of(wheel)
    signed = f"{digest}  w.whl\n"
    sig = base64.b64encode(private.sign(signed.encode())).decode()

    assert signing.verify_wheel(wheel, signed.replace("\n", "\r\n"), sig, pub) == digest
    assert signing.verify_wheel(wheel, signed.replace("\n", "\r"), sig, pub) == digest


def test_sign_release_writes_the_bytes_it_signed(tmp_path):
    """sign_release 必须把签的那串字节原样落盘——write_text 会偷偷把 \\n 改成 CRLF。"""
    import importlib.util
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "sign_release.py"
    spec = importlib.util.spec_from_file_location("_sign_release", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    private, pub = _keypair()
    from cryptography.hazmat.primitives import serialization

    key_path = tmp_path / "k.pem"
    key_path.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "hubrelay-0.0.1-py3-none-any.whl"
    wheel.write_bytes(b"payload")

    assert module.main([str(dist), "--key", str(key_path)]) == 0

    raw = (dist / "SHA256SUMS").read_bytes()
    assert b"\r" not in raw, "清单落盘不能带 CR，否则签名字节与文件字节不一致"
    sig = base64.b64encode(private.sign(raw)).decode()  # 私钥签的就是落盘的字节
    assert signing.verify_wheel(wheel, raw.decode(), sig, pub) == signing.sha256_of(wheel)


def test_pinned_public_key_verifies_release_signature(tmp_path):
    """用仓库里写死的公钥验签：任何站长装的包都该能验同一份发布。"""
    from relayhub.gateway.signing_key import PUBLIC_KEY_B64

    wheel = tmp_path / "hubrelay-9.9.9-py3-none-any.whl"
    wheel.write_bytes(b"payload")
    manifest = f"{signing.sha256_of(wheel)}  {wheel.name}\n"

    # 只有持有私钥的人能签出可被这把公钥验证的签名
    private, _ = _keypair()
    wrong = base64.b64encode(private.sign(manifest.encode())).decode()
    with pytest.raises(signing.VerifyError):
        signing.verify_wheel(wheel, manifest, wrong, PUBLIC_KEY_B64)   # 别人的私钥签的 → 拒绝
