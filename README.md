# SecAgent

> 网络安全 / 网页 JS 逆向 agent。一行装包，一行配置，剩下用自然语言对话。
> Engagement 项目化 + Scope 授权边界 + 灵魂契约 + Narration 门控 + MCP 工具聚合 + 沙箱保护。

## 30 秒开跑

**装**（Linux / macOS / WSL2，自动装 python + pipx + node + docker）：

```bash
curl -fsSL https://raw.githubusercontent.com/lengling06/secagent/master/install.sh | bash
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

- **JS 反混淆 + 加密参数还原**（接 [`chrome-devtools-mcp`](https://github.com/ChromeDevTools/chrome-devtools-mcp) — Google 官方，观察导向）
- **HAR 解析、bundle diff、JS 沙箱验证算法**（内置）
- **sourcemap 一键拉取 + bundle 美化**（内置 `sourcemap_fetch` / `js_beautify`）
- **任务规划 + 思考门控 + 经验库**（内置 `plan` / `think` / `skills` 工具）
- **子域枚举 / web 探活 / 端口扫**（按需开 `js_reverse_plus_recon` profile）
- **Findings 文档化 + 全程审计**

## 它怎么思考（不是工作流，是 agent）

每次 LLM 调工具前必须先用一句中文 narration 起头：

> 小霜大人，主页已抓取，加载了 5 个 JS bundle; 下一步定位签名入口; 用 grep 搜 `X-Sign` 字面量。

没说话直接调工具会被 **narration gate 拒绝**，要求重写。这是 `soul.md` 的人格契约 + `loop.py` 的运行时门控共同作用的结果。

复杂任务必须先 `plan(goal, steps)`，每步完成调 `step_done(idx, summary)`，状态落到 `engagement/state/plan.md`，跨 session 存活。

## 5 道安全闸

每次工具调用按顺序过 5 道闸：

1. **Operation allowed** — `scope.allowed_operations` 没列就拒
2. **Scope check** — 目标不在 `in_scope` 或在 `out_of_scope` 就拒
3. **Policy check** — `rm -rf /` / `mkfs` / 内核级危险命令 hard fail
4. **Approval gate** — `require_approval` 列表里的操作必须手动 y/N
5. **Audit log** — 调用前后各写一行 JSONL

`js_execute` 额外走沙箱（docker `--network none` 默认）。中转站 SSL hang 由 httpx 硬超时（connect=10s, read=120s）兜底。

## 文件结构

```
~/.secagent/
├── llm.yaml                          ← `secagent init` 写
├── skills/                           ← 你自己积累的招数 (可选)
│   └── *.md
└── engagements/
    ├── default/                      ← 默认刮板，无网络目标
    └── talkai.info_20260503/         ← `secagent target` / 聊天里贴 URL 时自动建
        ├── scope.yaml                ← 法律/授权边界
        ├── mcp.json                  ← 默认仅 chrome-devtools-mcp; 旧版有 playwright 残留时跑 `secagent doctor --fix`
        ├── sop.md                    ← 可选方法论 prompt
        ├── audit.jsonl               ← 每个工具调用的 before/after
        ├── findings/                 ← 结构化发现，写报告时直接拼装
        ├── recon/                    ← subfinder/httpx 结果 (recon profile)
        ├── js/                       ← 拉下来的 bundle
        ├── state/
        │   ├── plan.md               ← 当前任务计划 (跨 session 存活)
        │   └── checkpoint.md         ← 进度 checkpoint (压缩历史时不丢)
        └── .tmp/                     ← js_execute 留下的脚本（审计回放）
```

## CLI 速查

| 命令 | 作用 |
|---|---|
| `secagent init` | 配 LLM (写 `~/.secagent/llm.yaml`) |
| `secagent` 或 `secagent chat` | 进默认 engagement |
| `secagent target <url>` | 自动建 url-scoped engagement 并进入 |
| `secagent chat -e <name>` | 进指定 engagement |
| `secagent audit -e <name>` | 看审计日志 |
| `secagent scope -e <name>` | 看 scope 摘要 |
| `secagent llm` | 看 LLM 配置 |
| `secagent doctor` | 检查所有 engagement 配置漂移 |
| `secagent doctor --fix` | 自动修复 (备份到 `*.bak`) |

REPL 内: `/tools` 列工具，`/llm` 看当前 backend，`/switch <name>` 切换，`/sandbox` 看 js_execute 沙箱状态，`/quit` 退出。

## 提示词系统

加载顺序（top-down 拼成 system prompt）：

```
secagent/prompts/soul.md            ← 人格契约: 小霜大人称呼 / 思考模式 / 拒绝模式
secagent/prompts/system_sec.md      ← 操作契约: 5 道闸 / 审批 / 工具优先级
secagent/prompts/js_reverse_sop.md  ← 默认方法论: JS 逆向 8 步流程
secagent/prompts/skills/*.md        ← 内置经验: cloudflare / nextjs / webpack 反混淆
~/.secagent/skills/*.md             ← 你自己加的经验 (优先级最高)
<engagement>/sop.md                 ← 项目级覆盖 (可选)
<engagement>/state/checkpoint.md    ← 上次进度 (压缩后仍存活)
scope.yaml summary                  ← 当前 scope 摘要
```

`soul.md` 是人格，写口吻和思考模式；`skills/*.md` 是经验，写"对 X 类站点用 Y 招数"。两者分离，agent 自更新 skills 不污染人格。

## LLM 配置（多 backend + fallback）

```yaml
# ~/.secagent/llm.yaml
default_backend: proxy
backends:
  proxy:
    type: openai_compat
    base_url: "https://your-proxy.example.com/v1"
    api_key: "sk-..."
    model: "claude-sonnet-4-5"
    context_window: 200000        # 中转站偷偷截窗口时设保守值
  claude:
    type: anthropic
    api_key: "sk-ant-..."
    model: "claude-sonnet-4-5"
  deepseek:
    type: openai_compat
    base_url: "https://api.deepseek.com/v1"
    api_key: "..."
    model: "deepseek-chat"

mixin:
  primary: proxy
  fallback_order: [claude, deepseek]
```

中转站警告：很多 OpenAI 兼容代理会把工具调用降级成纯文本（如 DeepSeek 的 DSML 格式）。secagent 在 `openai_session._parse_text_tool_calls` 做了兜底解析，但仍建议用真模型（Anthropic 官方 / 主流厂商）。`/llm` 看当前，`/switch <name>` 切换。

## 设计文档

[../secagent_design.md](../secagent_design.md) — Engagement 概念、Scope 是灵魂、L0-L4 红线、为什么不用通用 agent。

[docs/refactor_plan.md](docs/refactor_plan.md) — 工作流→真 agent 的改造记录（soul.md / narration gate / think+plan / skills 的来龙去脉）。
