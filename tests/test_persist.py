"""延迟落盘：请求路径不得同步写盘；flush 必须真的写出。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from relayhub.gateway import persist


def test_marks_are_coalesced_into_few_writes(tmp_path: Path):
    calls = []
    writer = persist.DeferredWriter(tmp_path / "x.json", lambda p: calls.append(1), interval=0.05)
    try:
        for _ in range(500):
            writer.mark()          # 请求路径只做这个
        assert calls == []          # 同步零写盘
        time.sleep(0.25)
        assert 1 <= len(calls) <= 6, f"500 次 mark 应合并成少数几次写，实际 {len(calls)}"
    finally:
        writer.stop()


def test_flush_writes_immediately(tmp_path: Path):
    calls = []
    writer = persist.DeferredWriter(tmp_path / "y.json", lambda p: calls.append(1), interval=10)
    try:
        writer.mark()
        assert calls == []
        writer.flush()
        assert len(calls) == 1
    finally:
        writer.stop()


def test_writer_is_shared_per_path(tmp_path: Path):
    path = tmp_path / "z.json"
    a = persist.writer_for(path, lambda p: None)
    b = persist.writer_for(path, lambda p: None)
    try:
        assert a is b              # 同一文件只有一个后台线程，避免多写互踩
    finally:
        a.stop()


def test_router_persist_is_non_blocking(tmp_path: Path):
    """路由器 _persist 绝不能同步调用 pool.save。"""
    from relayhub.gateway.pool import KeyPool, UpstreamKey
    from relayhub.gateway.router import KeyPoolRouter

    path = tmp_path / "pool.json"
    pool = KeyPool([UpstreamKey(key_id="k1", label="l", base_url="https://x", api_key="sk-a")])
    pool.save(path)
    router = KeyPoolRouter(pool, persist_path=path)

    saved = []
    pool.save = lambda p: saved.append(1)      # type: ignore[method-assign]
    started = time.perf_counter()
    for _ in range(200):
        router._persist()
    elapsed = (time.perf_counter() - started) * 1000
    assert saved == [], "请求路径不该同步写盘"
    assert elapsed < 50, f"200 次标记耗时 {elapsed:.1f}ms，太慢"
