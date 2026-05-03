"""`secagent init` — one-shot setup wizard.

Asks four questions, writes ~/.secagent/llm.yaml (chmod 600), tests the
connection, prints sandbox status, creates the default engagement.

Goal: a person who has never seen the YAML files runs `secagent init` once,
then `secagent`, then talks to it.
"""
from __future__ import annotations

import getpass
import os
import re
import stat
import sys
from pathlib import Path
from typing import Optional

import yaml

from secagent.cli.bootstrap import (
    ensure_default_engagement,
    probe_llm_connection,
    probe_sandbox,
    user_llm_config_path,
    user_secagent_home,
)


# ============================================================
# helpers
# ============================================================

def _ask(prompt: str, default: Optional[str] = None, choices: Optional[list[str]] = None) -> str:
    """Prompt loop. Returns the trimmed answer. Re-asks on empty input unless a default exists."""
    suffix = ""
    if choices:
        suffix = f" [{'/'.join(choices)}]"
    if default is not None:
        suffix += f" (default: {default})"
    while True:
        try:
            ans = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(130)
        if not ans and default is not None:
            return default
        if not ans:
            print("  (required)")
            continue
        if choices and ans not in choices:
            print(f"  must be one of: {', '.join(choices)}")
            continue
        return ans


def _ask_secret(prompt: str) -> str:
    while True:
        try:
            v = getpass.getpass(f"{prompt}: ")
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(130)
        except Exception:
            # getpass can fail in unusual terminals; fall back to plain input
            print("  (note: input will be visible)")
            try:
                v = input(f"{prompt}: ")
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(130)
        v = v.strip()
        if v:
            return v
        print("  (required)")


