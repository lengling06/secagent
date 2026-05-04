# Next.js / Vercel 站点

**何时用此 skill**: 目标 HTML 含以下信号:
- `<script id="__NEXT_DATA__"` (强信号)
- 资源路径 `/_next/static/chunks/...`
- `<div id="__next">` root
- 响应 header `x-vercel-id` / `x-nextjs-cache`

## 入口文件定位 (核心套路)

Next.js 把代码切成多个 chunk:

```
/_next/static/chunks/
  framework-<hash>.js      ← React 等基础库, 一般不要看
  webpack-<hash>.js         ← runtime, 一般不要看
  main-<hash>.js            ← Next runtime + hooks
  pages/_app-<hash>.js      ← 全局 App 包装, **常含全局拦截器/签名注入**
  pages/<route>-<hash>.js   ← 路由页面级
  <id>-<hash>.js            ← 按需 split 出来的业务模块
```

**搜签名/加密优先级**:
1. `pages/_app-*.js` → 看有没有 axios.interceptors / fetch override
2. 业务路由对应的 `pages/<route>-*.js` 或 `<id>-*.js` (从 `__NEXT_DATA__.props` 反推哪个 route)
3. 关键字搜全部 chunk: `sign|hmac|aes|signature|x-sign|encrypt`

## sourcemap 抓取

Next.js 生产模式默认**不出 sourcemap** (`productionBrowserSourceMaps: false`)。但有时候开发偷懒:
- 试 `<chunk_url>.map`
- 看 chunk 末尾 `//# sourceMappingURL=` 注释
- 用我们的 `sourcemap_fetch(js_url)` 工具一次试三种方法

如果拉到 .map → **直接赢**, 还原源码省掉美化步骤。

## __NEXT_DATA__ 是金矿

```js
JSON.parse(document.getElementById('__NEXT_DATA__').textContent)
```

里面通常有:
- `buildId` → 拼具体 chunk URL
- `runtimeConfig` / `publicRuntimeConfig` → API base URL, 第三方 key (有时漏)
- `props.pageProps` → 当前页面的初始数据, 含已脱敏的 token/user_id 等

**第一步先抓 `__NEXT_DATA__`**, 再决定看哪个 chunk。

## 反例

- 上来就美化所有 chunk → 浪费时间, 80% 是 framework 代码
- 在 `framework-*.js` 里搜签名 → 永远找不到, 那是 React

## 提示词片段

> 小霜大人, 这是 Next.js 站; 先抓 `__NEXT_DATA__` 拿 buildId, 然后定位 `_app` 和当前 route 对应的 chunk, 在那两个文件里搜签名。
