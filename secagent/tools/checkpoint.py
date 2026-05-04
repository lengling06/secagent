"""Working checkpoint — survive across context compaction and across sessions.

Inspired by GenericAgent's L3-style ``update_working_checkpoint`` tool.

Design:
- The agent calls ``update_working_checkpoint(notes)`` after each meaningful
  sub-task. ``notes`` is a Markdown blob in 4 fixed sections. We just write
  it (overwrite) to ``<engagement>/state/checkpoint.md``.
- On REPL start, ``state/checkpoint.md`` is auto-loaded into the system
  prompt under ``## Resume from last checkpoint``. So:
    - it survives history compaction (it's in system, not history)
    - it survives REPL restarts
- ``read_working_checkpoint`` is mostly defensive — the LLM rarely needs to
  re-read what's already in its system prompt, but exposing it keeps the
  contract clean (write/read symmetry, useful for debugging).
"""
from __future__ import annotations

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


_CHECKPOINT_REL = ("state", "checkpoint.md")


def _checkpoint_path(ctx):
    eng = ctx["engagement_dir"]
    p = eng
    for seg in _CHECKPOINT_REL:
        p = p / seg
    return p


def _do_update_checkpoint(args, ctx):
    notes = (args.get("notes") or "").strip()
    if not notes:
        return StepOutcome.error("update_working_checkpoint: notes is required (Markdown 4-section blob)")

    if len(notes) > 20_000:
        return StepOutcome.error(
            f"checkpoint too large: {len(notes)} chars. "
            "Compress to <20k. Drop raw tool outputs, keep only file:line / "
            "URL / function name / algorithm-name level facts."
        )

    cp = _checkpoint_path(ctx)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(notes, encoding="utf-8")

    return StepOutcome.cont(
        data={
            "saved": str(cp),
            "size": len(notes),
            "note": "checkpoint persisted; will auto-load on next REPL start "
                    "and survive context compaction.",
        },
        prompt="checkpoint saved. continue with the next probe.",
    )


def _do_read_checkpoint(args, ctx):
    cp = _checkpoint_path(ctx)
    if not cp.exists():
        return StepOutcome.cont(
            data={"checkpoint": None},
            prompt="no checkpoint yet — call update_working_checkpoint after the next milestone.",
        )
    return StepOutcome.cont(
        data={
            "path": str(cp),
            "checkpoint": cp.read_text(encoding="utf-8"),
        },
        prompt="checkpoint loaded.",
    )


def register(reg: ToolRegistry) -> None:
    reg.register(
        name="update_working_checkpoint",
        description=(
            "持久化当前任务进度, 让关键事实抗 history 压缩 + 跨会话保留。"
            "在以下时机调用: 确定入口文件 / 还原算法 / 沙箱验证通过 / 撞上需要换思路的失败。\n"
            "notes 必须是 Markdown 格式, 含 4 节 (用 ## 标题): \n"
            "  ## 当前任务  (一句话)\n"
            "  ## 已确认事实  (含坐标 file:line / URL / 函数名 / 算法 / key 派生方式)\n"
            "  ## 待办  (1-3 条明确动作)\n"
            "  ## 已尝试失败的路径  (避免重复)\n"
            "每次调用整个覆盖 (不是 append)。<20k 字符。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "notes": {
                    "type": "string",
                    "description": "完整的 4 节 Markdown 进度快照",
                },
            },
            "required": ["notes"],
        },
        fn=_do_update_checkpoint,
        operation="checkpoint_write",
        side_effects="write",
        category="memory",
    )

    reg.register(
        name="read_working_checkpoint",
        description=(
            "读取当前 engagement 的 checkpoint。"
            "通常你不需要调用 — checkpoint 已经在 system prompt 里。"
            "仅在你不确定 system prompt 里的 checkpoint 是否最新时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
        fn=_do_read_checkpoint,
        operation="checkpoint_read",
        side_effects="read",
        category="memory",
    )
