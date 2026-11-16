"""原子 JSON 落盘：同目录临时文件 + os.replace。

为什么必须原子：网关数据面每个请求都会重写 tokens/pool/users 等共享状态
文件，管理面（另一进程）与测试又在并发读它们。非原子写会让读者「撕读」——
读到空文件或半截 JSON（CI 上 Python 3.10 的
test_streaming_usage_reaches_the_token 因此挂过：轮询读到正在重写的
tokens.json）。

os.replace 在同一文件系统内是原子的：读者要么看到完整的旧文件，
要么看到完整的新文件，永远看不到中间态。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


def replace_with_retry(src: Path, dst: Path, attempts: int = 6, backoff: float = 0.003) -> None:
    """os.replace 带重试：Windows 上读者 open 与替换撞车会抛共享冲突（WinError 5）。

    冲突窗口只有微秒级，但网关每个请求都在重写 tokens/pool，撞上一次就是一个 500
    （`test_no_tear_reads_under_concurrent_writes` 在满载时约每三次复现一次：
    写线程第一次 save 就撞上，直接退出）。等几毫秒就能过去；
    真正不可恢复的（目录没了、只读）照样抛出去。
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(backoff * (attempt + 1))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """JSON 原子落盘：先写同目录临时文件，再 os.replace 顶替正式文件。

    临时文件名带 pid + 线程 id：管理面与网关是两个进程，都可能保存同一份
    文件，固定临时名会让两个进程互相写坏对方的中间态。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident():x}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        replace_with_retry(tmp, path)
    finally:
        # replace 成功后 tmp 已不存在；写失败时清理，避免残留垃圾
        if tmp.exists():
            tmp.unlink()


def ensure_private(path: Path) -> None:
    """把已存在的文件收紧到 0600。追加写无法先写临时文件，只能写后收紧。"""
    try:
        if os.name == "posix":
            os.chmod(Path(path), 0o600)
    except OSError:
        pass
