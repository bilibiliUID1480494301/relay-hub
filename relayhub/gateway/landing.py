"""落地页模板。

与用户面板共用同一套外观：同一个图标、同一组颜色、同一份壁纸，避免
「打开域名一个样、点进面板另一个样」。`__THEME_BG__` 由 service 注入。
"""

ICON = (
    "<link rel=\"icon\" href=\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
    "viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23185FA5'/%3E"
    "%3Cpath d='M9 23V9h5l9 9v5h-5l-9-9v9z' fill='%23fff'/%3E%3C/svg%3E\">"
)

LANDING_HTML = (
    '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    + ICON
    + '<title>__SITE__</title><style>'
    ":root{--accent:#185FA5;--fg:#26215C;--muted:#6B6A8F;--line:#E3E1F0}"
    'body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;'
    "color:var(--fg);margin:0;display:flex;align-items:center;justify-content:center;"
    "min-height:100vh;__THEME_BG__}"
    ".card{background:#fff;border:1px solid var(--line);border-radius:14px;"
    "box-shadow:0 8px 28px rgba(38,33,92,.10);padding:28px 30px;max-width:620px;"
    "width:calc(100% - 48px)}"
    "h1{font-size:19px;margin:0 0 6px;font-weight:600}"
    ".sub{color:var(--muted);font-size:13px;margin:0 0 18px}"
    "a{color:var(--accent)}code{font-family:ui-monospace,Consolas,monospace;font-size:12px}"
    "ul{margin:8px 0 14px;padding-left:18px;line-height:1.9}"
    ".btn{display:inline-block;background:var(--accent);color:#fff;text-decoration:none;"
    "padding:9px 16px;border-radius:9px;font-size:13px}"
    "</style></head><body><div class=\"card\">"
    "<h1>__SITE__</h1><p class=\"sub\">OpenAI / Anthropic / Gemini 兼容的 API 网关</p>"
    "<p><a class=\"btn\" href=\"/panel\">进入用户面板</a></p>"
    "<p class=\"sub\" style=\"margin-top:18px\">可用端点（均需凭证）：</p>"
    "<ul><li><code>POST /v1/chat/completions</code></li>"
    "<li><code>POST /v1/messages</code></li>"
    "<li><code>POST /v1/embeddings</code></li>"
    "<li><code>POST /v1/responses</code></li>"
    "<li><code>POST /v1beta/models/{model}:generateContent</code></li></ul>"
    "<p class=\"sub\">健康检查 <code>GET /healthz</code>（无需凭证）。"
    "凭证缺失返回 401，超限返回 429。</p>"
    "</div></body></html>"
)
