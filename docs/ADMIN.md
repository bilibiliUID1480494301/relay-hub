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

## API 一览（管理面 HTTP 接口）

- `GET /api/state`：渠道/令牌/健康总览
- `POST /api/keys` / `PATCH /api/keys/{id}`：渠道增改（含 group、cost_multiplier、price_in/out_per_1k）
- `GET /api/usage?days=N`：用量汇总（window 含 revenue / upstream_cost / profit）
- `GET /api/users` / `POST /api/users` / `PUT /api/users`：用户与计费规则（PUT 含 peak_hours / peak_multiplier）
- `POST /api/codes`：兑换码批量生成 `{count, credits}`
- `GET /login` → `POST /login`：会话 cookie（HttpOnly）
