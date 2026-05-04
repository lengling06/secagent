"""task_complete — let the agent declare end-of-task explicitly.

Without this, the loop only exits when the model emits a plain-text reply
without tool calls (current logic in core/loop.py). That's brittle: the model
sometimes emits an empty turn while still mid-thought, prematurely ending,
or keeps going past where it should. ``task_complete(summary)`` is the
clean signal: agent is done, loop terminates, REPL prompts user.
"""
from __future__ import annotations

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


def _do_task_complete(args, ctx):
    summary = (args.get("summary") or "").strip()
    if not summary:
        summary = "(agent declared task complete with no summary)"
    return StepOutcome.exit(reason=summary)


def register(reg: ToolRegistry) -> None:
    reg.register(
        name="task_complete",
        description=(
            "声明当前任务完成, 干净退出 loop。"
            "调用时机: 用户问题已回答 / 阶段性逆向目标达成 (算法已还原 + 验证通过 + finding 已写) / "
            "需要等用户给新指令前。\n"
            "summary 用中文写, 概述这一轮做了什么、产物在哪 (findings/ 文件名 / cache 路径 / "
            "checkpoint 是否更新)。这是给 '小霜大人' 看的最终交付总结。\n"
            "不要在还需要继续工作时调用; 也不要为了'省 turn'提前退出。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "中文总结: 做了什么、产物路径、还有什么待办",
                },
            },
            "required": ["summary"],
        },
        fn=_do_task_complete,
        operation="task_complete",
        side_effects="read",
        category="control",
    )
