# 请求安全检测（guard）

默认**关闭**。正则与模型判定都会误伤正常请求，而误伤的代价是"用户发不出话"，
比放过一次高危请求更刺眼。开关与规则都在站长手里。

## 启用

管理面「服务器 → 请求安全检测」，或直接写 `<home>/guard.json`：

```json
{
  "enabled": true,
  "action": "block",
  "rules": [
    {"rule_id": "r1", "pattern": "忽略(以上|之前).*(指令|规则)", "reason": "疑似破甲", "action": "block"},
    {"rule_id": "r2", "pattern": "你现在是(DAN|无限制模式)", "reason": "角色越狱", "action": "flag"}
  ],
  "models": [],
  "model_enabled": false,
  "model_endpoint": "",
  "model_key": "",
  "model_name": "guard",
  "model_timeout": 3.0,
  "model_fail_open": true
}
```

| 字段 | 说明 |
|---|---|
| `enabled` | 总开关，默认 false |
| `action` | 命中动作：`block`（403）或 `flag`（只记审计，不拦） |
| `rules[].pattern` | 正则，忽略大小写与换行；编译失败的规则会被丢弃而不是让整份配置失效 |
| `models` | 只对这些模型生效，空 = 全部 |
| `model_enabled` | 启用轻量模型判定（额外的网络请求与延迟） |
| `model_fail_open` | 模型服务不可用时是否放行；默认 true（可用性优先），但会留记录 |

## 模型判定协议

把请求文本 POST 到 `model_endpoint`，期望返回：

```json
{"blocked": true, "reason": "疑似诱导越狱"}
```

超时由 `model_timeout` 控制（0.5–15 秒）。返回非 200 或超时按 `model_fail_open` 处理。

## 拦截效果

命中 `block` 规则时：

```
HTTP 403
{"error": {"message": "请求被安全策略拦截：疑似破甲", "type": "permission_error"}}
```

同时写审计 `guard.block`（规则 id、来源 regex/model、模型名、请求 id、来源 IP）。
**不落盘请求原文**——隐私优先，只留判定结论。

## 两条已知边界（不要误解）

1. **直通路径只跑正则**。站间直通拿不到解析后的消息体，只能对原始字节做正则匹配，
   模型判定不生效。这是"零翻译"的代价，写在 `docs/PEER.md` 里。
2. **正则必然有误伤与绕过**。建议先用 `flag` 跑一段时间看命中率，确认没误杀再切 `block`；
   规则要具体（"忽略以上所有指令"）而不是宽泛（"忽略"）。

## 规则示例（破甲类）

| 正则 | 说明 |
|---|---|
| `忽略(以上|之前|上面).{0,6}(所有)?(指令|规则|设定)` | 典型提示注入开头 |
| `(现在|从现在起)你是.{0,10}(DAN|无限制|不受限制)` | 角色越狱 |
| `(输出|重复).{0,6}(你的)?(系统提示|system prompt)` | 套取系统提示 |
| `(base64|十六进制).{0,10}(解码|执行)` | 编码绕过 |

这些只是起点：真实的破甲话术变化很快，规则需要按你站上的实际命中情况维护。
