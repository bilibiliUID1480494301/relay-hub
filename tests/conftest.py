# -*- coding: utf-8 -*-
"""测试级隔离：每个测试结束清空上游连接池。

`connpool` 是进程级 thread-local，测试跑在同一线程里，池会跨测试存活。
而夹具里的 `FakeUpstream` 是「起一个临时服务端、测试结束关掉」——
它关掉后端口会被操作系统复用给下一个夹具，池里指向同一 (host, port)
的旧连接就成了半开连接，复用即 `ConnectionAbortedError`。
生产里同理：上游容器重启 / 端口复用也会踩到同一个坑。

这里在测试边界强制清池，让每个测试面对干净的连接状态。生产侧对
「对端已 FIN」的防护在 `connpool._alive()` 的 MSG_PEEK 探测。
"""

from __future__ import annotations

import pytest

from relayhub.gateway import connpool


@pytest.fixture(autouse=True)
def _reset_upstream_connpool() -> None:
    connpool.clear()
    yield
    connpool.clear()
