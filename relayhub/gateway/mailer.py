"""邮件发送（SMTP）+ 验证码存储。

设计约束（都是踩过坑才会写的）：

1. **不假装能发**：没配置或配置不完整时 `send_code` 直接抛 MailNotConfigured，
   调用方据此回 503 并说明「本站未开启邮件服务」，而不是静默吞掉让用户等。
2. **同步发送、超时短**：注册是有人等着的交互，SMTP 卡住比发不出去更糟。
   连接与读各 12 秒，用完即关（不做连接池：一天几十封不值得）。
3. **验证码只存哈希**：落盘的是 sha256(salt + code)，拿到文件也反推不出码。
4. **限流按「邮箱 + 用途」**：60 秒内不许重发，1 小时最多 5 次；
   校验失败 5 次即作废，避免 6 位码被穷举（10^6 / 5 次概率极低但仍要拦）。
5. **单次有效**：验证成功立即删除记录，重放同一码无效。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path
from typing import Any

from .atomicio import write_json_atomic

MAIL_TIMEOUT = 12.0
CODE_TTL = 600            # 10 分钟
RESEND_INTERVAL = 60      # 同邮箱同用途 60 秒
MAX_PER_HOUR = 5
MAX_ATTEMPTS = 5

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")
PURPOSES = ("register", "reset")


class MailError(RuntimeError):
    """发信失败（配置错 / 连不上 / 被拒）。message 可以直接展示给用户。"""


class MailNotConfigured(MailError):
    """没开邮件服务：调用方应回 503 并给出配置指引。"""


def clean_email(value: object) -> str:
    text = str(value or "").strip().lower()
    if len(text) > 200 or not _EMAIL_RE.match(text):
        return ""
    return text


@dataclass
class MailConfig:
    enabled: bool = False
    host: str = ""
    port: int = 465
    user: str = ""
    password: str = ""
    sender: str = ""
    tls: str = "ssl"          # ssl | starttls | none
    site_name: str = "relay-hub"
    # 注册允许的邮箱域名；空 = 不限。只约束**注册**，不约束找回密码——
    # 域名后来被移除的人也要能登回去。
    allowed_domains: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.host and self.sender and self.tls in ("ssl", "starttls", "none"))

    def public(self) -> dict[str, Any]:
        """给管理面回读用：密码永远不回显，只给「是否已设置」。"""
        return {
            "enabled": self.enabled, "host": self.host, "port": self.port,
            "user": self.user, "sender": self.sender, "tls": self.tls,
            "has_password": bool(self.password),
            "allowed_domains": list(self.allowed_domains),
        }


_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def clean_domains(values: object) -> tuple[str, ...]:
    """域名白名单：去协议/空格/@，小写，最多 50 条；非法条目直接丢弃。"""
    NL = chr(10)
    if isinstance(values, str):
        values = values.replace(",", NL).split(NL)
    if not isinstance(values, (list, tuple)):
        return ()
    out: list[str] = []
    for raw in values:
        item = str(raw or "").strip().lower()
        item = item.split("@")[-1].strip(".").strip()
        if item.startswith("*."):
            item = item[2:]
        if not item or len(item) > 190 or not _DOMAIN_RE.match(item):
            continue
        if item not in out:
            out.append(item)
        if len(out) >= 50:
            break
    return tuple(out)


def domain_allowed(email: str, domains: tuple[str, ...]) -> bool:
    """白名单为空放行；否则要求精确匹配或子域匹配（example.com 覆盖 a.example.com）。"""
    if not domains:
        return True
    addr = str(email or "").strip().lower()
    if "@" not in addr:
        return False
    host = addr.rsplit("@", 1)[1]
    for d in domains:
        if host == d or host.endswith("." + d):
            return True
    return False


def load_config(path: Path) -> MailConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MailConfig()
    tls = str(raw.get("tls") or "ssl").lower()
    if tls not in ("ssl", "starttls", "none"):
        tls = "ssl"
    try:
        port = int(raw.get("port") or 465)
    except (TypeError, ValueError):
        port = 465
    return MailConfig(
        enabled=bool(raw.get("enabled")),
        host=str(raw.get("host") or "").strip(),
        port=max(1, min(65535, port)),
        user=str(raw.get("user") or "").strip(),
        password=str(raw.get("password") or ""),
        sender=str(raw.get("sender") or "").strip(),
        tls=tls,
        site_name=str(raw.get("site_name") or "relay-hub"),
        allowed_domains=clean_domains(raw.get("allowed_domains") or []),
    )


def save_config(path: Path, cfg: MailConfig) -> None:
    write_json_atomic(Path(path), {
        "enabled": cfg.enabled, "host": cfg.host, "port": cfg.port,
        "user": cfg.user, "password": cfg.password, "sender": cfg.sender,
        "tls": cfg.tls, "site_name": cfg.site_name,
        "allowed_domains": list(cfg.allowed_domains),
    })


def _body(code: str, purpose: str, site: str) -> tuple[str, str]:
    what = "注册账号" if purpose == "register" else "重置密码"
    subject = f"【{site}】{what}验证码：{code}"
    text = (
        f"你正在{what}，验证码是：{code}\n\n"
        f"{CODE_TTL // 60} 分钟内有效，仅可使用一次。\n"
        "如果不是你本人操作，请忽略这封邮件——账号不会有任何变化。\n"
    )
    return subject, text


def send_code(cfg: MailConfig, to: str, code: str, purpose: str) -> None:
    """同步发一封验证码邮件。失败抛 MailError（含可以展示的原因）。"""
    if not cfg.ready:
        raise MailNotConfigured("本站未开启邮件服务")
    subject, text = _body(code, purpose, cfg.site_name)
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(cfg.site_name, "utf-8")), cfg.sender))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)

    try:
        if cfg.tls == "ssl":
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=MAIL_TIMEOUT,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=MAIL_TIMEOUT)
        try:
            server.ehlo()
            if cfg.tls == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if cfg.user:
                server.login(cfg.user, cfg.password)
            server.sendmail(cfg.sender, [to], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise MailError(f"发送失败：{exc}") from exc


# ---------------------------------------------------------------- 验证码存储


@dataclass
class CodeStore:
    """邮箱验证码。文件小、写入少，直接在锁内整体读写。"""

    path: Path
    _lock: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        import threading

        self.path = Path(self.path)
        self._lock = threading.Lock()

    # -- 内部 --
    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _key(email: str, purpose: str) -> str:
        return f"{purpose}:{email}"

    @staticmethod
    def _hash(code: str, salt: str) -> str:
        return hashlib.sha256((salt + code).encode("utf-8")).hexdigest()

    def issue(self, email: str, purpose: str) -> str:
        """生成并落盘验证码；被限流时抛 MailError（可直接展示）。"""
        if purpose not in PURPOSES:
            raise MailError("未知用途")
        now = time.time()
        key = self._key(email, purpose)
        with self._lock:
            data = self._read()
            rec = data.get(key) or {"sent": []}
            sent = [t for t in rec.get("sent", []) if now - float(t) < 3600]
            if rec.get("last") and now - float(rec["last"]) < RESEND_INTERVAL:
                wait = int(RESEND_INTERVAL - (now - float(rec["last"])))
                raise MailError(f"发送太频繁，请 {max(1, wait)} 秒后再试")
            if len(sent) >= MAX_PER_HOUR:
                raise MailError("该邮箱 1 小时内发送次数过多，请稍后再试")
            code = f"{secrets.randbelow(1000000):06d}"
            salt = secrets.token_hex(8)
            data[key] = {
                "hash": self._hash(code, salt), "salt": salt,
                "exp": now + CODE_TTL, "attempts": 0, "last": now,
                "sent": sent + [now],
            }
            write_json_atomic(self.path, data)
            return code

    def verify(self, email: str, purpose: str, code: str) -> tuple[bool, str]:
        """校验并**作废**。返回 (是否通过, 失败原因)。"""
        key = self._key(email, purpose)
        now = time.time()
        with self._lock:
            data = self._read()
            rec = data.get(key)
            if not rec:
                return False, "验证码不存在或已使用"
            if now > float(rec.get("exp") or 0):
                data.pop(key, None)
                write_json_atomic(self.path, data)
                return False, "验证码已过期，请重新获取"
            if int(rec.get("attempts") or 0) >= MAX_ATTEMPTS:
                data.pop(key, None)
                write_json_atomic(self.path, data)
                return False, "尝试次数过多，请重新获取验证码"
            if not secrets.compare_digest(self._hash(str(code), rec["salt"]), rec["hash"]):
                rec["attempts"] = int(rec.get("attempts") or 0) + 1
                write_json_atomic(self.path, data)
                left = MAX_ATTEMPTS - rec["attempts"]
                return False, f"验证码不正确（还可尝试 {max(0, left)} 次）"
            data.pop(key, None)      # 单次有效
            write_json_atomic(self.path, data)
            return True, ""

    def purge_expired(self) -> int:
        now = time.time()
        with self._lock:
            data = self._read()
            dead = [k for k, v in data.items() if float(v.get("exp") or 0) < now]
            for k in dead:
                data.pop(k, None)
            if dead:
                write_json_atomic(self.path, data)
            return len(dead)
