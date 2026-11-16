"""一键升级：下载 → 验签 → 安装 → 留回滚点。

铁律：**验签不过绝不安装**。升级等于执行代码，任何一处失败都必须停在原地。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import signing
from .atomicio import write_json_atomic
from .signing_key import PUBLIC_KEY_B64

UA = "relayhub-updater"
REPO = "bilibiliUID1480494301/relay-hub"
GITHUB_API = "https://api.github.com"
PROBE_TIMEOUT = 8.0  # 清单探针的超时上限：直连被墙时别干等


VERSION_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")


def check_version(version: str) -> str:
    """版本号会拼进落盘文件名和下载 URL：只允许 x.y.z，杜绝 ../ 与换行注入。"""
    text = str(version or "").strip()
    if not VERSION_RE.match(text):
        raise ValueError("版本号必须形如 1.2.3")
    return text


def release_base(version: str, repo: str = REPO) -> str:
    return f"https://github.com/{repo}/releases/download/v{version}"


def wheel_name(version: str) -> str:
    return f"hubrelay-{version}-py3-none-any.whl"


def _cache_name(name: str, version: str) -> str:
    """落盘缓存名：清单带版本后缀防止多版本互相覆盖，wheel 保持原名（安装器按名找）。"""
    if name == "SHA256SUMS":
        return f"SHA256SUMS-{version}"
    if name == "SHA256SUMS.sig":
        return f"SHA256SUMS-{version}.sig"
    return name


def pick_wheel(manifest: str, version: str) -> tuple[str, str]:
    """从 SHA256SUMS 里挑出该版本的 wheel，返回 (文件名, sha256)。"""
    target = wheel_name(version)
    for line in manifest.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*") == target:
            return target, parts[0].lower()
    raise signing.VerifyError(f"签名清单里没有 {target}")


def _download(url: str, dest: Path, timeout: float = 30.0, accept: str | None = None) -> Path:
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        dest.write_bytes(response.read())
    return dest


def _fetch(url: str, dest: Path, timeout: float = 30.0) -> Path:
    return _download(url, dest, timeout)


def _release_asset_index(repo: str, version: str, scratch: Path, timeout: float = 30.0) -> dict[str, str]:
    """按 tag 取「资产名 → 下载直链」。

    github.com:443 在部分地区会被整段封掉，api.github.com 通常仍然可通，
    这条回落是给「站长在自己机器上一键升级」兜底的。
    """
    payload = json.loads(
        _download(
            f"{GITHUB_API}/repos/{repo}/releases/tags/v{version}",
            scratch,
            timeout,
            accept="application/vnd.github+json",
        ).read_bytes().decode("utf-8")
    )
    return {a["name"]: a["url"] for a in payload.get("assets", []) if a.get("name") and a.get("url")}


def _fetch_asset(
    name: str,
    direct_url: str,
    dest: Path,
    timeout: float,
    repo: str,
    version: str,
    state: dict[str, Any],
    direct_timeout: float | None = None,
) -> Path:
    """先走 releases/download 直链；被挡时回落到 API 资产端点。

    回落只换传输路径，**不换校验强度**：拿回来的字节照旧要过签名与 sha256。
    一次直连失败就把 state["prefer_api"] 置位，后面几个资产别再挨个等超时
    （否则升级流程会被 3 次 30 秒超时拖成一分半）。

    `direct_timeout` 只作用于直连这一次尝试：拿清单当探针时给短超时，
    但 API 回落仍用完整 `timeout`——那一段是真在传数据，压不得。
    """
    if not state.get("prefer_api"):
        try:
            return _download(direct_url, dest, direct_timeout or timeout)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as direct_error:
            state["prefer_api"] = True
    else:
        direct_error = urllib.error.URLError("直连不可用，走 API 回落")

    try:
        index = state.setdefault("index", {})
        if not index:
            index.update(
                _release_asset_index(repo, version, dest.with_name(dest.name + ".assets.json"), timeout)
            )
        api_url = index.get(name)
        if not api_url:
            raise direct_error
        return _download(api_url, dest, timeout, accept="application/octet-stream")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError, ValueError, KeyError):
        raise direct_error from None


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


def stage(
    version: str, cache_dir: Path, timeout: float = 30.0, force: bool = False, repo: str = REPO
) -> dict[str, Any]:
    """下载清单+签名+wheel 并验证。返回 {ok, wheel, sha256, error}。"""
    version = check_version(version)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    base = release_base(version, repo)
    index: dict[str, Any] = {}

    def fetch(name: str, direct_timeout: float | None = None) -> Path:
        return _fetch_asset(name, f"{base}/{name}", cache_dir / f"{_cache_name(name, version)}",
                            timeout, repo, version, index, direct_timeout)

    try:
        # 先用清单当探针、给一个更短的超时：它只有几百字节，等满 30 秒不值得。
        # 直连被墙时（静默丢包、不是 RST）这一步省下的就是纯等待。
        probe_timeout = min(timeout, PROBE_TIMEOUT)
        # 显式按字节解码再归一：不依赖 read_text 的隐式换行翻译，
        # 否则换个读法（newline="" / bytes）验签结论就变了。
        manifest = signing.normalize_manifest(fetch("SHA256SUMS", probe_timeout).read_bytes().decode("utf-8"))
        signature = fetch("SHA256SUMS.sig").read_text(encoding="utf-8")
        name, expected = pick_wheel(manifest, version)
        wheel = fetch(name)
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
    write_json_atomic(history_path, history)
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
    flag_path.write_text(
        json.dumps({"version": version, "at": int(time.time()), "pid": os.getpid()}),
        encoding="utf-8",
    )
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
