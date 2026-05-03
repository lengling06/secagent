# SecAgent — 使用指南

> 三行命令开跑 → 自然语言对话 → 拿产物。下面按"先用起来"再"调细节"的顺序来。

---

## 1. 三行命令开跑

```bash
# A. 装包
cd secagent
pip install -e .                       # 一次

# B. 配置 LLM (交互式向导)
secagent init                          # 一次
                                       # 选 LLM 类型 / 填 base_url / 填 api_key
                                       # 自动测连接 / 探沙箱 / 建默认 engagement

# C. 直接进 chat
secagent                               # 默认 engagement，刮板模式
# 或 secagent target https://talkai.info/   # 直接给目标
```

`secagent init` 长这样（互动）：

```
============================================================
 SecAgent 初始化向导
============================================================

可选后端:
  [1] Anthropic 直连
  [2] DeepSeek (deepseek-chat / deepseek-reasoner)
  [3] Kimi (Moonshot)
  [4] SiliconFlow
  [5] OpenRouter
  [6] OpenAI 兼容中转站 (自定义 base_url)
  [7] OpenAI 直连

选哪个 [1/2/.../7] (default: 1): 6
base_url (例如 https://api.your-proxy.com/v1): https://api.your-proxy.com/v1
model id: claude-sonnet-4
API Key: ********

如果中转站需要自定义 header（X-Tenant 等），现在填，不需要直接回车跳过。
加 default_headers? [y/N]: n
加 extra_body 字段?  [y/N]: n

  -> 写入 /home/you/.secagent/llm.yaml

测试连接...
  [ok] reply: ok

沙箱能力探测:
  docker:           [ok] ready
  node-permission:  [ok] node 20.10.0

默认 engagement: /home/you/.secagent/engagements/default

============================================================
 完成
============================================================

现在可以:
  secagent              # 直接进 chat (默认 engagement)
  secagent target <url> # 给一个目标 URL，自动建专用 engagement 再进 chat
```

---

## 2. 真实使用：怎么"对话"

三种入口，效果一样：

### 2a. 直接给目标 URL（最快）

```bash
$ secagent target https://talkai.info/

目标 URL: https://talkai.info/
建议 engagement:
  名称:   talkai.info_20260503
  路径:   /home/you/.secagent/engagements/talkai.info_20260503
  scope:  talkai.info, *.talkai.info
  授权方: self (local analysis)

确认创建? [Y/n]: y
已创建: /home/you/.secagent/engagements/talkai.info_20260503

=== SecAgent — engagement: talkai.info_20260503 ===
scope: talkai.info, *.talkai.info
llm:   proxy / claude-sonnet-4
tools: 13 registered  (`/tools` to list)
js_execute sandbox:
  docker:           [ok] ready
  node-permission:  [ok] node 20.10.0
  raw (no sandbox): [ok] available

> 我想分析这个目标: https://talkai.info/。按 SOP 进行。

[agent 自动开跑 SOP 8 阶段...]
```

### 2b. 默认 engagement，对话里贴 URL

```bash
$ secagent

=== SecAgent — engagement: default ===
scope: (local only — no network targets)
llm:   proxy / claude-sonnet-4

> 帮我逆向 https://talkai.info/ 的登录请求加密参数

[detected target: https://talkai.info/]
  host 'talkai.info' is not in current engagement 'default' scope.
  options:
    [Y] 新建 engagement 'target_talkai.info_<today>' (推荐)
    [s] 留在当前 engagement (网络操作会被 scope 拒绝)
    [n] 取消这条输入
  选 [Y/s/n]: y
  -> creating: /home/you/.secagent/engagements/talkai.info_20260503
     scope: talkai.info, *.talkai.info

-> 切到 engagement: talkai.info_20260503

[agent 自动接着跑你刚才那句话]
```

### 2c. 已有 engagement，按名进

```bash
$ secagent chat -e talkai.info_20260503
```

或 `--engagement` 接绝对路径也行。

---

## 3. 一段完整的 JS 反向对话（示例）

