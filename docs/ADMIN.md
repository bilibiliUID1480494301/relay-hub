# 管理面与页面自定义（ADMIN）

> 管理面是独立进程：`hubrelay admin --port 18802`（或 `python -m relayhub.gateway admin`）。
> 数据与网关共享同一组 JSON 文件，改完即写盘、网关热加载，不用重启、不掐断客户端。

## 页面结构

单页应用（admin.py 内嵌 HTML/JS），顶栏 tab 切换：

| Tab | 内容 |
|-----|------|
| 渠道 | 上游渠道 CRUD：协议 / 模型与上下文 / 模型映射 / **渠道组** / **成本倍率与单价** / 优先级 |
| 下游令牌 | API Key 管理：模型白名单 / RPM 与日配额 / 有效期 / 用户绑定 / 分组标签 |
| 接入策略 | IP/令牌拉黑与优先 |
| 用户 | 用户 CRUD、计费规则（标价 / 分组倍率 / **峰时分时**）、开放注册、兑换码批量生成 |
| 请求 / 用量 | 请求日志明细；用量汇总卡片（请求/成功/失败/tokens/**收入/上游成本/利润**）+ 按日/令牌/模型/渠道分桶 |
| 插件 / 动态口令 / 审计 | 插件分账日志、TOIP 站点口令、审计流水 |
| 账单 | 上游 new-api 余额/倍率/单次成本；**服务商一键测试**（13 家，勾选→填 Key→模型+余额）；本站 billing 开关 |

## 页面自定义（主题令牌）

整个管理面的配色收敛在 CSS 变量里，改 `relayhub/gateway/admin.py` 顶部 `:root` 即可全站生效：

```css
:root{
  --bg:#F7F6F3;         /* 页面背景 */
  --panel:#FFFFFF;      /* 卡片/面板背景 */
  --fg:#26215C;         /* 正文前景 */
  --muted:#5F5E5A;      /* 次要文字 */
  --line:#E3E1DA;       /* 分隔线/边框 */
  --accent:#185FA5;     /* 主色（按钮/选中态） */
  --accent-soft:#E6F1FB;/* 主色软底 */
  --ok:#0F6E56; --ok-soft:#E1F5EE;       /* 成功 */
  --warn:#854F0B; --warn-soft:#FAEEDA;   /* 警告 */
  --bad:#A32D2D; --bad-soft:#FCEBEB;     /* 危险 */
}
@media(prefers-color-scheme:dark){ /* 深色主题同名变量 */ }
```

自定义步骤：复制 `:root` 块 → 改色值 → 替换文件内两处 `:root`（浅色 + 深色媒体查询）。间距类工具类：`.row`（表单行，gap 10px）、`.ops`（行内按钮组，gap 8px）、`.tabs`（页签，gap 8px）。

## 管理员登录与动态密码

- `--token <凭据>`：静态 token，页面用 `?token=…` 打开或登录表单输入。
- `--totp-secret <base32>`：**动态管理员密码**（RFC 6238，认证器 App 生成 6 位码）。启用后登录须输入 `token:动态码`；±1 个 30 秒步长容错。生成示例：

```bash
python -c "import base64,os;print(base64.b32encode(os.urandom(20)).decode())"
```

- **用户密码永远是静态的**（SHA-256 落盘）：动态码只用于管理员，避免用户被锁死在门外。

## 账单与功能开关

管理面「账单」页分两块，**权限与开关不同**：

1. **上游 new-api 查询（仅管理员）**：填站点地址 + Key，查总额度 / 已用 / 剩余 / 单位 / 分组倍率；
   另可按 `X-Oneapi-Request-Id` 查单次成本。密钥**只用于本次查询**：不落盘、不进审计。
   实现见 `relayhub/gateway/newapi.py`；口径详见 [BILLING.md](BILLING.md)。
2. **本站 billing 开关**：勾选「开放 billing 接口」后，下游可用自己的令牌访问
   `GET /v1/dashboard/billing/subscription` 与 `/usage`（默认**关闭**，改完下一请求生效）。
   开关落在 `<RELAYHUB_HOME>/features.json`（`billing_enabled`）。

## 站点个性化命名

管理面「站点」区块可改站点名称（默认 `relay-hub`），显示在用户面板、落地页、管理面登录页与控制台标题上；
存 `<RELAYHUB_HOME>/site.json`，留空恢复默认，改完即时生效（无需重启）。

## 用户与管理员 CLI

`users.json` 没有管理员时管理面是**锁死**的。用 CLI 建站长 / 救密码：

```bash
python -m relayhub.gateway users init                  # 随机 16 位密码，只打印一次；quota=-1
python -m relayhub.gateway users reset-password        # 服务端强制重置（面板进不去也能救）
python -m relayhub.gateway users ls                    # 列出用户 / 角色 / 额度
```

密码门槛 **≥ 8 位**（注册、管理面重置、CLI 三处一致）。

## API 一览（管理面 HTTP 接口）

- `POST /v1beta/models/{model}:generateContent`：Gemini 原生入站（含 `:streamGenerateContent`）
- `GET /api/state`：渠道/令牌/健康总览
- `POST /api/keys` / `PATCH /api/keys/{id}`：渠道增改（含 group、cost_multiplier、price_in/out_per_1k）
- `GET /api/usage?days=N`：用量汇总（window 含 revenue / upstream_cost / profit）
- `GET /api/users` / `POST /api/users` / `PUT /api/users`：用户与计费规则（PUT 含 peak_hours / peak_multiplier）
- `POST /api/codes`：兑换码批量生成 `{count, credits}`
- `GET /login` → `POST /login`：会话 cookie（HttpOnly）
- `GET/POST /api/features`：功能开关（`billing_enabled` 等）
- `POST /api/newapi`：查上游 new-api 余额 / 分组倍率（`{base_url, api_key, start_date, end_date}`）
- `POST /api/newapi/cost`：按 `request_id`（`X-Oneapi-Request-Id`）查单次成本


## 服务商一键测试

「账单」页上半部分：勾选服务商 → 填 Key（域名可覆盖预填值）→「测试选中」。
每家同时查**模型端点**与**余额端点**，一行一家：

| 字段 | 说明 |
|---|---|
| 模型 | `/v1/models`（Gemini 为 `/v1beta/models`，智谱为 `/v4/models`）返回的条数 |
| 余额 | 有余额接口的解析成金额+币种；取不到只标错，不影响该家可用性 |
| 协议 | 该家支持的上游协议（`openai-chat` / `anthropic-messages` / `gemini-api`） |

内置 13 家：OpenAI、Anthropic、Gemini、DeepSeek、Kimi（月之暗面）、Kimi 国际站、
硅基流动、OpenRouter、Groq、Mistral、Together、智谱 GLM、new-api / one-api。

密钥只用于本次探测，不落盘；审计只记服务商名。接口：`GET /api/providers`、`POST /api/probe`。


## 上游定价表（new-api / relay-hub）

`POST /api/pricing {base_url, api_key}` 拉 `/api/pricing` 并整理成：模型 × 倍率 × 分组 × 端点。
`billing_expr` 原样带回（站点自己的计费口径，不重算）。

- `relayhub` 预设：模型 `/v1/models`、余额 `/v1/usage`（`quota - used`，单位「点」）
- `newapi` 预设：模型 `/v1/models`、余额 `/v1/dashboard/billing/subscription`


## 上游定价表（new-api / relay-hub）

`POST /api/pricing {base_url, api_key}` 拉 `/api/pricing` 并整理成：模型 × 倍率 × 分组 × 端点。
`billing_expr` 原样带回（站点自己的计费口径，不重算）。

- `relayhub` 预设：模型 `/v1/models`、余额 `/v1/usage`（`quota - used`，单位「点」）
- `newapi` 预设：模型 `/v1/models`、余额 `/v1/dashboard/billing/subscription`


## 模型指纹（同名换后端检测）

上游常改「同一个模型 id 背后的能力/倍率」：上下文缩水、多模态被砍、倍率翻倍。
网关按渠道对 `/v1/models` 做**清单指纹**（`id / context_window / max_output_tokens /
modalities / ratio / completion_ratio` 规范化后 sha256 前 16 位），只比对元数据，
**不调模型、不花 token**。

| 接口 | 作用 |
|---|---|
| `GET /api/fingerprint` | 上次扫描的基线（按渠道） |
| `POST /api/fingerprint/scan` | 拉全池渠道的清单，返回 `added / removed / changed` 与 `notable` |

`changed` 专指「id 没变但字段变了」——这是最值得提醒的一类。
基线存 `pool.json` 同目录的 `fingerprints.json`；首次扫描只建立基线（`first_seen: true`）。


## 模型指纹（同名换后端检测）

上游常改「同一个模型 id 背后的能力/倍率」：上下文缩水、多模态被砍、倍率翻倍。
网关按渠道对 `/v1/models` 做**清单指纹**（`id / context_window / max_output_tokens /
modalities / ratio / completion_ratio` 规范化后 sha256 前 16 位），只比对元数据，
**不调模型、不花 token**。

| 接口 | 作用 |
|---|---|
| `GET /api/fingerprint` | 上次扫描的基线（按渠道） |
| `POST /api/fingerprint/scan` | 拉全池渠道的清单，返回 `added / removed / changed` 与 `notable` |

`changed` 专指「id 没变但字段变了」——这是最值得提醒的一类。
基线存 `pool.json` 同目录的 `fingerprints.json`；首次扫描只建立基线（`first_seen: true`）。


## 全站公告

| 接口 | 说明 |
|---|---|
| `POST /api/announcement`（管理员） | `{"text":"…","until":<epoch 秒>,"level":"info|warning|critical"}`；`text` 传空即清除 |
| `GET /api/announcement` | **无需凭证**，面板/落地页读；过期后 `text` 自动为空 |

`until=0` 表示长期有效。写入 `site.json`，与站点名共用一份配置；**改名不会清掉公告**。
用户面板加载时会在顶部插入横幅（`textContent` 赋值，不拼 HTML）。

## 错误里的日志编号

所有错误响应体带 `request_id`，与响应头 `X-Request-ID`、`/api/requests` 里的 `req_id` 一致：

```json
{"error":{"message":"…","type":"api_error","code":"upstream_rate_limit","request_id":"req_9f2c…"}}
```

用户报障时贴这一串即可定位；不需要让他们翻响应头。


## 全站公告

| 接口 | 说明 |
|---|---|
| `POST /api/announcement`（管理员） | `{"text":"…","until":<epoch 秒>,"level":"info|warning|critical"}`；`text` 传空即清除 |
| `GET /api/announcement` | **无需凭证**，面板/落地页读；过期后 `text` 自动为空 |

`until=0` 表示长期有效。写入 `site.json`，与站点名共用一份配置；**改名不会清掉公告**。
用户面板加载时会在顶部插入横幅（`textContent` 赋值，不拼 HTML）。

## 错误里的日志编号

所有错误响应体带 `request_id`，与响应头 `X-Request-ID`、`/api/requests` 里的 `req_id` 一致：

```json
{"error":{"message":"…","type":"api_error","code":"upstream_rate_limit","request_id":"req_9f2c…"}}
```

用户报障时贴这一串即可定位；不需要让他们翻响应头。


## 一键升级与回滚

| 端点 | 权限 | 说明 |
|---|---|---|
| `POST /api/update/install` | **仅 owner** | `{version, apply, force?, reason?}`：下载 → 验签 → 安装 |
| `POST /api/update/rollback` | **仅 owner** | 退回上一版（依赖 `updates/history.json`） |

- 验签失败：默认 **409 不安装**；`force:true` 需附 `reason`（≥8 字），会写 `update.forced` 审计并返回 `verified:false`
- 安装用 `pip install --no-deps --force-reinstall --no-input`，不动依赖、不交互
- 装完写 `restart.flag`，**由外部 keepalive 拉起新进程**；服务自己不退出（避免无人接管的空窗）
- 历史保留最近 5 版 wheel，装坏可回滚


## 一键升级与回滚

| 端点 | 权限 | 说明 |
|---|---|---|
| `POST /api/update/install` | **仅 owner** | `{version, apply, force?, reason?}`：下载 → 验签 → 安装 |
| `POST /api/update/rollback` | **仅 owner** | 退回上一版（依赖 `updates/history.json`） |

- 验签失败：默认 **409 不安装**；`force:true` 需附 `reason`（≥8 字），会写 `update.forced` 审计并返回 `verified:false`
- 安装用 `pip install --no-deps --force-reinstall --no-input`，不动依赖、不交互
- 装完写 `restart.flag`，**由外部 keepalive 拉起新进程**；服务自己不退出（避免无人接管的空窗）
- 历史保留最近 5 版 wheel，装坏可回滚


## 静态密钥保护（0.12.1）

| 文件 | 落盘方式 |
|---|---|
| `pool.json`（上游 Key） | **secretbox 密封**（DPAPI / 降级明文+`enc` 标记），0600 |
| `tokens.json` / `policy.json` / `pairing.json` | 同上 |
| 数据根 | **0700** |
| 历史明文 | `unseal()` 自动兼容，下次写入自动升级为密封，无需迁移脚本 |

`config_fingerprint()` 改为**解封后按内容算**——否则密文每次都不同，指纹永远在变，
会退化成每请求 reload（round-robin 游标被反复重置）。


## 静态密钥保护（0.12.1）

| 文件 | 落盘方式 |
|---|---|
| `pool.json`（上游 Key） | **secretbox 密封**（DPAPI / 降级明文+`enc` 标记），0600 |
| `tokens.json` / `policy.json` / `pairing.json` | 同上 |
| 数据根 | **0700** |
| 历史明文 | `unseal()` 自动兼容，下次写入自动升级为密封，无需迁移脚本 |

`config_fingerprint()` 改为**解封后按内容算**——否则密文每次都不同，指纹永远在变，
会退化成每请求 reload（round-robin 游标被反复重置）。
