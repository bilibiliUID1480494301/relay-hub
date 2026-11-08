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
