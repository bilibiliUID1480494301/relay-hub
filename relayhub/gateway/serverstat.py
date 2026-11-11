"""管理面「服务器状态」：只读、无凭据、缺依赖就降级。

psutil 是**可选依赖**：有它出内存/负载细节，没有就只给磁盘与进程基础信息，
绝不因为少装一个包让管理面报错。
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any


def _memory() -> dict[str, Any]:
    try:
        import psutil  # noqa: PLC0415

        proc = psutil.Process()
        info = proc.memory_info()
        return {
            "source": "psutil",
            "rss_mb": round(info.rss / 1048576, 1),
            "vms_mb": round(info.vms / 1048576, 1),
            "system_total_mb": round(psutil.virtual_memory().total / 1048576, 1),
            "system_used_pct": psutil.virtual_memory().percent,
        }
    except Exception:  # noqa: BLE001
        return {"source": "none", "rss_mb": None, "vms_mb": None,
                "system_total_mb": None, "system_used_pct": None}


def _load() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        return None          # Windows 没有 getloadavg


def _dir_size(path: Path, limit: int = 20000) -> tuple[float, int]:
    total, count = 0, 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
                count += 1
            except OSError:
                continue
            if count >= limit:
                return round(total / 1048576, 1), count
    return round(total / 1048576, 1), count


def status(home: Path, started_at: float) -> dict[str, Any]:
    home = Path(home)
    disk: dict[str, Any] = {"path": str(home)}
    try:
        usage = shutil.disk_usage(home if home.exists() else Path.home())
        disk.update(
            total_gb=round(usage.total / 1073741824, 1),
            free_gb=round(usage.free / 1073741824, 1),
            used_pct=round(usage.used / usage.total * 100, 1) if usage.total else 0.0,
        )
    except OSError:
        disk.update(total_gb=None, free_gb=None, used_pct=None)

    size_mb, file_count = _dir_size(home) if home.exists() else (0.0, 0)
    return {
        "pid": os.getpid(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": platform.platform(terse=True),
        "uptime_s": int(time.time() - started_at) if started_at else None,
        "threads": threading.active_count(),
        "load1": _load(),
        "memory": _memory(),
        "disk": disk,
        "data": {"home": str(home), "size_mb": size_mb, "files": file_count},
    }
