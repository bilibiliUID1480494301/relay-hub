"""请求安全检测（默认关闭）。

为什么默认关：正则和模型判定都会误伤正常请求，而误伤的代价是"用户发不出话"，
比放过一次高危请求更刺眼。开关交给站长，规则也交给站长。

三层能力，可单独启用：

1. **正则规则**：命中即按规则动作处理（block / flag）。零成本、可解释，
   适合拦"破甲"这类有固定句式特征的行为（如"忽略以上所有指令"）。
2. **轻量模型判定**：把请求文本发给一个便宜模型做二分类。比正则灵活，
   但有延迟与额外成本，所以超时可配、失败策略可配。
3. **长度/频率等结构性规则**：不在这里做——那是限流模块的职责。

设计约束：

* 判定只读请求文本，**不落盘原文**（隐私），命中时只记规则 id 与请求 id；
* 直通（peer passthrough）路径拿不到解析后的消息体，只能用正则扫原始字节，
  这一点必须在文档里写清，不能让人以为它也过了模型判定。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atomicio import write_json_atomic

MAX_TEXT = 200_000


@dataclass
class Rule:
    rule_id: str
    pattern: str
    action: str = "block"      # block | flag
    reason: str = ""
    enabled: bool = True
    _re: Any = field(default=None, repr=False)

    def compiled(self) -> Any:
        if self._re is None:
            try:
                self._re = re.compile(self.pattern, re.I | re.S)
            except re.error:
                self._re = False
        return self._re


@dataclass
class GuardConfig:
    enabled: bool = False
    # 正则命中的默认动作；单条规则可以覆盖
    action: str = "block"
    rules: list[Rule] = field(default_factory=list)
    # 轻量模型判定
    model_enabled: bool = False
    model_endpoint: str = ""
    model_key: str = ""
    model_name: str = ""
    model_timeout: float = 3.0
    # 模型判定失败（超时/5xx）时是否放行。默认放行：可用性优先，
    # 但要留下 flag 记录，便于事后发现"检测其实一直没生效"。
    model_fail_open: bool = True
    # 只对这些模型生效（空 = 全部）
    models: tuple[str, ...] = ()


def clean_rules(values: Any) -> list[Rule]:
    out: list[Rule] = []
    for item in values or []:
        if isinstance(item, str):
            item = {"pattern": item}
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or "").strip()
        if not pattern or len(pattern) > 500:
            continue
        try:
            re.compile(pattern)
        except re.error:
            continue
        action = str(item.get("action") or "block").lower()
        out.append(Rule(
            rule_id=str(item.get("rule_id") or f"r{len(out) + 1}")[:32],
            pattern=pattern,
            action="block" if action not in ("block", "flag") else action,
            reason=str(item.get("reason") or "")[:200],
            enabled=bool(item.get("enabled", True)),
        ))
        if len(out) >= 100:
            break
    return out


def load(path: Path) -> GuardConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return GuardConfig()
    return GuardConfig(
        enabled=bool(raw.get("enabled")),
        action="flag" if str(raw.get("action")) == "flag" else "block",
        rules=clean_rules(raw.get("rules") or []),
        model_enabled=bool(raw.get("model_enabled")),
        model_endpoint=str(raw.get("model_endpoint") or ""),
        model_key=str(raw.get("model_key") or ""),
        model_name=str(raw.get("model_name") or ""),
        model_timeout=float(raw.get("model_timeout") or 3.0),
        model_fail_open=bool(raw.get("model_fail_open", True)),
        models=tuple(str(m) for m in (raw.get("models") or ())),
    )


def save(path: Path, cfg: GuardConfig) -> None:
    write_json_atomic(Path(path), {
        "enabled": cfg.enabled,
        "action": cfg.action,
        "rules": [
            {"rule_id": r.rule_id, "pattern": r.pattern, "action": r.action,
             "reason": r.reason, "enabled": r.enabled}
            for r in cfg.rules
        ],
        "model_enabled": cfg.model_enabled,
        "model_endpoint": cfg.model_endpoint,
        "model_key": cfg.model_key,
        "model_name": cfg.model_name,
        "model_timeout": cfg.model_timeout,
        "model_fail_open": cfg.model_fail_open,
        "models": list(cfg.models),
    })


def public(cfg: GuardConfig) -> dict[str, Any]:
    """管理面回读：模型密钥不回显，只给"是否已设置"。"""
    return {
        "enabled": cfg.enabled, "action": cfg.action,
        "rules": [
            {"rule_id": r.rule_id, "pattern": r.pattern, "action": r.action,
             "reason": r.reason, "enabled": r.enabled}
            for r in cfg.rules
        ],
        "model_enabled": cfg.model_enabled,
        "model_endpoint": cfg.model_endpoint,
        "model_name": cfg.model_name,
        "model_timeout": cfg.model_timeout,
        "model_fail_open": cfg.model_fail_open,
        "models": list(cfg.models),
        "has_model_key": bool(cfg.model_key),
    }


@dataclass
class Verdict:
    allowed: bool = True
    action: str = ""          # block | flag
    rule_id: str = ""
    reason: str = ""
    source: str = ""          # regex | model

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "action": self.action, "rule_id": self.rule_id,
                "reason": self.reason, "source": self.source}


def _regex_hit(text: str, cfg: GuardConfig) -> Verdict | None:
    sample = text[:MAX_TEXT]
    for rule in cfg.rules:
        if not rule.enabled:
            continue
        pattern = rule.compiled()
        if not pattern:
            continue
        if pattern.search(sample):
            action = rule.action or cfg.action
            return Verdict(allowed=(action != "block"), action=action,
                           rule_id=rule.rule_id,
                           reason=rule.reason or f"命中规则 {rule.rule_id}",
                           source="regex")
    return None


def _model_hit(text: str, cfg: GuardConfig) -> Verdict | None:
    """让一个轻量模型做二分类。失败按 model_fail_open 处理。"""
    import http.client
    from urllib.parse import urlsplit

    if not cfg.model_endpoint:
        return None
    body = json.dumps({"text": text[:MAX_TEXT], "model": cfg.model_name or "guard"}, ensure_ascii=False)
    parts = urlsplit(cfg.model_endpoint)
    if not parts.hostname:
        return None
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                    timeout=cfg.model_timeout)
    try:
        headers = {"Content-Type": "application/json"}
        if cfg.model_key:
            headers["Authorization"] = f"Bearer {cfg.model_key}"
        conn.request("POST", parts.path or "/", body=body.encode("utf-8"), headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise OSError(f"guard model {resp.status}")
        data = json.loads(raw.decode("utf-8") or "{}")
        if bool(data.get("blocked")):
            return Verdict(allowed=False, action="block", rule_id="model",
                           reason=str(data.get("reason") or "模型判定为高危请求")[:200],
                           source="model")
        return None
    except Exception as exc:  # 超时/连不上/返回不合法
        if cfg.model_fail_open:
            return None
        return Verdict(allowed=False, action="block", rule_id="model-error",
                       reason=f"安全检测不可用：{exc}"[:200], source="model")
    finally:
        conn.close()


def inspect(text: str, cfg: GuardConfig, model: str = "", *, allow_model: bool = True) -> Verdict:
    """检查一段请求文本。未启用直接放行（零成本）。"""
    if not cfg.enabled or not text:
        return Verdict()
    if cfg.models and model and model not in cfg.models:
        return Verdict()
    hit = _regex_hit(text, cfg)
    if hit is not None:
        return hit
    if allow_model and cfg.model_enabled:
        hit = _model_hit(text, cfg)
        if hit is not None:
            return hit
    return Verdict()


def elapsed_ms(start: float) -> float:
    return round((time.time() - start) * 1000, 1)
