"""安全公告 / 版本检查 / 主题校验的纯函数测试（不联网）。"""

from __future__ import annotations

from relayhub.gateway import security, site


def test_parse_version_orders_numerically():
    assert security.parse_version("0.10.5") > security.parse_version("0.9.17")
    assert security.parse_version("0.10.5rc1") == (0, 10, 5, 1)


def test_advisories_keep_both_ghsa_and_cve():
    payload = [
        {
            "ghsa_id": "GHSA-aaaa-bbbb-cccc",
            "cve_id": "CVE-2026-12345",
            "severity": "high",
            "summary": "upstream error leak",
            "html_url": "https://example.test/a",
            "vulnerabilities": [{"patched_versions": "0.10.0"}],
        },
        {
            "ghsa_id": "GHSA-dddd-eeee-ffff",
            "cve_id": None,                     # 还没分配 CVE
            "severity": "medium",
            "summary": "older",
            "html_url": "https://example.test/b",
            "vulnerabilities": [{"patched_versions": "0.9.12"}],
        },
    ]
    rows = security.advisories_from(payload, "0.10.5")
    assert [r["ghsa"] for r in rows] == ["GHSA-aaaa-bbbb-cccc", "GHSA-dddd-eeee-ffff"]
    assert rows[0]["cve"] == "CVE-2026-12345"
    assert rows[1]["cve"] == ""
    assert all(not r["affects_current"] for r in rows)   # 都已在 0.10.5 之前修复


def test_advisory_marks_affected_when_behind():
    payload = [{"ghsa_id": "GHSA-x", "vulnerabilities": [{"patched_versions": "0.10.0"}]}]
    rows = security.advisories_from(payload, "0.9.9")
    assert rows[0]["affects_current"] is True


def test_update_from_pypi_flags_behind():
    assert security.update_from_pypi({"info": {"version": "0.10.6"}}, "0.10.5")["behind"] is True
    assert security.update_from_pypi({"info": {"version": "0.10.5"}}, "0.10.5")["behind"] is False


def test_theme_accepts_only_colors(tmp_path):
    path = tmp_path / "site.json"
    site.save_site(path, "n", theme_primary="#2563eb", theme_bg="javascript:x", theme_logo="javascript:alert(1)")
    loaded = site.load_site(path)
    assert loaded.theme_primary == "#2563eb"
    assert loaded.theme_bg == ""                 # 非颜色被丢弃
    assert loaded.theme_logo == ""               # 非 http(s) 被丢弃
