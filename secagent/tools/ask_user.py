"""ask_user: human-in-the-loop. Used both by LLM directly and by the
approval gate inside Handler.
"""
from __future__ import annotations

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


def _do_ask_user(args: dict, ctx: dict) -> StepOutcome:
    question = args.get("question", "").strip()
    candidates = args.get("candidates") or []
    if not question:
        return StepOutcome.error("ask_user: question required")

    cb = ctx.get("approval_callback")
    if not cb:
        return StepOutcome.error("ask_user: no UI callback wired")

    # The REPL's approval_callback returns bool by default; for free-form Q&A
    # we use a different convention: store a generic prompt callable.
    # Here we just reuse approval_callback for boolean Y/N; for richer input,
    # frontends should hook a `prompt_callback(question, candidates) -> str`.
    prompt_cb = ctx.get("prompt_callback")
    if prompt_cb:
        answer = prompt_cb(question, candidates)
        return StepOutcome.cont(data={"answer": answer}, prompt="continue")

    # fallback: y/n
    approved = cb(question)
    return StepOutcome.cont(data={"approved": approved}, prompt="continue")


def register(reg: ToolRegistry) -> None:
    reg.register(
        name="ask_user",
        description=(
            "Ask the human user a clarifying question. Use this for: ambiguous goals, "
            "missing data the user has, or before any sensitive/irreversible action."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "candidates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional choices to present to the user",
                },
            },
            "required": ["question"],
        },
        fn=_do_ask_user,
        operation="ask_user",
        side_effects="read",
        category="hitl",
    )
