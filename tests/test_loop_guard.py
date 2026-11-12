"""防自环不能误伤：8 个客户端并发发一模一样的请求必须全部放行。"""

from __future__ import annotations

from relayhub.gateway.service import LoopGuard


def _fp(tag: str = "same") -> str:
    return f"fp-{tag}"


def test_eight_identical_direct_requests_are_not_a_loop():
    """直连（无 Via/Hops）客户端并发同 prompt 是正常负载，不是环路。"""
    guard = LoopGuard()
    results = [guard.acquire(_fp(), relayed=False) for _ in range(8)]
    assert all(results), f"第 {results.index(False)+1} 个被误判成环路"


def test_relayed_requests_still_trip_early():
    """带 Via/Hops 的链路仍按紧阈值判环——那才是真的可能绕圈。"""
    guard = LoopGuard()
    outcomes = [guard.acquire(_fp("relayed"), relayed=True) for _ in range(4)]
    assert outcomes[:3] == [True, True, True]
    assert outcomes[3] is False


def test_release_frees_slot():
    guard = LoopGuard()
    for _ in range(3):
        assert guard.acquire(_fp("rel"), relayed=True)
    assert guard.acquire(_fp("rel"), relayed=True) is False
    guard.release(_fp("rel"))
    assert guard.acquire(_fp("rel"), relayed=True) is True
