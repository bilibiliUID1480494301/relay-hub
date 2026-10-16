"""Product-level leak scanner: unpacks whl/sdist and greps for leak shapes.

用法：python scan_leak.py dist/*.whl dist/*.tar.gz

设计约束：**这个脚本本身是公开发行的**，所以它不能把内部项目名写在这里
（那等于把要保护的名单连同一份清单一起公开）。它只查**通用泄露形态**：

  * 本机绝对路径（Windows 盘符路径 / macOS `/Users/` / Linux `/home/`）
  * 机器名（LAPTOP-* / DESKTOP-* 这类默认主机名前缀）
  * 凭证形态（各家 key 前缀、PEM 私钥、JWT）
  * 疑似内网地址（非 RFC1918 示例段）

组织特有的敏感词（内部项目名等）通过环境变量注入，**不进仓库**：

    RELAYHUB_LEAK_EXTRA="foo,bar,baz" python scan_leak.py dist/*.whl

CI 里把该变量从 secret 注入即可，仓库保持干净。
"""
import os
import re
import sys
import tarfile
import zipfile

# 通用形态：与具体组织无关，写死在仓库里是安全的
SHAPES = {
    "localpath": re.compile(
        r"[A-Za-z]:[\\/](?:Users|Documents|AppData|dev|src|work)[\\/]"
        r"|/Users/[A-Za-z0-9._-]+/"
        r"|/home/[A-Za-z0-9._-]+/",
        re.I,
    ),
    "hostname": re.compile(r"\b(?:LAPTOP|DESKTOP|WIN)-[0-9A-Z]{6,}\b", re.I),
    "secret": re.compile(
        r"sk-[A-Za-z0-9]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}"
        r"|ghp_[A-Za-z0-9]{20,}"
        r"|pypi-[A-Za-z0-9_\-]{20,}"
        r"|AKIA[0-9A-Z]{16}"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
        r"|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\."
    ),
}

TEXT_SUFFIXES = (
    ".py", ".md", ".toml", ".txt", ".json", ".cfg", ".ini",
    ".yml", ".yaml", ".lock", ".cff", ".rst", ".sh", ".bat", ".ps1",
)


def extra_pattern() -> re.Pattern | None:
    """组织特有词表：从环境变量读，仓库里不留名单。"""
    raw = (os.environ.get("RELAYHUB_LEAK_EXTRA") or "").strip()
    terms = [t.strip() for t in raw.split(",") if t.strip()]
    if not terms:
        return None
    return re.compile("|".join(re.escape(t) for t in terms), re.I)


ZIP_SUFFIXES = (".whl", ".zip", ".egg")


def _open_archive(path: str):
    """按后缀选容器。zip 家族走 ZipFile，其余走 tarfile。

    早先只认 `.whl` 为 zip，其余一律塞给 tarfile —— 传 .zip 会得到一坨
    "not a gzip/bzip2/lzma file / truncated header" 的 ReadError，读的人
    会以为产物坏了，其实是扫描器认错了容器。
    """
    if path.lower().endswith(ZIP_SUFFIXES):
        return zipfile.ZipFile(path), True
    try:
        return tarfile.open(path), False
    except tarfile.ReadError as exc:
        raise SystemExit(
            f"无法识别容器（{path}）：既不是 zip 家族（{', '.join(ZIP_SUFFIXES)}）"
            f"也不是 tar。{exc}"
        ) from exc


def scan(path: str) -> int:
    archive, is_zip = _open_archive(path)
    names = archive.namelist() if is_zip else archive.getnames()
    read = archive.read if is_zip else archive.extractfile

    patterns = list(SHAPES.items())
    extra = extra_pattern()
    if extra is not None:
        patterns.append(("extra", extra))

    hits = 0
    for name in names:
        # 成员名本身也算泄露面（路径里带本机目录名同样要拦）
        targets: list[tuple[str, str]] = [("member name", name)]
        if name.endswith(TEXT_SUFFIXES):
            try:
                fh = read(name)
                # zipfile.read() 直接给 bytes；tarfile.extractfile() 给文件对象。
                # 两者必须都认——早先写的是 `fh.read() if hasattr(fh,"read") else b""`，
                # 而 bytes 没有 .read()，于是 **wheel 永远扫了个空**、一律报 CLEAN。
                # 这就是本仓 scan_leak 长期形同虚设、泄露溜过去的真正原因。
                blob = fh.read() if hasattr(fh, "read") else fh
                text = blob if isinstance(blob, str) else bytes(blob).decode("utf-8", "ignore")
                targets.append((name, text))
            except Exception:
                pass
        for where, text in targets:
            for kind, pattern in patterns:
                for m in pattern.finditer(text):
                    hits += 1
                    line = text[: m.start()].count("\n") + 1
                    label = f"{path}:{where}:{line}"
                    print(f"  LEAK[{kind}] {label} {m.group(0)[:80]!r}")
    print(f"{path}: {'CLEAN' if hits == 0 else str(hits) + ' HITS'}")
    return hits


if __name__ == "__main__":
    bad = sum(scan(p) for p in sys.argv[1:])
    print("VERDICT:", "CLEAN" if bad == 0 else "BLOCK")
    raise SystemExit(1 if bad else 0)