```
> 帮我看 talkai.info 登录请求的 sign 头怎么算的

[agent]
Plan (8-phase JS reverse SOP):
1. Pin target: 登录请求 (POST /api/auth/login), sign header
2. Capture: 用 playwright 抓
3-8. Locate JS, deobfuscate, find function, trace inputs, sandbox verify, document

Probe → playwright__browser_navigate {"url":"https://talkai.info/login"}
[approval needed: scope.require_approval matched]
approve [y/N]> y

(...agent 一步步跑，遇到 require_approval 才停下来问你...)

[agent]
找到 sign 函数: app.main.deob.js:1247
  function _sign(method, path, body, ts) {
    return HMAC_SHA256(_KEY, method + path + body + ts).toString(Hex)
  }

→ js_execute (docker sandbox, --network none) 验证：
  inputs:  {method:"POST", path:"/api/auth/login", body:"...", ts:1683...}
  expected: "9a3f2e..."
  got:      "9a3f2e..."
  ✓ matches

→ add_finding "talkai-sign-algorithm.md"
→ 附 Python 端口实现 + 测试向量

============================================================
 Session over
============================================================
  engagement:       /home/you/.secagent/engagements/talkai.info_20260503
  duration:         4m 23s
  findings written: 1    (.../findings)
  audit log lines:  127  (.../audit.jsonl)
  js files dumped:  4    (.../js)
  js_execute runs:  3    (.../.tmp)

  下次写报告时直接拿 findings/*.md 拼装。
```

---

## 4. 文件去哪了

```
~/.secagent/
├── llm.yaml                          ← `secagent init` 写
└── engagements/
    ├── default/                      ← 默认刮板（无网络目标）
    │   ├── scope.yaml
    │   ├── mcp.json
    │   └── audit.jsonl
    └── talkai.info_20260503/         ← `target` 或 chat 里 URL 触发
        ├── scope.yaml                ← 法律/授权边界
        ├── mcp.json                  ← 自动填 js-reverse + playwright
        ├── audit.jsonl               ← 每个工具调用的 before/after
        ├── findings/
        │   └── 2026-05-03-talkai-sign.md
        ├── recon/
        ├── js/
        │   └── talkai.info/
        │       ├── main.js
        │       └── main.deob.js
        └── .tmp/
            └── js_run_<ts>.js        ← js_execute 留下的脚本（审计回放）
```

写报告时 `cat ~/.secagent/.../findings/*.md` 直接拼。

---

## 5. `js_execute` 沙箱

跑任意 Node 代码 → 默认走沙箱。

| 模式 | 网络 | 文件系统 | 进程能力 | 依赖 |
|------|------|----------|----------|------|
| **`docker`**（推荐） | `--network none` 完全断网 | 根 FS 只读 + 64MB tmpfs `/tmp` | nobody 用户、cap-drop ALL、no-new-privileges、PID/CPU/RAM 限额 | docker daemon |
| **`node-permission`** | ⚠️ **不能阻断** | `--allow-fs-read=script` `--allow-fs-write=.tmp` | 同 host | Node ≥ 20 |
| **`raw`** | 全开 | 全开 | 全开 | Node |
| **`auto`**（默认） | 自动选最强可用：docker → node-permission → 拒绝（**绝不**降级到 raw） |

REPL 内 `/sandbox` 看当前能力。

**装 docker（强烈推荐）：**

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
docker pull node:20-alpine     # 预拉沙箱镜像 (~80MB)
docker run --rm hello-world    # 验证
```

**自己验沙箱真断网：**

```
> 在 docker 沙箱里跑这段：
  fetch('http://example.com').then(r=>r.text()).catch(e=>'BLOCKED: '+e.message)
```

预期返回 `"BLOCKED: fetch failed"`。不是这个就别用。

---

## 6. MCP 工具自动接入

`secagent init` 检测到 `node` + `npx`，会自动给新 engagement 写一个默认 `mcp.json`：

```json
{
  "mcpServers": {
    "js-reverse": {
      "command": "npx",
      "args": ["-y", "js-reverse-mcp"],
      "target_keys": { "navigate": ["url"] },
      "approval_required": ["evaluate_script", "trace_function", ...]
    },
    "playwright": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"],
      "target_keys": { "browser_navigate": ["url"] },
      "approval_required": ["browser_evaluate"]
    }
  }
}
```

第一次启动时 npx 会拉包（慢一次），之后秒启动。

REPL 启动看到 `[MCP] connected: js-reverse` / `[MCP] connected: playwright` 就是接上了。
失败的话不阻塞——agent 还能用内置工具。

**不想要默认 MCP？** 编辑 `<engagement>/mcp.json`，把 `mcpServers` 改成 `{}`。

---

## 7. REPL 斜杠命令

```
/tools                查看注册了哪些工具（含 MCP 接入的）
/llm                  查看当前 LLM backend / 模型
/switch <name>        切换到 llm.yaml 里的另一个 backend
/sandbox              重新探测 js_execute 沙箱可用性
/quit                 退出
```

空行也会退出。

---

## 8. 常见错误

| 现象 | 原因 | 解决 |
|------|------|------|
| `没找到 ~/.secagent/llm.yaml` | 没跑 init | `secagent init` |
| `[FATAL] cannot build LLM session` | API key 错 / base_url 错 / 模型 id 不支持 | `secagent init` 重配，或编辑 `~/.secagent/llm.yaml` |
| `Scope violation: target 'X' is NOT in authorized scope` | 当前 engagement 没包含这个 host | 给 chat 一个含 URL 的输入 → 它会问你要不要新建 |
| `Operation 'X' is not in scope.allowed_operations` | engagement 没开这个 op | 编辑 `<engagement>/scope.yaml` 的 `allowed_operations` |
| `js_execute: no sandbox available` | 没 docker 也没 Node 20+ | 装 docker（推荐）或升级 Node |
| `js_execute: docker mode unavailable` | docker daemon 没起 / 用户没在 docker 组 | `sudo systemctl start docker` + `usermod -aG docker $USER` |
| `[MCP] failed to connect ...` | mcp.json 路径错 / 包没装 | 看 `<engagement>/mcp.json`，`npx -y <pkg>` 试一下 |
| `[FATAL] scope.yaml has expired` | engagement 过期（默认 30 天） | 改 `expires_at`（续约后再改） |
| LLM 回复但工具一直不调用 | 中转站可能不支持 native tool calling | `/switch` 到一个直连 backend 试试 |

---

## 9. Linux VM 部署 checklist

```bash
# 1. 系统 + Python
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git curl

