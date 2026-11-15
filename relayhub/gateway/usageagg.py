"""用量聚合：按「客户端」或「直连 API 令牌」分桶。

口径（用户明确要求）：

  * **有身份头**的请求 → 归到对应客户端（如 deepseekharness）
  * **没有身份头**的请求 → 归到「直连 API 令牌」桶，**不进客户端维度**
  * 两个桶**都计入总用量与计费**——分账口径不影响收钱

因此 `group_by=client` 的返回里，客户端明细之和 ≠ 总量，差额就是直连部分。
这个差额必须显式给出来（`direct` 字段），不能让人去减。
"""

from __future__ import annotations

from typing import Any, Iterable

DIRECT_LABEL = "直连 API 令牌"


def _bucket() -> dict[str, Any]:
    return {"requests": 0, "ok": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0}


def _add(bucket: dict[str, Any], entry: dict[str, Any]) -> None:
    bucket["requests"] += 1
    if entry.get("ok"):
        bucket["ok"] += 1
    else:
        bucket["failed"] += 1
    bucket["tokens_in"] += int(entry.get("tokens_in") or 0)
    bucket["tokens_out"] += int(entry.get("tokens_out") or 0)
    bucket["cost"] += int(entry.get("cost") or 0)


def client_of(entry: dict[str, Any]) -> str:
    """从一条请求里取「客户端」标识；取不到返回空串（= 直连）。"""
    for key in ("ident_src", "ident_dev", "ident_user"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return ""


def aggregate(
    entries: Iterable[dict[str, Any]],
    *,
    since: float | None = None,
    group_by: str = "client",
) -> dict[str, Any]:
    """返回 {total, clients, direct, by_client}。

    `by_client` 只在 `group_by="client"` 时填充；`direct` 永远单独给。
    """
    total = _bucket()
    direct = _bucket()
    clients: dict[str, dict[str, Any]] = {}

    for entry in entries:
        ts = float(entry.get("ts") or 0)
        if since is not None and ts < since:
            continue
        _add(total, entry)
        name = client_of(entry)
        if not name:
            _add(direct, entry)
            continue
        if group_by == "client":
            _add(clients.setdefault(name, _bucket()), entry)

    rows = [
        {"client": name, **data}
        for name, data in sorted(clients.items(), key=lambda kv: -kv[1]["requests"])
    ]
    return {
        "total": total,
        "direct": direct,
        "by_client": rows,
        "note": f"「{DIRECT_LABEL}」不计入客户端明细，但已计入总量与计费",
    }
