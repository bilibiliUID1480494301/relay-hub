"""一键升级：下载 → 验签 → 安装 → 留回滚点。

铁律：**验签不过绝不安装**。升级等于执行代码，任何一处失败都必须停在原地。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import signing
from .signing_key import PUBLIC_KEY_B64

UA = "relayhub-updater"


def release_base(version: str, repo: str = "bilibiliUID1480494301/relay-hub") -> str:
    return f"https://github.com/{repo}/releases/download/v{version}"


def wheel_name(version: str) -> str:
    return f"hubrelay-{version}-py3-none-any.whl"


def pick_wheel(manifest: str, version: str) -> tuple[str, str]:
    """从 SHA256SUMS 里挑出该版本的 wheel，返回 (文件名, sha256)。"""
    target = wheel_name(version)
    for line in manifest.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*") == target:
            return target, parts[0].lower()
    raise signing.VerifyError(f"签名清单里没有 {target}")


def _fetch(url: str, dest: Path, timeout: float = 30.0) -> Path:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        dest.write_bytes(response.read())
    return dest


def verify_or_force(
    wheel: Path, manifest: str, signature: str, public_key: str, force: bool
) -> tuple[bool, bool, str]:
    """返回 (可安装, 已验签, 错误说明)。

    验签失败时：force=False → 拒绝；force=True → 允许，但**必须**把 verified=False
    传出去，让调用方记审计、让 UI 红字提示。不该存在「静默忽略」的分支。
    """
    try:
        signing.verify_wheel(wheel, manifest, signature, public_key)
        return True, True, ""
    except signing.VerifyError as exc:
        if not force:
            return False, False, str(exc)
        return True, False, str(exc)


def stage(version: str, cache_dir: Path, timeout: float = 30.0, force: bool = False) -> dict[str, Any]:
    """下载清单+签名+wheel 并验证。返回 {ok, wheel, sha256, error}。"""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    base = release_base(version)
    try:
        manifest = _fetch(f"{base}/SHA256SUMS", cache_dir / f"SHA256SUMS-{version}", timeout).read_text(
            encoding="utf-8"
        )
        signature = _fetch(f"{base}/SHA256SUMS.sig", cache_dir / f"SHA256SUMS-{version}.sig", timeout).read_text(
            encoding="utf-8"
        )
        name, expected = pick_wheel(manifest, version)
        wheel = _fetch(f"{base}/{name}", cache_dir / name, timeout)
        ok, verified, error = verify_or_force(wheel, manifest, signature, PUBLIC_KEY_B64, force)
        digest = signing.sha256_of(wheel)
        if not ok:
            return {"ok": False, "wheel": str(wheel), "sha256": digest, "verified": False, "error": error}
        return {"ok": True, "wheel": str(wheel), "sha256": digest, "verified": verified, "error": error}
    except signing.VerifyError as exc:
        return {"ok": False, "wheel": None, "sha256": "", "verified": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "wheel": None, "sha256": "", "error": f"{type(exc).__name__}: {exc}"}



def keep_rollback_point(current_wheel: Path | None, store_dir: Path) -> Path | None:
    """把当前版本的 wheel 复制进回滚目录，装坏了能退回。"""
    if current_wheel is None or not Path(current_wheel).is_file():
        return None
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    target = store_dir / f"{int(time.time())}-{Path(current_wheel).name}"
    shutil.copy2(current_wheel, target)
    return target


def record_install(store_dir: Path, version: str, wheel: Path, sha256: str) -> dict[str, Any]:
    """记一笔安装历史：回滚靠它找到上一版 wheel。只保留最近 5 条。"""
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    history_path = store_dir / "history.json"
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        history = []
    kept = store_dir / Path(wheel).name
    if Path(wheel) != kept:
        shutil.copy2(wheel, kept)
    history.append({"version": version, "wheel": str(kept), "sha256": sha256, "at": int(time.time())})
    history = history[-5:]
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"kept": str(kept), "count": len(history)}


def previous_install(store_dir: Path) -> dict[str, Any] | None:
    """返回**上一条**安装记录（当前正在跑的那版的上一版），没有则 None。"""
    try:
        history = json.loads((Path(store_dir) / "history.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if len(history) < 2:
        return None
    candidate = history[-2]
    return candidate if Path(candidate.get("wheel", "")).is_file() else None


def request_restart(flag_path: Path, version: str) -> Path:
    """写重启标记。**不在服务里自杀**：由 keepalive 看到标记后拉起新进程。"""
    flag_path = Path(flag_path)
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(json.dumps({"version": version, "at": int(time.time())}), encoding="utf-8")
    return flag_path


def install_argv(python: str, wheel: Path) -> list[str]:
    """安装命令：不装依赖、强制覆盖，避免升级时顺手改了别的东西。"""
    return [python, "-m", "pip", "install", "--no-deps", "--force-reinstall", "--no-input", str(wheel)]


def install(python: str, wheel: Path) -> dict[str, Any]:
    argv = install_argv(python, wheel)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)  # noqa: S603
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-2000:],
    }


def default_python() -> str:
    return sys.executable