# 2. Node (js_execute + MCP 都要)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# 3. Docker (js_execute 沙箱推荐用 docker 模式)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
docker pull node:20-alpine

# 4. 可选：ProjectDiscovery 工具链 (recon)
sudo apt-get install -y nmap dnsutils
curl -LO https://go.dev/dl/go1.22.0.linux-amd64.tar.gz
sudo tar -C /usr/local -xzf go1.22.0.linux-amd64.tar.gz
echo 'export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin' >> ~/.bashrc
source ~/.bashrc
go install -v github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest
go install -v github.com/projectdiscovery/httpx/cmd/httpx@latest
go install -v github.com/projectdiscovery/dnsx/cmd/dnsx@latest

# 5. SecAgent
git clone <你的仓库> ~/secagent
cd ~/secagent
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# 6. 一次性配置
secagent init

# 7. 跑
secagent target https://your-target.example/
```

---

## 10. 给 power user：dev 模式 + 多 backend + scope 调优

### 多 backend + fallback

`~/.secagent/llm.yaml`：

```yaml
default_backend: proxy
backends:
  proxy:
    type: openai_compat
    base_url: https://api.your-proxy.com/v1
    api_key: sk-xxx                    # 或 api_key_env: PROXY_API_KEY
    model: claude-sonnet-4
    default_headers: {X-Tenant: ...}   # 中转站要的话
    extra_body: {route: anthropic}     # 中转站要的话
  claude:
    type: anthropic
    api_key: sk-ant-...
    model: claude-sonnet-4-5-20250929
  deepseek:
    type: openai_compat
    base_url: https://api.deepseek.com/v1
    api_key: ...
    model: deepseek-chat

mixin:
  primary: proxy
  fallback_order: [claude, deepseek]
```

REPL 内 `/llm` 看当前，`/switch <name>` 切。

### 自定义 engagement scope

`<engagement>/scope.yaml`：

```yaml
in_scope:
  domains:
    - "*.acme.com"        # fnmatch 通配
  ips:
    - "203.0.113.0/24"    # CIDR
  apis:
    - "https://api.acme.com/v1/"

out_of_scope:
  domains:
    - "admin.acme.com"    # 子域虽然在 *.acme.com 里，单独排除

allowed_operations: [...]
forbidden_operations: [dos, bruteforce_password, ...]
require_approval: [sql_injection_payload, js_execute, ...]

network:
  proxy: "http://127.0.0.1:8080"   # Burp 代理
  rate_limit_per_second: 5
```

### Dev 模式（仓库内 engagements/）

```bash
secagent repl --engagement example
# 走的是 <repo>/engagements/example/, 不是 ~/.secagent/engagements/
```

---

## 11. 安全提醒

- **scope.yaml 是法律文件**。每次新目标都让 SecAgent 帮你建一个 engagement，并填合理的 `authorized_by`（自有资产 / SRC 范围 / 客户名）。
- **forbidden_operations 不可用 ask_user 绕过**：DoS、爆破密码、数据外带、破坏写、钓鱼，永远 no。
- **audit.jsonl 不要删**——万一甲方 / 法务 有疑问，这是你的证据链。
- **API key**：`secagent init` 写在 `~/.secagent/llm.yaml` 并 chmod 600。如果机器是共享的，改成 `api_key_env: VAR` 形式，自己 export。
- **第三方中转站的 ToS 自己看**：有些中转站日志你的 prompt，安全场景慎用——可以用 mixin primary=直连 + fallback=中转。
