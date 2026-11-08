"""安全公告与版本检查。

两条独立信息，别混：
  * 安全公告（GHSA / CVE）：我这版**有没有已知问题**
  * 版本检查（PyPI）：我这版**是不是落后了**

都在服务端做，但只做「查 + 报」——**不自动升级**。让服务端自己拉包重启等于
给供应链开直达通道，真要自动必须先有签名与回滚槽位。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Any

UA = "relayhub-security"
ADVISORY_URL = "https://api.github.com/repos/bilibiliUID1480494301/relay-hub/security-advisories"
PYPI_URL = "https://pypi.org/pypi/hubrelay/json"


def parse_version(text: str) -> tuple[int, ...]:
    """把 0.10.5 / 0.10.5rc1 都折成可比较的元组（非数字段记 0）。"""
    parts = re.findall(r"\d+", str(text or ""))
    return tuple(int(p) for p in parts[:4]) or (0,)


def _patched_ok(patched: str, current: str) -> bool:
    """current 是否已 >= 修复版本。"""
    if not patched:
        return False
    return parse_version(current) >= parse_version(patched)


def advisories_from(payload: Any, current: str) -> list[dict[str, Any]]:
    """把 GitHub advisories 响应折成给 UI 的最小结构，**GHSA 与 CVE 都保留**。"""
    rows: list[dict[str, Any]] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        patched = ""
        for vuln in item.get("vulnerabilities") or []:
            if isinstance(vuln, dict) and vuln.get("patched_versions"):
                patched = str(vuln["patched_versions"])
                break
        rows.append(
            {
                "ghsa": str(item.get("ghsa_id") or ""),
                "cve": str(item.get("cve_id") or ""),          # 可能为空，MITRE 未分配
                "severity": str(item.get("severity") or ""),
                "summary": str(item.get("summary") or ""),
                "published": str(item.get("published_at") or ""),
                "url": str(item.get("html_url") or ""),
                "patched": patched,
                "affects_current": not _patched_ok(patched, current),
            }
        )
    rows.sort(key=lambda r: (not r["affects_current"], r["published"]), reverse=False)
    return rows


def update_from_pypi(payload: Any, current: str) -> dict[str, Any]:
    latest = ""
    if isinstance(payload, dict):
        latest = str((payload.get("info") or {}).get("version") or "")
    return {
        "current": current,
        "latest": latest,
        "behind": bool(latest) and parse_version(latest) > parse_version(current),
    }


def _get(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check(current: str, timeout: float = 6.0) -> dict[str, Any]:
    """查一次公告与最新版。任何一路失败都不影响另一路，也不抛给调用方。"""
    out: dict[str, Any] = {"checked_at": int(time.time()), "current": current, "error": ""}
    try:
        out["advisories"] = advisories_from(_get(ADVISORY_URL, timeout), current)
    except Exception as exc:  # noqa: BLE001
        out["advisories"] = []
        out["error"] = f"advisories: {type(exc).__name__}"
    try:
        out["update"] = update_from_pypi(_get(PYPI_URL, timeout), current)
    except Exception as exc:  # noqa: BLE001
        out["update"] = {"current": current, "latest": "", "behind": False}
        out["error"] = (out["error"] + " " if out["error"] else "") + f"pypi: {type(exc).__name__}"
    return out