def _confirm(prompt: str, default_yes: bool = False) -> bool:
    yn = "[Y/n]" if default_yes else "[y/N]"
    try:
        ans = input(f"{prompt} {yn}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(130)
    if not ans:
        return default_yes
    return ans in ("y", "yes")


# ============================================================
# preset templates per backend type
# ============================================================

# Common public proxies / direct providers + a default model id. The user
# can pick one and only fills in the API key, OR pick "custom" for full control.
_PRESETS = {
    "1": {
        "label": "Anthropic 直连",
        "kind":  "anthropic",
        "base_url": None,
        "default_model": "claude-sonnet-4-5-20250929",
    },
    "2": {
        "label": "DeepSeek (deepseek-chat / deepseek-reasoner)",
        "kind":  "openai_compat",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
    },
    "3": {
        "label": "Kimi (Moonshot)",
        "kind":  "openai_compat",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-32k",
    },
    "4": {
        "label": "SiliconFlow",
        "kind":  "openai_compat",
        "base_url": "https://api.siliconflow.cn/v1",
        "default_model": "deepseek-ai/DeepSeek-V3",
    },
    "5": {
        "label": "OpenRouter",
        "kind":  "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-sonnet-4",
    },
    "6": {
        "label": "OpenAI 兼容中转站 (自定义 base_url)",
        "kind":  "openai_compat",
        "base_url": None,            # user fills
        "default_model": None,
    },
    "7": {
        "label": "OpenAI 直连",
        "kind":  "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
    },
}


# ============================================================
# main wizard
# ============================================================

def run_init() -> int:
    print()
    print("=" * 60)
    print(" SecAgent 初始化向导")
    print("=" * 60)
    print()
    print("会做这几件事:")
    print("  1. 选 LLM 后端 → 写 ~/.secagent/llm.yaml (chmod 600)")
    print("  2. 测一下连接通不通")
    print("  3. 探测 js_execute 的沙箱能力 (docker / node-permission)")
    print("  4. 在 ~/.secagent/engagements/default 建一个默认 engagement")
    print()

    home = user_secagent_home()
    cfg_path = user_llm_config_path()

    if cfg_path.exists():
        print(f"已检测到现有配置: {cfg_path}")
        if not _confirm("覆盖?", default_yes=False):
            print("取消。如果只想加 backend，自己编辑那个文件即可。")
            return 0
        print()

    # ---------- 1. Pick backend ----------
    print("可选后端:")
    for k, p in _PRESETS.items():
        print(f"  [{k}] {p['label']}")
    print()
    choice = _ask("选哪个", default="1", choices=list(_PRESETS.keys()))
    preset = _PRESETS[choice]
    print()

    # ---------- 2. Collect details ----------
    backend_name = re.sub(r"[^a-z0-9_]+", "_", preset["label"].lower().split(" ")[0]) or "default"

    base_url = preset["base_url"]
    if base_url is None:
        base_url = _ask("base_url (例如 https://api.your-proxy.com/v1)")

    default_model = preset["default_model"] or ""
    model = _ask("model id", default=default_model if default_model else None)

    api_key = _ask_secret("API Key")

    # ---------- 3. Build cfg ----------
    backend_cfg: dict = {
        "type":       preset["kind"],
        "model":      model,
        "api_key":    api_key,
        "max_tokens": 8192,
        "temperature": 0.2,
    }
    if base_url and preset["kind"] == "openai_compat":
        backend_cfg["base_url"] = base_url

    # 中转站可能要求 default_headers / extra_body
    if choice == "6":
        print()
        print("如果中转站需要自定义 header（X-Tenant 等），现在填，不需要直接回车跳过。")
        if _confirm("加 default_headers?", default_yes=False):
            backend_cfg["default_headers"] = _collect_kv_pairs("header")
        if _confirm("加 extra_body 字段?", default_yes=False):
            backend_cfg["extra_body"] = _collect_kv_pairs("body field")

    cfg = {
        "default_backend": backend_name,
        "backends": {backend_name: backend_cfg},
    }

    # ---------- 4. Write config ----------
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    try:
        os.chmod(cfg_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except (OSError, NotImplementedError):
        pass  # Windows / no-op
    print()
    print(f"  -> 写入 {cfg_path}")

    # ---------- 5. Test connection ----------
    print()
    print("测试连接...")
    ok, msg = probe_llm_connection(cfg)
    if ok:
        print(f"  [ok] {msg}")
    else:
        print(f"  [fail] {msg}")
        print()
        print("  llm.yaml 已写入，但调用失败。可能原因:")
        print("    - API key 错")
        print("    - base_url 错")
        print("    - 模型 id 该后端不支持")
        print("    - 中转站要求 default_headers / extra_body")
        print(f"  改 {cfg_path} 后重跑 `secagent init` 或 `secagent llm`.")
        # don't fail the whole init; user can fix and retry

    # ---------- 6. Sandbox status ----------
    print()
    print("沙箱能力探测:")
    sb = probe_sandbox()
    docker = sb["docker"]
    nodep  = sb["node_permission"]
    docker_label = "[ok] ready" if docker["available"] else f"[--] {docker['reason']}"
    print(f"  docker:           {docker_label}")
    np_label = "[ok]" if nodep["available"] else "[--]"
    print(f"  node-permission:  {np_label} node {nodep['node_version']}")
    if not docker["available"]:
        print()
        print("  建议装 docker 以获得网络隔离 (node-permission 模式不阻网):")
        print("    curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER")

    # ---------- 7. Default engagement ----------
    print()
    eng = ensure_default_engagement()
    print(f"默认 engagement: {eng}")

    # ---------- 8. done ----------
    print()
    print("=" * 60)
    print(" 完成")
    print("=" * 60)
    print()
    print("现在可以:")
    print("  secagent              # 直接进 chat (默认 engagement)")
    print("  secagent target <url> # 给一个目标 URL，自动建专用 engagement 再进 chat")
    print()
    return 0


def _collect_kv_pairs(label: str) -> dict:
    """Collect key=value pairs from interactive input. Empty key ends."""
    out: dict = {}
    print(f"  输入 {label}（key=value 形式，每行一个；空行结束）:")
    while True:
        try:
            line = input("    > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return out
        if not line:
            return out
        if "=" not in line:
            print("    格式: key=value")
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
