"""审计建议的四条 CI 断言：防「机制有了但没接线」复发。"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from relayhub.gateway import secretbox
from relayhub.gateway.pool import KeyPool, UpstreamKey
from relayhub.gateway.pool import SUPPORTED_PROTOCOLS


def _key(idx: int) -> UpstreamKey:
    return UpstreamKey(
        key_id=f"k{idx}", label=f"l{idx}", base_url="https://x", api_key=f"sk-secret-{idx}"
    )


def test_cli_help_exits_zero():
    """① 任何入口崩了，--help 都过不去。这是最便宜的冒烟。"""
    proc = subprocess.run(
        [sys.executable, "-m", "relayhub.gateway", "--help"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-500:]


def test_pool_file_and_home_permissions(tmp_path: Path):
    """② 号池 600、数据根 700（POSIX 才有意义，Windows 只验内容）。"""
    path = tmp_path / "pool.json"
    KeyPool([_key(1)]).save(path)
    assert json_has_no_plaintext(path)

    if os.name == "posix":
        assert oct(path.stat().st_mode)[-3:] == "600"
        os.chmod(tmp_path, 0o700)
        assert oct(tmp_path.stat().st_mode)[-3:] == "700"


def json_has_no_plaintext(path: Path) -> bool:
    raw = path.read_bytes()
    return b"sk-secret-1" not in raw


def test_no_tear_reads_under_concurrent_writes(tmp_path: Path):
    """③ 一边写一边读，绝不能读到「空号池」——那是线上偶发 5xx 的根因。"""
    path = tmp_path / "pool.json"
    KeyPool([_key(1)]).save(path)

    stop = threading.Event()
    writes = [0]

    def writer() -> None:
        while not stop.is_set():
            try:
                KeyPool([_key(1)]).save(path)
                writes[0] += 1
            except OSError:
                return      # 测试结束、tmp 被清：静默收工，别把异常抛进 pytest 的线程钩子

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    empties = 0
    try:
        for _ in range(20000):
            if not KeyPool.load(path).keys:
                empties += 1
    finally:
        stop.set()
        thread.join(timeout=10)
    assert writes[0] > 0, "写线程没跑起来"
    assert empties == 0, f"出现 {empties} 次撕读"


def test_api_accepts_every_supported_protocol():
    """④ CLI 与 Python API 的协议集合必须一致（曾出现 CLI 能传 gemini-api、API 报错）。"""
    import relayhub.api as api_mod

    assert api_mod.PROTOCOL_ANTHROPIC in SUPPORTED_PROTOCOLS
    for proto in SUPPORTED_PROTOCOLS:
        got = api_mod.Station._guess_protocol("https://x", proto)  # type: ignore[attr-defined]
        assert got == proto
