"""请求明细按日清理 / daily request-log retention."""

from __future__ import annotations

import time
from pathlib import Path

from relayhub.gateway import reqlog


def test_retention_prunes_old_days(tmp_path):
    log = tmp_path / "requests.jsonl"
    now = time.time()
    # 手工造三天的按日文件：今天、3 天前、40 天前
    for age, body in ((0, '{"ts":1}'), (3 * 86400, '{"ts":2}'), (40 * 86400, '{"ts":3}')):
        p = reqlog._daily_path(log, now - age)
        Path(p).write_text(body, encoding="utf-8")

    reqlog.set_retention(30)
    try:
        reqlog.record(log, ts=now, ok=True)  # record 内部触发清理
        files = [f.name for f in reqlog._all_files(log)]
        today = reqlog._daily_path(log, now).name
        d3 = reqlog._daily_path(log, now - 3 * 86400).name
        d40 = reqlog._daily_path(log, now - 40 * 86400).name
        assert today in files and d3 in files and d40 not in files
    finally:
        reqlog.set_retention(0)


def test_retention_zero_keeps_everything(tmp_path):
    log = tmp_path / "requests.jsonl"
    now = time.time()
    old = reqlog._daily_path(log, now - 400 * 86400)
    old.write_text("{}", encoding="utf-8")
    reqlog.set_retention(0)
    reqlog.record(log, ts=now, ok=True)
    assert old.exists()


def test_retention_applies_immediately_after_change(tmp_path):
    """改保留期必须下一次写入就生效，且不受「同进程之前已 prune 过」影响。

    回归：节流戳 `_last_prune` 是模块全局，前一个用例刚清理过就会把本用例的
    record 挡在节流窗口外，旧文件清不掉 —— 单跑通过、全量跑失败的顺序依赖。
    产品侧的正确行为是 set_retention 重置节流戳（管理员改配置应立即生效）。
    """
    log = tmp_path / "requests.jsonl"
    now = time.time()
    old = reqlog._daily_path(log, now - 40 * 86400)
    old.write_text("{}", encoding="utf-8")

    # 先制造一次「本进程刚刚 prune 过」的状态
    reqlog.set_retention(30)
    reqlog.record(log, ts=now, ok=True)
    assert not old.exists()

    # 再放一个过期文件，第二次配置 + 记录仍必须立刻清掉它
    stale = reqlog._daily_path(log, now - 50 * 86400)
    stale.write_text("{}", encoding="utf-8")
    reqlog.set_retention(30)
    reqlog.record(log, ts=now, ok=True)
    assert not stale.exists(), "改保留期后应立即生效，不该被节流窗口挡住"

    reqlog.set_retention(0)


def test_prune_not_blocked_by_backwards_clock(tmp_path):
    """墙钟回拨（NTP 校正/改时间）不该把清理锁死。

    旧条件 `now - _last_prune < 3600` 在负间隔时恒真，等于永久停掉清理。
    """
    log = tmp_path / "requests.jsonl"
    now = time.time()
    reqlog.set_retention(30)
    # 先正常推进一次，把节流戳设到「未来」
    reqlog.record(log, ts=now, ok=True)

    stale = reqlog._daily_path(log, now - 40 * 86400)
    stale.write_text("{}", encoding="utf-8")
    # 时钟回拨：传一个比节流戳更早的 ts，仍应触发清理
    reqlog.record(log, ts=now - 7200, ok=True)
    assert not stale.exists(), "时钟回拨后清理被锁死了"

    reqlog.set_retention(0)
