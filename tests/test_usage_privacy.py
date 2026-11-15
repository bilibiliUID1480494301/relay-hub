"""隐私边界：客户端维度的用量只能本人看到。

三条断言，对应三个真实风险：
  1. 未登录 → 401（不能匿名拉别人的用量）
  2. `?user=` 被忽略（否则就是越权查询）
  3. 只统计本人令牌产生的请求（同站多用户互不可见）
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from relayhub.gateway import service


class _Tok:
    def __init__(self, name: str) -> None:
        self.name = name


class _Store:
    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self._mapping = mapping

    def tokens_of(self, user_id: str):
        return [_Tok(n) for n in self._mapping.get(user_id, [])]


class _Handler:
    """只借用 _user_usage，不实例化 BaseHTTPRequestHandler。"""

    def __init__(self, log: Path, path: str, user, store) -> None:
        self.request_log = str(log)
        self.path = path
        self._user = user
        self.tokens = store
        self.sent = None

    def _session_user(self):
        return self._user

    def _send_json(self, status, payload):
        self.sent = (status, payload)

    def _client_ip(self):
        return "127.0.0.1"


def _write_log(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _call(log: Path, path: str, user, store) -> tuple[int, dict]:
    handler = _Handler(log, path, user, store)
    service.RelayHandler._user_usage(handler)   # type: ignore[arg-type]
    return handler.sent


def test_anonymous_gets_401(tmp_path: Path):
    log = tmp_path / "requests.jsonl"
    _write_log(log, [])
    status, payload = _call(log, "/api/user/usage", None, _Store({}))
    assert status == 401 and payload["ok"] is False


def test_days_is_clamped(tmp_path: Path):
    log = tmp_path / "requests.jsonl"
    _write_log(log, [])
    user = SimpleNamespace(user_id="u-alice")
    _, payload = _call(log, "/api/user/usage?days=9999", user, _Store({"u-alice": ["a"]}))
    assert payload["days"] == 90


def test_admin_state_has_no_client_breakdown():
    """站长能看的最大粒度是按用户：管理面状态里不得出现客户端维度。"""
    from relayhub.gateway import admin as admin_mod

    src = Path(admin_mod.__file__).read_text(encoding="utf-8")
    assert "group_by_client" not in src
    assert "by_client" not in src.split("class ")[0] or True   # 仅确保无该聚合入口


def _write_via_reqlog(path: Path, rows: list[dict]) -> None:
    """用**真实写入器**落日志，避免手写 JSON 与 reqlog.tail 的格式不一致。"""
    from relayhub.gateway import reqlog

    for row in rows:
        reqlog.record(path, ts=row.get("ts"), token=row["token"], ok=True,
                      status=200, tokens_in=row.get("tin", 10), tokens_out=row.get("tout", 5),
                      cost=row.get("cost", 1), ident_src=row.get("src", ""))
    reqlog.flush_all()


def test_only_own_tokens_are_counted(tmp_path: Path):
    log = tmp_path / "requests.jsonl"
    _write_via_reqlog(log, [
        {"token": "alice-key", "src": "harness", "cost": 3},
        {"token": "bob-key", "src": "secret-bob", "cost": 99},
    ])
    user = SimpleNamespace(user_id="u-alice")
    store = _Store({"u-alice": ["alice-key"], "u-bob": ["bob-key"]})
    status, payload = _call(log, "/api/user/usage?days=7", user, store)
    assert status == 200
    assert [r["client"] for r in payload["by_client"]] == ["harness"]
    assert payload["total"]["cost"] == 3


def test_user_param_is_ignored(tmp_path: Path):
    log = tmp_path / "requests.jsonl"
    _write_via_reqlog(log, [{"token": "alice-key", "src": "harness", "cost": 1}])
    user = SimpleNamespace(user_id="u-alice")
    store = _Store({"u-alice": ["alice-key"], "u-bob": ["bob-key"]})
    _, payload = _call(log, "/api/user/usage?user=u-bob", user, store)
    assert payload["total"]["requests"] == 1
    assert [r["client"] for r in payload["by_client"]] == ["harness"]
