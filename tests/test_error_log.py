"""500 必须可查：errors.log 有堆栈，且**不含 query 里的凭据**。"""

from __future__ import annotations

from pathlib import Path

from relayhub.gateway import reqlog, service


class _Obj:
    """只提供 _note_unhandled 需要的几个属性，不碰 BaseHTTPRequestHandler。"""

    def __init__(self, log_path: Path, path: str) -> None:
        self.request_log = str(log_path)
        self.path = path
        self._current_request_id = "req-1"

    def log_error(self, *args, **kwargs) -> None:
        pass

    def _client_ip(self) -> str:
        return "127.0.0.1"


def _call(log: Path, path: str, exc: BaseException) -> None:
    service.RelayHandler._note_unhandled(_Obj(log, path), exc)   # type: ignore[arg-type]


def test_unhandled_writes_stack_and_strips_query(tmp_path: Path):
    log = tmp_path / "requests.jsonl"
    try:
        raise ValueError("boom")
    except ValueError as exc:
        _call(log, "/v1/chat/completions?token=SECRET-TOKEN", exc)

    text = (tmp_path / "errors.log").read_text(encoding="utf-8")
    assert "Traceback" in text and "ValueError: boom" in text
    assert "SECRET-TOKEN" not in text          # query 里的凭据不落盘
    assert "/v1/chat/completions" in text      # 路径本身保留，便于定位


def test_unhandled_also_records_request_log(tmp_path: Path):
    log = tmp_path / "requests.jsonl"
    try:
        raise RuntimeError("kaboom")
    except RuntimeError as exc:
        _call(log, "/v1/messages", exc)
    reqlog.flush_all()

    entries = reqlog.tail(log, limit=10)
    assert entries and entries[-1]["status"] == 500
    assert "unhandled" in entries[-1]["reason"]
