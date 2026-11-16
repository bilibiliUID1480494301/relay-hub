"""原子落盘：Windows 共享冲突必须能重试过去，真失败仍要抛。"""

from __future__ import annotations

import os

import pytest

from relayhub.gateway import atomicio


def test_replace_survives_sharing_violation(tmp_path, monkeypatch):
    """读者 open 与 os.replace 撞车（WinError 5）是瞬时的：重试几次就该过去。

    网关每个请求都在重写 tokens/pool，撞一次就是一个 500；
    `test_no_tear_reads_under_concurrent_writes` 满载时约每三次复现一次。
    """
    src = tmp_path / "a.tmp"
    dst = tmp_path / "a.json"
    src.write_text("payload", encoding="utf-8")
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "拒绝访问")
        return real_replace(a, b)

    monkeypatch.setattr(atomicio.os, "replace", flaky)
    atomicio.replace_with_retry(src, dst, backoff=0)

    assert dst.read_text(encoding="utf-8") == "payload"
    assert calls["n"] == 3


def test_replace_gives_up_and_raises(tmp_path, monkeypatch):
    """真的替换不了（只读目标、权限不足）不能吞掉：调用方要知道写失败了。"""
    src = tmp_path / "a.tmp"
    dst = tmp_path / "a.json"
    src.write_text("payload", encoding="utf-8")

    def always_denied(a, b):
        raise PermissionError(13, "拒绝访问")

    monkeypatch.setattr(atomicio.os, "replace", always_denied)
    with pytest.raises(PermissionError):
        atomicio.replace_with_retry(src, dst, attempts=2, backoff=0)


def test_write_json_atomic_uses_the_retry(tmp_path, monkeypatch):
    """write_json_atomic 走的是带重试的那条路，不是裸 os.replace。"""
    target = tmp_path / "state.json"
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "拒绝访问")
        return real_replace(a, b)

    monkeypatch.setattr(atomicio.os, "replace", flaky)
    atomicio.write_json_atomic(target, {"ok": True})

    assert calls["n"] == 2
    assert '"ok": true' in target.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp")), "临时文件不能残留"
