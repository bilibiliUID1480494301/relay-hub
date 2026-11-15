"""用户自建 TOIP 通行证：归属字段与失效规则。

只固化数据层约定，不动路由——路由改错会影响所有鉴权路径。
"""

from __future__ import annotations

from relayhub.gateway import toip


def test_ticket_defaults_to_station_owned():
    """没写 owner_user_id 的旧数据必须继续表示「站点级」。"""
    ticket = toip.Ticket(ticket_id="t1", ticket_hash="h", ticket_hint="…", name="n")
    assert ticket.owner_user_id == ""


def test_user_ticket_carries_owner():
    ticket = toip.Ticket(ticket_id="t2", ticket_hash="h", ticket_hint="…",
                         name="harness", owner_user_id="u-1")
    assert ticket.owner_user_id == "u-1"


def test_expired_ticket_detected_independently_of_owner():
    ticket = toip.Ticket(ticket_id="t3", ticket_hash="h", ticket_hint="…", name="n",
                         owner_user_id="u-1", expires_at=100.0)
    assert ticket.is_expired(clock=lambda: 200.0) is True
    assert ticket.is_expired(clock=lambda: 50.0) is False


def _store(tmp_path):
    return toip.TicketStore(tmp_path / "tickets.json")


def test_issue_for_user_persists_owner(tmp_path):
    store = _store(tmp_path)
    ticket, secret = toip.issue_for_user(store, "u-1", "我的 harness", ttl=0)
    assert secret.startswith(toip.TICKET_PREFIX)
    assert ticket.owner_user_id == "u-1"
    # 重开文件：字段必须还在（asdict + 字段过滤本来就会带过去）
    again = _store(tmp_path)
    again._refresh() if hasattr(again, "_refresh") else None
    saved = [t for t in again.tickets if t.name == "我的 harness"]
    assert saved and saved[0].owner_user_id == "u-1"


def test_issue_for_user_requires_user_id(tmp_path):
    store = _store(tmp_path)
    try:
        toip.issue_for_user(store, "", "x")
    except toip.ToipError:
        return
    raise AssertionError("没有 user_id 应当拒绝")


def test_revoke_owner_only_touches_its_own(tmp_path):
    store = _store(tmp_path)
    mine, _ = toip.issue_for_user(store, "u-1", "mine")
    theirs, _ = toip.issue_for_user(store, "u-2", "theirs")
    station, _ = toip.make_ticket(name="站点级")
    store.add(station)

    assert toip.revoke_owner(store, "u-1") == 1
    names = {t.name for t in store.tickets}
    assert "mine" not in names
    assert "theirs" in names and "站点级" in names   # 别人的、站点级的都不受影响


def test_join_with_user_ticket_attributes_token(tmp_path):
    """用户自建票换出的令牌必须带 user_id —— 否则钱记不到他头上。"""
    from relayhub.gateway.tokens import TokenStore

    station_path = tmp_path / "station.json"
    toip.save_station(station_path, toip.StationIdentity.create(name="t"))

    tickets = toip.TicketStore(tmp_path / "tickets.json")
    tokens = TokenStore(tmp_path / "tokens.json")
    service = toip.ToipService(tickets, tokens, station_path)

    ticket, secret = toip.issue_for_user(tickets, "u-9", "我的 harness")
    result = service.join(ticket=secret, name="dev", plugin_id="dsh", ip="127.0.0.1")
    assert result.token.user_id == "u-9"


def test_join_with_station_ticket_leaves_token_unowned(tmp_path):
    from relayhub.gateway.tokens import TokenStore

    station_path = tmp_path / "station.json"
    toip.save_station(station_path, toip.StationIdentity.create(name="t"))
    tickets = toip.TicketStore(tmp_path / "tickets.json")
    tokens = TokenStore(tmp_path / "tokens.json")
    service = toip.ToipService(tickets, tokens, station_path)

    record, secret = toip.make_ticket(name="站点级")
    tickets.add(record)
    result = service.join(ticket=secret, name="dev2", plugin_id="dsh", ip="127.0.0.1")
    assert result.token.user_id == ""      # 站点级不参与用户计费，行为不变
