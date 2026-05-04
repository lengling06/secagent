# Cloudflare 防护站点

**何时用此 skill**: 目标响应里出现以下任一信号:
- HTTP header `cf-ray:` / `server: cloudflare`
- Cookie `__cf_bm` / `cf_clearance`
- HTML 含 `challenges.cloudflare.com` / `cf-mitigated`
- curl 拿到 403 或 challenge HTML 但浏览器能正常打开

## 不要做

- ❌ 用 `shell + curl` 反复请求 — 会被 ban IP
- ❌ 自己造 UA + cookie 试图绕 — Turnstile 是 JS 挑战, 不是单纯 cookie 校验
- ❌ 高 QPS 抓 bundle — 立刻触发限流

## 正确做法

1. **直接走真实浏览器**: 用 `js_reverse__*` (chrome-devtools-mcp) 的 `new_page` / `navigate_page` 让真 Chrome 跑过 Turnstile, 它会自动通过。
2. **复用已登录 Chrome**: `chrome-devtools-mcp` 支持 `--autoConnect` 模式, 你手动在常用 Chrome 登录后, agent 接管那个 session, cookie 自然带过去。
3. **静态 bundle 单独抓**: `_next/static/`, `assets/*.js` 这些**通常不走 CF challenge**, 拿到 URL 后用 curl 直抓即可 (但仍要尊重 rate limit, scope.rate_limit_per_second)。

## 反爬识别速查

| 现象 | 多半是 |
|---|---|
| 403 + `cf-ray` header | Cloudflare WAF |
| 503 + Turnstile widget | Bot challenge |
| HTML 只有几行 + `_cf_chl_opt` 函数 | Managed challenge |
| 200 但 body 是混淆 JS 跳转 | Browser integrity check |

## 反例 (撞过的坑)

- 用 `shell curl` 加自定义 UA `Mozilla/5.0 ...` → 还是 403, CF 不只看 UA
- 让 agent 重试 5 次 → 触发 rate limit, 接下来 30 分钟都 503

## 提示词片段 (引用本 skill 时可直接写给用户)

> 小霜大人, 检测到 Cloudflare 防护; 改用 chrome-devtools-mcp 走真实浏览器, 不要 curl。
