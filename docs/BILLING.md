# 账单与额度查询（BILLING）

relay-hub 的「账单」有两层，**权限与开关完全不同**，别混：

| 层 | 谁用 | 查什么 | 开关 |
|---|---|---|---|
| **本站 billing 接口** | 下游（任何持有效令牌的客户端） | **本站**给你的余额/已用 | `features.json` 的 `billing_enabled`，默认 **关** |
| **上游 new-api 查询** | **仅管理员**（管理面「账单」页） | **别人的 new-api 站**（上游/同学站）余额、分组倍率、单次成本 | 无（管理面本身要鉴权） |

---

## 一、本站 billing 接口（下游可查自己的账）

对标 new-api 的 OpenAI 兼容 Billing 接口。**默认关闭**——管理面「账单」页勾选「开放 billing 接口」后生效，改完下一请求即生效。

### `GET /v1/dashboard/billing/subscription`

```
Authorization: Bearer <你的下游令牌>
```

```json
{
  "object": "billing_subscription",
  "hard_limit_usd": 1000.0,     // 总额度；unlimited 时为 -1
  "soft_limit_usd": 1000.0,
  "access_until": 0,            // 令牌到期时间戳（0 = 不过期）
  "display_type": "points",     // 本站单位是「点」，不是美元
  "unlimited": false,
  "quota": 1000,
  "used": 123
}
```

### `GET /v1/dashboard/billing/usage?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`

```json
{
  "object": "list",
  "total_usage": 12300,         // 单位「分」= used × 100
  "display_type": "points",
  "used": 123,
  "quota": 1000,
  "unlimited": false
}
```

**口径**：
- 只返回**调用者自己**的账（按令牌绑定的用户；无绑定时按令牌名归集）。
- `total_usage` 沿用 new-api 的「分」约定（`amount × 100`），方便同一套客户端逻辑。
- 关闭时返回 `404 {"error":{"message":"billing API 未开放…"}}`；未带令牌返回 401。

---

## 二、上游 new-api 查询（仅管理员）

管理面「账单」页。密钥**只用于本次查询**：不落盘、不进审计（审计只记站点地址 / request_id）。

### `POST /api/newapi`

```json
{ "base_url": "https://example.com", "api_key": "sk-…",
  "start_date": "2026-10-01", "end_date": "2026-10-31" }
```

返回：

```json
{
  "ok": true,
  "base_url": "https://example.com",
  "unit": "CNY",                 // 站点 quota_display_type（USD / CNY / tokens）
  "hard_limit": 100.0,           // /v1/dashboard/billing/subscription 的 hard_limit_usd
  "used": 12.34,                 // total_usage / 100（分 → 金额）
  "remaining": 87.66,            // hard_limit - used
  "used_cents": 1234,
  "groups": {"default": 1, "vip": 0.15},   // /api/pricing 的分组倍率
  "model_count": 11,
  "subscription": { … }, "usage": { … }, "checked_at": 1790000000.0
}
```

- 自动兼容带 / 不带 `/v1` 的 billing 路径。
- 单位由站点 `quota_display_type` 决定（字段名仍叫 `*_usd`，别按美元硬算）。

### `POST /api/newapi/cost`

```json
{ "base_url": "https://example.com", "api_key": "sk-…", "request_id": "2026…" }
```

`request_id` 取自上游响应头 `X-Oneapi-Request-Id`；查询 `GET /api/cost/request/<id>` 拿单次精确消耗。返回 `{"ok": true, "result": { … }}`。

---

## 三、功能开关

`<RELAYHUB_HOME>/features.json`：

```json
{ "schemaVersion": 1, "billing_enabled": false }
```

- 默认关闭；数据面**每请求读取**该文件，改完下一请求生效，无需重启。
- 管理面：`GET /api/features` 读、`POST /api/features {"billing_enabled": true}` 写（写后记审计 `features.update`）。
- 新增开关时在 `relayhub/gateway/features.py` 的 `DEFAULTS` 登记即可（`load()` 自动补默认）。
