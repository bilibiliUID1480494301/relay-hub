"""用户管理 CLI：初始化站点管理员 / 强制重置密码 / 列出用户。

为什么单独一个命令：
  * 管理面（`admin`）登录靠 users.json 里 role=admin 的账号；users.json 为空时
    面板实际是锁死的，之前**没有任何命令能建第一个管理员**。
  * 「部署时站长密码随机生成、只显示一次」：`init` 生成随机密码，只往 stdout
    打一次，不落盘明文。
  * 「服务端系统管理员通过指令强制重置密码」：`reset-password` 不受管理面状态
    影响（面板进不去也能救场）。

用法::

    relayhub.gateway users init [--username admin] [--password X] [--force]
    relayhub.gateway users reset-password [--username admin] [--password X]
    relayhub.gateway users ls
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

from .. import paths
from . import audit as audit_module
from .users import UserError, UserPool, hash_password

# 与 UserPool.register 同一门槛：密码 >= 8 位。
MIN_PASSWORD = 8
DEFAULT_ADMIN = "admin"


def _random_password() -> str:
    """16 字符的随机密码（token_urlsafe(12)），满足 >=8 且够随机。"""
    return secrets.token_urlsafe(12)


def _find(pool: UserPool, username: str):
    return pool.find_by_name(username)


def _announce(action: str, username: str, password: str) -> None:
    print(f"=== {action}（密码只显示这一次，请立即保存）===")
    print(f"  用户名：{username}")
    print(f"  密码  ：{password}")
    print("登录管理面：浏览器打开管理面地址，用上面的账号密码登录。")
    print(f"忘记密码：relayhub.gateway users reset-password --username {username}")


def cmd_init(args: argparse.Namespace) -> int:
    path = paths.users_path()
    pool = UserPool.load(path)
    existing = _find(pool, args.username)
    if existing is not None and existing.role == "admin" and not args.force:
        print(
            f"已存在管理员 {args.username}；重置密码用 reset-password，"
            "或加 --force 强制重置",
            file=sys.stderr,
        )
        return 1
    password = args.password or _random_password()
    if len(password) < MIN_PASSWORD:
        print(f"密码至少 {MIN_PASSWORD} 位", file=sys.stderr)
        return 2
    try:
        if existing is not None:
            existing.password_hash = hash_password(password)
            existing.role = "admin"
            existing.enabled = True
            if existing.quota == 0:
                existing.quota = -1  # 站长不限量
            action = "管理员已重置"
            event = "user.init-reset"
        else:
            user = pool.register(args.username, password, role="admin")
            user.quota = -1  # 站长不限量（-1 = 不过计费）
            action = "管理员已就绪"
            event = "user.init"
        pool.save(path)
    except UserError as exc:
        print(f"创建失败：{exc}", file=sys.stderr)
        return 2
    audit_module.record(event, user=args.username, via="cli", role="admin")
    _announce(action, args.username, password)
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    path = paths.users_path()
    pool = UserPool.load(path)
    user = _find(pool, args.username)
    if user is None:
        print(
            f"找不到用户 {args.username}（先用 users init 建管理员）", file=sys.stderr
        )
        return 1
    password = args.password or _random_password()
    if len(password) < MIN_PASSWORD:
        print(f"密码至少 {MIN_PASSWORD} 位", file=sys.stderr)
        return 2
    user.password_hash = hash_password(password)
    pool.save(path)
    audit_module.record("user.reset-password", user=args.username, via="cli")
    _announce("密码已重置", args.username, password)
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    pool = UserPool.load(paths.users_path())
    if not pool.users:
        print("（还没有用户；先跑 users init）")
        return 0
    print(f"{'用户名':<18}{'角色':<8}{'启用':<6}{'额度':<10}{'已用'}")
    for u in pool.users:
        quota = "∞" if u.quota < 0 else str(u.quota)
        print(
            f"{u.username:<18}{u.role:<8}{'是' if u.enabled else '否':<6}"
            f"{quota:<10}{u.used}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(
        prog="relayhub.gateway users", description="用户/管理员管理"
    )
    sub = parser.add_subparsers(dest="cmd")
    p_init = sub.add_parser("init", help="初始化站点管理员（随机密码，只显示一次）")
    p_init.add_argument("--username", default=DEFAULT_ADMIN)
    p_init.add_argument("--password", default="", help="指定密码（默认随机；>=8 位）")
    p_init.add_argument("--force", action="store_true", help="已存在管理员时也强制重置")
    p_init.set_defaults(func=cmd_init)

    p_reset = sub.add_parser(
        "reset-password", help="强制重置某用户密码（服务端救援用）"
    )
    p_reset.add_argument("--username", default=DEFAULT_ADMIN)
    p_reset.add_argument("--password", default="", help="指定密码（默认随机；>=8 位）")
    p_reset.set_defaults(func=cmd_reset)

    p_ls = sub.add_parser("ls", help="列出用户")
    p_ls.set_defaults(func=cmd_ls)

    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    return int(func(args) or 0)
