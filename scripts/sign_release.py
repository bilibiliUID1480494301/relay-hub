"""给 dist/ 里的产物签名：生成 SHA256SUMS 与 SHA256SUMS.sig。

用法（发布者本机，私钥不在仓库里）：
    python scripts/sign_release.py dist0110 --key E:/path/relayhub-signing.key

产出两个文件，随 GitHub Release 一起上传；客户端/服务端用包里写死的公钥验证。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sign_release")
    parser.add_argument("dist", help="dist 目录")
    parser.add_argument("--key", required=True, help="Ed25519 私钥 PEM 路径")
    args = parser.parse_args(argv)

    from cryptography.hazmat.primitives import serialization

    dist = Path(args.dist)
    files = sorted(p for p in dist.iterdir() if p.suffix in (".whl", ".gz"))
    if not files:
        print("没有可签名的产物", file=sys.stderr)
        return 1

    lines = []
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    manifest = "\n".join(lines) + "\n"
    (dist / "SHA256SUMS").write_text(manifest, encoding="utf-8")

    key = serialization.load_pem_private_key(Path(args.key).read_bytes(), password=None)
    signature = base64.b64encode(key.sign(manifest.encode("utf-8")))
    (dist / "SHA256SUMS.sig").write_bytes(signature)

    print(manifest, end="")
    print(f"signed {len(files)} file(s) -> {dist/'SHA256SUMS.sig'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
