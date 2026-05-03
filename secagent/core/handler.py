"""Handler: dispatches tool calls through the pre-call pipeline.

Pipeline (in order):
  1. Scope check     — is the target in authorized scope?
  2. Policy check    — is this a dangerous command pattern?
  3. Approval gate   — does this need ask_user first?
  4. Audit log       — write before-call record
  ─── execute ───
  5. Audit log       — write after-call record (with result/error)
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Any

from secagent.core.outcome import StepOutcome
from secagent.safety.audit import AuditLog
from secagent.safety.policy import PolicyEngine
from secagent.tools.registry import ToolRegistry
from secagent.tools.scope import Scope


class Handler:
    """Wraps a ToolRegistry with the safety pipeline."""

    def __init__(
        self,
        registry: ToolRegistry,
        scope: Scope,
        policy: PolicyEngine,
        audit: AuditLog,
        engagement_dir: Path,
        approval_callback=None,    # callable(prompt) -> bool, set by REPL
        prompt_callback=None,      # callable(question, candidates) -> str
    ):
        self.registry = registry
        self.scope = scope
        self.policy = policy
        self.audit = audit
        self.engagement_dir = engagement_dir
        self.approval_callback = approval_callback or (lambda _: False)
        self.prompt_callback = prompt_callback or (lambda q, c: "")
        self.current_turn = 0
        self.max_turns = 0

    def dispatch(self, tool_name: str, args: dict) -> StepOutcome:
        """The single entry point for all tool calls."""
        t0 = time.time()
        meta = self.registry.meta(tool_name)

        # context for nested tools (e.g. shell needs scope)
        ctx = {
            "scope": self.scope,
            "policy": self.policy,
            "audit": self.audit,
            "engagement_dir": self.engagement_dir,
            "approval_callback": self.approval_callback,
            "prompt_callback": self.prompt_callback,
        }

        # ===== 1. unknown tool =====
        if meta is None:
            self.audit.write("tool_unknown", tool=tool_name, args=args)
            return StepOutcome.error(f"Unknown tool: {tool_name}")

        op_kind = meta.get("operation", tool_name)

        # ===== 2. operation allowed? (forbidden list / not-in-allowed list) =====
        if not self.scope.operation_allowed(op_kind):
            self.audit.write("operation_forbidden", tool=tool_name, op=op_kind)
            return StepOutcome.error(
                f"Operation '{op_kind}' is not in scope.allowed_operations OR "
                f"is explicitly in forbidden_operations. Edit scope.yaml after "
                f"obtaining proper authorization."
            )

        # ===== 3. scope check (per-target) =====
        targets = self._extract_targets(tool_name, args, meta)
        if targets:
            for t in targets:
                if not self.scope.is_in_scope(t):
                    self.audit.write(
                        "scope_violation",
                        tool=tool_name, args=args, target=t,
                    )
                    return StepOutcome.error(
                        f"Scope violation: target '{t}' is NOT in authorized scope. "
                        f"This is a hard fail. Either fix the target, or update scope.yaml "
                        f"with proper authorization (require_approval applies)."
                    )

        # ===== 4. policy check =====
        ok, reason = self.policy.check(tool_name, args)
        if not ok:
            self.audit.write("policy_block", tool=tool_name, args=args, reason=reason)
            return StepOutcome.error(f"Policy block: {reason}")

        # ===== 5. approval gate =====
        scope_needs_approval = self.scope.requires_approval(op_kind)
        policy_needs_approval = self.policy.needs_approval(tool_name, args)
        if scope_needs_approval or policy_needs_approval:
            why = []
            if scope_needs_approval: why.append(f"scope.require_approval matched '{op_kind}'")
            if policy_needs_approval: why.append("policy heuristic matched a sensitive pattern")
            prompt = (
                f"⚠️  Operation `{op_kind}` requires manual approval.\n"
                f"Reason: {', '.join(why)}\n"
                f"Tool: {tool_name}\n"
                f"Args: {args}\n"
                f"Targets: {targets}\n"
                f"Approve? [y/N]"
            )
            approved = self.approval_callback(prompt)
            self.audit.write(
                "approval_gate",
                tool=tool_name, op=op_kind, approved=approved,
            )
            if not approved:
                return StepOutcome.error(f"User denied approval for {op_kind}")

        # ===== 5. audit before =====
        self.audit.write("tool_before", tool=tool_name, args=args, turn=self.current_turn)

        # ===== execute =====
        try:
            result = self.registry.call(tool_name, args, ctx=ctx)
            elapsed = time.time() - t0
            self.audit.write(
                "tool_after",
                tool=tool_name,
                ok=True,
                elapsed_ms=int(elapsed * 1000),
                # only summary; full data is in result if needed
                summary=str(result.data)[:500] if result.data is not None else None,
            )
            return result
        except Exception as e:
            tb = traceback.format_exc()
            self.audit.write("tool_error", tool=tool_name, args=args, error=str(e), tb=tb)
            return StepOutcome.error(f"Tool {tool_name} raised: {e}")

    def _extract_targets(self, tool_name: str, args: dict, meta: dict) -> list[str]:
        """Pull target host/url from args based on tool metadata."""
        target_keys = meta.get("target_keys", [])
        targets: list[str] = []
        for k in target_keys:
            v = args.get(k)
            if isinstance(v, str):
                targets.append(v)
            elif isinstance(v, list):
                targets.extend(x for x in v if isinstance(x, str))
        return targets
