"""请求热路径不得做重 IO：请求日志常驻句柄、号池指纹按时间窗缓存。"""

from __future__ import annotations

from pathlib import Path

from relayhub.gateway import reqlog


def test_reqlog_opens_file_once_and_writes_all_lines(tmp_path: Path, monkeypatch):
    log = tmp_path / "requests.jsonl"
    opens = []
    real_open = open

    def counting_open(file, mode="r", *args, **kwargs):
        if "a" in mode:
            opens.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    for i in range(100):
        reqlog.record(path=log, model="m", ok=True, tokens_in=i, tokens_out=0)
    reqlog.close_all()

    assert len(opens) == 1, f"应只打开一次，实际 {len(opens)} 次"
    # 实际文件名带日期：requests.<YYYYMMDD>.jsonl
    written = next(tmp_path.glob("requests.*.jsonl"))
    assert len(written.read_text(encoding="utf-8").strip().splitlines()) == 100


def test_reqlog_chmod_only_on_open(tmp_path: Path, monkeypatch):
    log = tmp_path / "requests.jsonl"
    chmods = []
    monkeypatch.setattr(reqlog, "ensure_private", lambda p: chmods.append(p))
    for _ in range(50):
        reqlog.record(path=log, model="m", ok=True)
    reqlog.close_all()
    assert len(chmods) == 1, f"chmod 应只在打开时做一次，实际 {len(chmods)} 次"


def test_pool_fingerprint_is_cached_within_window(tmp_path: Path, monkeypatch):
    from relayhub.gateway import router
    from relayhub.gateway.pool import KeyPool, UpstreamKey

    path = tmp_path / "pool.json"
    KeyPool([UpstreamKey(key_id="k1", label="l", base_url="https://x", api_key="sk-a")]).save(path)

    calls = []
    real = router.config_fingerprint
    monkeypatch.setattr(router, "config_fingerprint", lambda p: calls.append(1) or real(p))
    monkeypatch.setenv("RELAYHUB_WATCH_INTERVAL", "5")

    reloading = router.ReloadingRouter(path)
    calls.clear()
    for _ in range(200):
        reloading.pool
    assert len(calls) <= 1, f"窗口内不该重复读盘，实际读了 {len(calls)} 次"
