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


def build_default_registry() -> ToolRegistry:
    """Wire up the default native tool set."""
    from secagent.tools import (
        ask_user,
        filesystem,
        findings,
        js_analysis,
        recon,
        shell,
    )

    reg = ToolRegistry()
    shell.register(reg)
    filesystem.register(reg)
    ask_user.register(reg)
    findings.register(reg)
    recon.register(reg)
    js_analysis.register(reg)
    return reg
