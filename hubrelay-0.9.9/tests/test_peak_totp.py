# -*- coding: utf-8 -*-
"""#18 分时计费 + #19 TOTP 动态管理密码。"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path

from relayhub.gateway.admin import AdminServer
from relayhub.gateway.toip import totp_now
from relayhub.gateway.users import User, UserPool


def _ts_at_hour(hour: int) -> float:
    """今天日期 + 指定小时（hour 判断与日期无关，无时间腐烂风险）。"""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))


def test_time_multiplier_peak_and_off() -> None:
    pool = UserPool(peak_hours=[9, 10, 11, 12, 13, 14], peak_multiplier=1.5)
    assert pool.time_multiplier(_ts_at_hour(10)) == 1.5  # 峰
    assert pool.time_multiplier(_ts_at_hour(3)) == 1.0   # 谷
    off = UserPool()
    assert off.time_multiplier(_ts_at_hour(10)) == 1.0   # 未启用
    assert off.time_multiplier() == 1.0


def test_cost_of_includes_time_rate() -> None:
    pool = UserPool(
        users=[User(user_id="u1", username="a", password_hash="x", quota=10_000)],
        price_in_per_1k=2.0, price_out_per_1k=0.0,
        peak_hours=[9, 10, 11], peak_multiplier=2.0,
    )
    user = pool.users[0]
    peak = pool.cost_of(user, 1000, 0, ts=_ts_at_hour(10))
    off = pool.cost_of(user, 1000, 0, ts=_ts_at_hour(3))
    assert peak == 4 and off == 2  # 2 点标价 × 峰 2.0 / 谷 1.0


def test_peak_config_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "users.json"
    pool = UserPool(peak_hours=[22, 23, 0], peak_multiplier=1.2)
    pool.save(p)
    restored = UserPool.load(p)
    assert restored.peak_hours == {0, 22, 23}
    assert restored.peak_multiplier == 1.2


def test_admin_static_token_ok_without_totp(tmp_path: Path) -> None:
    server = AdminServer(("127.0.0.1", 0), tmp_path / "pool.json", token="adm")
    try:
        assert server._token_ok("adm")
        assert not server._token_ok("wrong")
    finally:
        server.server_close()


def test_admin_totp_mode(tmp_path: Path) -> None:
    secret = base64.b32encode(os.urandom(10)).decode()
    server = AdminServer(
        ("127.0.0.1", 0), tmp_path / "pool.json", token="adm", totp_secret=secret
    )
    try:
        code = totp_now(base64.b32decode(secret))
        assert server._token_ok("adm:" + code)          # token:动态码
        assert not server._token_ok("adm")              # 缺动态码
        assert not server._token_ok("adm:000000")       # 错码
        assert not server._token_ok("bad:" + code)      # 静态段错
    finally:
        server.server_close()


def test_admin_totp_bad_secret_rejects(tmp_path: Path) -> None:
    server = AdminServer(
        ("127.0.0.1", 0), tmp_path / "pool.json", token="adm", totp_secret="!!not-base32!!"
    )
    try:
        assert not server._token_ok("adm:123456")
    finally:
        server.server_close()
