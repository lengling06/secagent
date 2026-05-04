# SecAgent 改造方案

> 目标：把当前的 "ReAct 工作流" 改成真正会思考的 agent。
> 状态：草案，等小霜大人定方向后再动手。

---

## 1. 现状诊断（一句话）

**模型每轮直接吐 tool_calls，没思考、没规划、没反思；60 个工具里 3 套能干同一件事，模型决策疲劳；system prompt 一次性注入后被长上下文淡忘。**

证据（2026-05-04 talkai.info session）：

- Turn 1-3 用 3 个不同工具做"打开网页"
- 模型 `content=""`，框架硬塞假 narration "小霜大人，当前没有额外说明..."
- Turn 4 中转站 SSL hang 死，无超时保护

---

## 2. 借鉴的开源设计

| 项目 | 关键机制 | 我们要不要抄 |
|---|---|---|
| GenericAgent | L0-L4 分层记忆 / Plan tool / Skill library / 强制 checkpoint | ✅ Plan + Skill |
| Hermes (Nous) | 模型级 `<think>` 训练 / 工具调用与思考一体 | ❌ 模型层动不了，但可在 framework 强制 narration |
| Claude Code | `CLAUDE.md` 项目记忆 / 工具简洁 / 子 agent | ✅ CLAUDE.md 模式 |
| Cline / Aider | 角色 prompt 文件 / repo map / rolling summary | ✅ 角色 prompt 文件 |
| Cursor | `.cursorrules` 项目级规则 | ✅ 同上 |

### `soul.md` 模式（小霜大人提的）

确实是个共识做法：

- **Cline** 有 `cline_rules` / **Cursor** 有 `.cursorrules` / **Claude Code** 有 `CLAUDE.md`
- 共同点：**人格、口吻、约束、风格**单独成文，与"工具/SOP"分离
- 优势：用户改 soul 不会动到操作逻辑；agent 加载顺序固定先 soul 后 SOP

我们的对应：

```
secagent/prompts/
  soul.md           ← 新增：人格(小霜大人/称呼/语气/拒绝模式)
  system_sec.md     ← 现有：操作契约(scope/审批/forbidden)
  js_reverse_sop.md ← 现有：方法论
```

加载顺序：`soul.md → system_sec.md → js_reverse_sop.md → engagement/sop.md → checkpoint`

`soul.md` 草案内容（示例）：

```markdown
# 小霜大人的 SecAgent — 人格与口吻

## 身份
你是小霜大人雇佣的 web 安全研究助手, 专精 JS 逆向。
你不是聊天机器人, 是会思考的工程师。

## 口吻 (强制)
- 用户姓名: 小霜
- 每次工具调用前以 "小霜大人，" 起头, 用一句话写: 现状/目的/方法
- 失败/换方案: "小霜大人，..."
- 完成: "小霜大人，..."

## 思考模式
- 调工具前必须先有 narration, 否则你不是 agent, 是 spam
- 同一目标 不要 同时调多个等价工具试运气, 选一个
- 撞墙 2 次同样错误: 停, 总结, 问

## 拒绝模式
- 越权(scope外/forbidden) → 直接拒绝并解释
- 模糊请求 → ask_user, 不要瞎猜
- 上下文 70% → 先 update_working_checkpoint
```

---

## 3. 架构改造 — 5 个核心修复

### 🔴 P0-A. 删掉假 narration band-aid

文件：`secagent/core/loop.py:174`

```python
# 删除这段
elif response.tool_calls:
    yield f"小霜大人，当前没有额外说明，我先按现有线索继续调用工具：..."
```

理由：掩盖问题。

### 🔴 P0-B. 强制思考：无 narration 不许调工具

文件：`secagent/core/loop.py`

```python
response = llm.chat(messages=messages, tools=tools_schema)

# 新增：narration gate
if response.tool_calls and not (response.content or "").strip():
    yield "[reject] 没说话就想动手?\n"
    messages = [{
        "role": "user",
        "content": "你直接调了工具但一个字没说。先用一句中文'小霜大人，<现状>; <目的>; <方法>'再行动。"
    }]
    continue   # 不发 tool_results, 让 LLM 重写这一轮
```

可选加强：narration 必须包含"小霜大人"四个字才放行（防它糊弄）。

### 🔴 P0-C. 工具裁剪：60 → 18

**重复的入口收敛**：

| 现在 | 改成 |
|---|---|
| `playwright__*` (10+) + `js_reverse__*` (8+) + `shell` 都能开网页 | 留 `playwright__*` (核心 4 个) + 高层 `fetch_page(url, mode=auto/curl/browser)` |
| MCP 默认全注册 | 按 profile (js_reverse / pentest / minimal) 过滤后注册 |

