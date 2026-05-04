"""Tool registry.

Each tool = (schema, fn, meta).
- schema: OpenAI-style JSON Schema for LLM
- fn: callable(args, ctx) -> StepOutcome
- meta: extra metadata for the safety pipeline:
    - target_keys: list of arg keys whose values should be scope-checked
    - operation: human-readable op kind, used to look up require_approval
    - side_effects: "read" | "write" | "exec" | "network"
    - category: "filesystem" | "shell" | "network" | "memory" | ...
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from secagent.core.outcome import StepOutcome


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, dict] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        fn: Callable[..., StepOutcome],
        *,
        target_keys: Optional[list[str]] = None,
        operation: Optional[str] = None,
        side_effects: str = "read",
        category: str = "misc",
    ) -> None:
        self._tools[name] = {
            "schema": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
            "fn": fn,
            "meta": {
                "target_keys": target_keys or [],
                "operation": operation or name,
                "side_effects": side_effects,
                "category": category,
            },
        }

    def schemas(self) -> list[dict]:
        return [t["schema"] for t in self._tools.values()]

    def meta(self, name: str) -> Optional[dict]:
        t = self._tools.get(name)
        return t["meta"] if t else None

    def call(self, name: str, args: dict, ctx: dict) -> StepOutcome:
        t = self._tools.get(name)
        if t is None:
            return StepOutcome.error(f"Unknown tool: {name}")
        return t["fn"](args, ctx)

    def names(self) -> list[str]:
        return list(self._tools.keys())


def build_default_registry(profile: str = "js_reverse") -> ToolRegistry:
    """Wire up the default native tool set.

    Profiles:
      - "js_reverse" (default): JS 逆向 + 文件 + checkpoint + 审计相关。**不挂 recon**, 防止
        模型一上来就 subfinder / nmap 跑偏。
      - "js_reverse_plus_recon": 上面 + recon 套件 (subdomain_enum / port_scan / http_probe /
        dns_resolve)。需要找泄漏的 staging / 备用 host 时切到这个 profile。
      - "pentest": 等价于 plus_recon, 别名。
      - "minimal": 只有 ask_user / add_finding / file_*, 调试用。
    """
    from secagent.tools import (
        ask_user,
        checkpoint,
        filesystem,
        findings,
        js_analysis,
        js_format,
        recon,
        shell,
        sourcemap,
        task_complete,
    )

    reg = ToolRegistry()

    if profile == "minimal":
        ask_user.register(reg)
        findings.register(reg)
        filesystem.register(reg)
        return reg

    # js_reverse / js_reverse_plus_recon / pentest 都包含基础套件
    shell.register(reg)
    filesystem.register(reg)
    ask_user.register(reg)
    findings.register(reg)
    js_analysis.register(reg)
    js_format.register(reg)             # C14 js_beautify
    sourcemap.register(reg)             # C15 sourcemap_fetch
    checkpoint.register(reg)            # B8 update_working_checkpoint
    task_complete.register(reg)         # C13 task_complete

    if profile in ("js_reverse_plus_recon", "pentest"):
        recon.register(reg)

    return reg
