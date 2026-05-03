# SecAgent

> 网络安全 / 网页逆向 agent。一行装包，一行配置，剩下用自然语言对话。
> Scope 授权 + Engagement 项目化 + MCP 工具聚合 + 沙箱保护。

## 30 秒开跑

**装**（Linux / macOS / WSL2，自动装 python + pipx + node + docker）：

```bash
curl -fsSL https://raw.githubusercontent.com/CHANGE-ME/secagent/main/install.sh | bash
```

或在仓库目录里本地装：

```bash
cd secagent && bash install.sh
```

**用**：

```bash
secagent init                         # 一次性，选 LLM 类型 + 填 key
secagent target https://example.com/  # 给目标 URL 直接进 chat
```

或：

```bash
secagent                              # 进默认 engagement
> 帮我逆向 https://example.com/ 的登录请求加密参数
[agent 自动建专用 engagement → 跑 SOP → 给产物]
```

完整说明：[USAGE.md](USAGE.md)。

## 它能干什么

- **JS 反混淆 + 加密参数还原**（接 `js-reverse-mcp`）
- **浏览器抓包 + XHR 跟踪**（接 `@playwright/mcp`）
- **HAR 解析、bundle diff、JS 沙箱验证算法**（内置）
- **子域枚举 / web 探活 / 端口扫**（内置 ProjectDiscovery 工具链）
- **Findings 文档化 + 审计可追溯**

每次工具调用按顺序过 5 道闸：

1. **Operation allowed** — `scope.allowed_operations` 没列就拒
2. **Scope check** — 目标不在 `in_scope` 或在 `out_of_scope` 就拒
3. **Policy check** — `rm -rf /` / `mkfs` / 内核级危险命令 hard fail
4. **Approval gate** — `require_approval` 列表里的操作必须手动 y/N
5. **Audit log** — 调用前后各写一行 JSONL

`js_execute` 额外走沙箱（docker `--network none` 默认）。

## 文件结构

```
~/.secagent/
├── llm.yaml                          ← `secagent init` 写
└── engagements/
    ├── default/                      ← 默认刮板，无网络目标
    └── talkai.info_20260503/         ← `secagent target` / 聊天里贴 URL 时自动建
        ├── scope.yaml                ← 法律/授权边界
        ├── mcp.json                  ← MCP servers (检测到 node 自动填 js-reverse + playwright)
        ├── sop.md                    ← 可选方法论 prompt
        ├── audit.jsonl               ← 每个工具调用的 before/after
        ├── findings/                 ← 结构化发现，写报告时直接拼装
        ├── recon/                    ← subfinder/httpx 结果
        ├── js/                       ← 拉下来的 bundle
        └── .tmp/                     ← js_execute 留下的脚本（审计回放）
```

## 完整使用指南

[USAGE.md](USAGE.md) — 30 秒开跑、LLM 配置、MCP 接入、沙箱说明、真实示例对话、Linux VM 部署 checklist、常见错误。

## 设计文档

[secagent_design.md](../secagent_design.md) — Engagement 概念、Scope 是灵魂、L0-L4 红线、为什么不用通用 agent。

## 贡献 / 改 backend

```yaml
# ~/.secagent/llm.yaml — 多 backend + fallback
default_backend: proxy
backends:
  proxy:    { type: openai_compat, base_url: "https://...", api_key: "...", model: "..." }
  claude:   { type: anthropic, api_key: "sk-ant-...", model: "claude-sonnet-4" }
  deepseek: { type: openai_compat, base_url: "https://api.deepseek.com/v1", api_key: "...", model: "deepseek-chat" }

mixin:
  primary: proxy
  fallback_order: [claude, deepseek]
```

REPL 内 `/llm` 看当前，`/switch <name>` 切换。