**目标工具清单**（约 18 个）：

```
核心 6: think, plan, ask_user, task_complete, update_working_checkpoint, add_finding
文件 3: file_read, file_write, file_patch
JS 4:   js_beautify, sourcemap_fetch, js_execute, har_analyze
网络 2: fetch_page, shell
浏览 3: browser_navigate, browser_click, browser_eval (从 playwright MCP 精选)
```

### 🔴 P0-D. Reminder 注入：每轮 user 消息前缀

文件：`secagent/core/loop.py`

```python
SOUL_REMINDER = "[规则提醒: 工具调用前必须用一句中文写现状/目的/方法, 以'小霜大人，'起头]\n\n"

# 每次构造 next-turn user message 时:
messages = [{
    "role": "user",
    "content": SOUL_REMINDER + "\n".join(next_prompts),
    "tool_results": tool_results,
}]
```

中转站对 system prompt 不可靠，user prefix 它一定看。

### 🔴 P0-E. 网络硬超时

文件：`secagent/llm/openai_session.py`

```python
import httpx
self._client = OpenAI(
    base_url=base_url,
    api_key=key,
    default_headers=default_headers or {},
    timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0),
    max_retries=max_retries,
)
```

防 SSL 卡死。

---

## 4. P1 进阶 — 让它真"会思考"

### P1-F. `think` 工具（轻量版）

不是真工具，是个空操作但**强制存在**：

```python
# tools/think.py
def think(observation: str, plan: str, next_action: str) -> StepOutcome:
    return StepOutcome.cont(
        data={"thought_logged": True},
        prompt="now act on the next_action you just stated."
    )
```

效果：模型必须把"我看到啥/我打算干啥/下一步啥"显式写出来才能继续。比单纯靠 prompt 引导可靠 3 倍。

### P1-G. `plan` 工具（任务分解）

```python
def plan(goal: str, steps: list[str]) -> StepOutcome:
    # 落盘到 engagement/state/plan.md
    # 后续每步执行后调 step_done(idx) 标记
```

用户首次说"逆向 talkai.info"时，agent 第一反应是 `plan(...)` 而不是直接动手。

### P1-H. Skill library

```
~/.secagent/skills/
  cloudflare_protected.md    ← "对 CF 站用 playwright 不用 curl"
  next_js_bundle.md          ← "_next/static/chunks 找 entry"
  webpack_obfuscator.md      ← "用 webcrack 还原"
```

每次新会话开头扫这个目录，匹配当前 target 特征的 skill 注入到 system prompt。

---

## 5. 落地顺序与代价

| 阶段 | 内容 | 改动文件 | 估计 LOC |
|---|---|---|---|
| **Step 1** | P0-A/B/D + 创建 `soul.md` | loop.py, repl.py, prompts/ | ~80 |
| **Step 2** | P0-E 硬超时 + mixin fallback 加强 | openai_session.py, mixin.py | ~30 |
| **Step 3** | P0-C 工具裁剪 + MCP profile 过滤 | registry.py, mcp/manager.py | ~120 |
| **Step 4** | P1-F `think` 工具 | tools/think.py + 注册 | ~40 |
| **Step 5** | P1-G `plan` 工具 + state/plan.md | tools/plan.py | ~80 |
| **Step 6** | P1-H skill library 扫描 | tools/skills.py + repl.py | ~100 |

**总计 ~450 行**。Step 1+2 是当晚就能跑起来的最小集，预计 1.5 小时。

---

## 6. 决策点（需要小霜大人确认）

1. **路线**：
   - [ ] 只做 Step 1+2（最小修复，今晚能用）
   - [ ] 做 Step 1-4（加 think 工具，agent 真的开始"思考"）
   - [ ] 全做 Step 1-6（含 plan + skill，是正经 agent 了）

2. **soul.md 内容**：上面草案 OK 还是要改？
   - "小霜大人" 这个称呼保留？
   - 思考模式还想加什么硬规则？

3. **工具裁剪取舍**：
   - playwright MCP 和 js_reverse (chrome-devtools) MCP **二选一**，我倾向留 playwright（更稳、更通用）。你的意见？
   - shell 工具要不要砍掉？(它太万能, 会让 agent 偷懒不用专用工具)

4. **中转站策略**：
   - 接受继续用 Deepseek-V4-Pro（便宜但工具协议不稳）
   - 或在 llm.yaml 加 claude-sonnet-4-5 当 mixin primary（贵但靠谱）

确认后我按你选的执行。
