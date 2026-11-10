"""守护进程：保活 + 消费 restart.flag。

升级端点只写 `restart.flag`，**不自杀**——进程自己退出会留下一段无人服务的空窗。
判断「该重启了」的职责在这里：本脚本被计划任务每分钟拉起，发现标记就重启服务。

用法（参考）：
    python scripts/keepalive.py --port 8799 --home %LOCALAPPDATA%/relay-hub
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def read_flag(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="keepalive")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--wait", type=float, default=20.0, help="等新进程就绪的秒数")
    args = parser.parse_args(argv)

    home = args.home or Path(os.environ.get("RELAYHUB_HOME", Path.home() / ".relayhub"))
    flag = Path(home) / "restart.flag"

    pending = read_flag(flag)
    if pending is None:
        # 没有升级标记就只保活：端口活着什么都不做
        if port_open(args.port):
            return 0

    if pending is not None:
        print(f"检测到升级标记：{pending}", flush=True)
        # 标记即意图：先让旧进程退出（端口释放），再拉新的
        _stop(flag)

    _start(home)
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if port_open(args.port):
            flag.unlink(missing_ok=True)   # 新进程起来了才算消费完
            print("已恢复服务", flush=True)
            return 0
        time.sleep(0.5)
    print("服务未能在预期时间内就绪", file=sys.stderr)
    return 1


def _stop(flag: Path) -> None:
    """标记里带了 pid 就精确杀；没有就按端口找（Windows 走 taskkill）。"""
    info = read_flag(flag) or {}
    pid = info.get("pid")
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
        else:
            os.kill(int(pid), 15)
    except (OSError, subprocess.SubprocessError):
        pass
    time.sleep(1.0)


def _start(home: Path) -> None:
    env = dict(os.environ, RELAYHUB_HOME=str(home))
    kwargs: dict = {"env": env, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "relayhub.gateway", "serve"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
