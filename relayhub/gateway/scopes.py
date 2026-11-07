"""管理面权限（资源:动作）。

**校验必须在服务端**：客户端隐藏按钮只是体验层，curl 一样能打。
当前 admin/viewer 拿的是角色默认集；逐用户授予（scopes 字段）在下一步接。
"""

from __future__ import annotations

ALL_SCOPES: tuple[str, ...] = (
    "channel:read", "channel:write", "channel:reset_cooldown",
    "token:read", "token:write",
    "user:read", "role:grant",
    "usage:read",
    "logs:read", "logs:read_raw",
    "site:version",
    "config:read", "config:write", "config:export",
)

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "owner": frozenset(ALL_SCOPES),
    # 管理员默认最小：能看，不能改
    "admin": frozenset({"channel:read", "token:read", "user:read", "usage:read", "logs:read", "config:read"}),
    "viewer": frozenset({"usage:read", "logs:read"}),
}

# 写操作按资源声明所需 scope：路由层统一查，别在每个 handler 里手写 if
WRITE_SCOPES: dict[str, str] = {
    "keys": "channel:write",
    "tokens": "token:write",
    "users": "role:grant",
    "codes": "token:write",
    "policy": "config:write",
    "settings": "config:write",
    "webui": "config:write",
    "announcement": "config:write",
    "fingerprint": "channel:read",
    "probe": "channel:read",
    "newapi": "config:read",
    "pricing": "config:read",
}


def allowed(role: str, scope: str, extra: list[str] | None = None) -> bool:
    """角色默认集 + 该账号被单独授予的 scope（只能加，不能超出 ALL_SCOPES）。"""
    if role == "owner":
        return True
    if scope in ROLE_SCOPES.get(role or "viewer", ROLE_SCOPES["viewer"]):
        return True
    return bool(extra) and scope in extra and scope in ALL_SCOPES
