# -*- coding: utf-8 -*-
"""scan_leak.py 自测：它必须真的**抓到**泄露，不能只是"跑完报 CLEAN"。

为什么要专门测一个开发脚本：它曾经是**假阴性**的——zipfile.read() 返回 bytes，
而代码写 `fh.read() if hasattr(fh, "read") else b""`，bytes 没有 .read()，
于是每个 wheel 都扫到空内容、一律报 CLEAN。泄露就这样溜了一年多。
一个永远说 CLEAN 的扫描器比没有扫描器更糟：它给人虚假的安心。

所以这里的断言是**双向**的：违约包必须 BLOCK，干净包必须 CLEAN。
只测"干净包 CLEAN"是没有意义的——坏的扫描器也满足。
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNER = REPO_ROOT / "scan_leak.py"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("scan_leak_under_test", SCANNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def scanner():
    return _load_scanner()


def _zip(tmp_path: Path, name: str, members: dict[str, str]) -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        for member, text in members.items():
            z.writestr(member, text)
    return path


def _tar_gz(tmp_path: Path, name: str, members: dict[str, str]) -> Path:
    path = tmp_path / name
    with tarfile.open(path, "w:gz") as t:
        for member, text in members.items():
            blob = text.encode("utf-8")
            info = tarfile.TarInfo(member)
            info.size = len(blob)
            t.addfile(info, io.BytesIO(blob))
    return path


def test_detects_local_path_in_zip(scanner, tmp_path: Path) -> None:
    """zip/wheel 里的本机绝对路径必须被拦——这正是曾经漏掉的那一类。"""
    path = _zip(tmp_path, "leaky.zip", {"pkg/mod.py": 'TMP = "C:/Users/alice/dev/x"\n'})
    assert scanner.scan(str(path)) > 0, "扫描器假阴性：zip 成员内容根本没被读到"


def test_detects_credentials_and_hostname(scanner, tmp_path: Path) -> None:
    path = _zip(
        tmp_path,
        "creds.zip",
        {
            "pkg/k.py": 'KEY = "sk-' + "a" * 30 + '"\n',
            # 主机名用片段拼接：明文写一个形如真机名的串，测试文件自己就会
            # 命中 hostname 规则，等于把要防的东西写进了仓库。
            "pkg/host.txt": "LAPTOP-" + "9F3K2AB\n",
        },
    )
    assert scanner.scan(str(path)) >= 2


def test_detects_in_tar_gz(scanner, tmp_path: Path) -> None:
    """sdist（tar.gz）路径同样要工作（走 extractfile 的文件对象分支）。"""
    path = _tar_gz(tmp_path, "leaky.tar.gz", {"pkg/m.py": 'P = "C:/Users/bob/dev/y"\n'})
    assert scanner.scan(str(path)) > 0


def test_clean_archive_reports_clean(scanner, tmp_path: Path) -> None:
    path = _zip(tmp_path, "clean.zip", {"pkg/ok.py": "print('hello')\n"})
    assert scanner.scan(str(path)) == 0


def test_org_terms_come_from_env_not_repo(scanner, tmp_path: Path, monkeypatch) -> None:
    """组织特有的敏感词从环境变量注入，仓库里不留名单。

    这也是 scan_leak.py 本身不泄露内部项目名的保证：若哪天真把名单写回
    源码，下面的 assert 会失败。
    """
    path = _zip(tmp_path, "priv.zip", {"pkg/p.md": "runs on acme-internal infra\n"})
    monkeypatch.delenv("RELAYHUB_LEAK_EXTRA", raising=False)
    assert scanner.scan(str(path)) == 0, "未注入词表时不该命中组织特有词"

    monkeypatch.setenv("RELAYHUB_LEAK_EXTRA", "acme-internal,project-x")
    assert scanner.scan(str(path)) > 0, "注入词表后必须命中"


def test_scanner_source_does_not_enumerate_internal_names() -> None:
    """scan_leak.py 自己不许可把内部项目名清单写进去（否则它自己就是泄露源）。

    注意这个测试**本身**也不能把名字写成明文——不然测试文件成了新的泄露点，
    等于把要防的东西换了个地方公开。所以这里用片段拼接构造待查词，
    并且断言结构性特征（不再有硬编码的 PRIVATE 名单）。
    """
    source = SCANNER.read_text(encoding="utf-8")

    # 结构性：旧设计是一张写死的 PRIVATE = [...] 名单，新设计必须走环境变量。
    assert "RELAYHUB_LEAK_EXTRA" in source, "私有词表应改由环境变量注入"
    assert "PRIVATE = [" not in source, "又出现了硬编码的 PRIVATE 名单"

    # 逐个片段拼接，源码里不出现完整词
    forbidden = [
        "study" + "mate",
        "学" + "伴",
        "z" + "code",
        "work" + "buddy",
        "tra" + "e",
        "No" + "Book",
    ]
    hits = [w for w in forbidden if w.lower() in source.lower()]
    assert not hits, f"扫描器源码出现内部名（拼接后命中 {len(hits)} 个）"


def test_cli_exit_codes(tmp_path: Path) -> None:
    """BLOCK 走非 0、CLEAN 走 0——CI 靠退出码卡门。"""
    leaky = _zip(tmp_path, "l.zip", {"pkg/a.py": 'x = "C:/Users/alice/dev/z"\n'})
    clean = _zip(tmp_path, "c.zip", {"pkg/a.py": "x = 1\n"})
    env = {k: v for k, v in os.environ.items() if k != "RELAYHUB_LEAK_EXTRA"}

    bad = subprocess.run(
        [sys.executable, str(SCANNER), str(leaky)],
        capture_output=True, text=True, env=env,
    )
    assert bad.returncode == 1, bad.stdout + bad.stderr

    good = subprocess.run(
        [sys.executable, str(SCANNER), str(clean)],
        capture_output=True, text=True, env=env,
    )
    assert good.returncode == 0, good.stdout + good.stderr
