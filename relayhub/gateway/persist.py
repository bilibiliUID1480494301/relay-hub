"""延迟落盘：把「每请求全量写盘」压成「每 N 毫秒最多一次」。

问题：路由器在请求结束时 `pool.save()`，那是「全量序列化 + 密封 + fsync +
os.replace + 全局锁」。请求越多写得越多，而且全在 GIL 与锁下排队——
这是吞吐卡在平台期的教科书成因（CPU 有余量、RPS 不动、p50 线性涨）。

取舍：最多丢一个 flush 窗口的**用量计数**（默认 150ms）。计费凭证在
reqlog（每请求追加写）里，不受影响；号池结构变更（增删 Key）走显式 flush。
"""

from __future__ import annotations

import atexit
import threading
import time
from pathlib import Path
from typing import Callable

DEFAULT_INTERVAL = 0.15

_writers: dict[str, "DeferredWriter"] = {}
_reg_lock_inner = threading.Lock()
_registry_lock = threading.Lock()


class DeferredWriter:
    def __init__(self, path: Path, saver: Callable[[Path], None], interval: float = DEFAULT_INTERVAL) -> None:
        self.path = Path(path)
        self.saver = saver
        self.interval = interval
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self.writes = 0
        self.failures = 0
        self._thread: threading.Thread | None = None

    def _ensure_thread(self) -> None:
        """**懒启动**：没有写入就不起线程。

        早期版本在构造时就起线程，测试里每个临时目录一个 writer →
        单进程几百个轮询线程，全量从 3 分钟涨到 37 分钟。
        """
        if self._thread is not None:
            return
        with _reg_lock_inner:
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="rh-persist", daemon=True)
                self._thread.start()

    def mark(self) -> None:
        """请求路径只做这一件事：置脏标记，不做任何 IO。"""
        self._dirty.set()
        self._ensure_thread()

    def flush(self) -> None:
        """立即写一次（退出、管理面写操作前调用）。"""
        self._dirty.clear()
        self._save()

    def _save(self) -> None:
        try:
            self.saver(self.path)
            self.writes += 1
        except Exception:  # noqa: BLE001
            self.failures += 1

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            if self._dirty.is_set():
                self._dirty.clear()
                self._save()

    def stop(self) -> None:
        self._stop.set()
        if self._dirty.is_set():
            self.flush()


def writer_for(path: Path, saver: Callable[[Path], None], interval: float = DEFAULT_INTERVAL) -> DeferredWriter:
    key = str(Path(path).resolve())
    with _registry_lock:
        existing = _writers.get(key)
        if existing is not None:
            return existing
        created = DeferredWriter(Path(path), saver, interval)
        _writers[key] = created
        return created


def flush_all() -> None:
    with _registry_lock:
        writers = list(_writers.values())
    for writer in writers:
        writer.flush()


def stats() -> list[dict[str, object]]:
    with _registry_lock:
        return [
            {"path": str(w.path), "writes": w.writes, "failures": w.failures}
            for w in _writers.values()
        ]


atexit.register(flush_all)
