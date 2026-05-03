"""Filesystem tools: read / write / patch.

All write operations are confined to engagement_dir.
"""
from __future__ import annotations

from pathlib import Path

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


_MAX_READ = 40_000  # bytes


def _resolve(eng_dir: Path, p: str) -> Path:
    target = (eng_dir / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
    return target


def _within(eng_dir: Path, target: Path) -> bool:
    try:
        return str(target).startswith(str(eng_dir.resolve()))
    except Exception:
        return False


def _do_read(args: dict, ctx: dict) -> StepOutcome:
    eng_dir: Path = ctx["engagement_dir"]
    p = args.get("path", "")
    if not p:
        return StepOutcome.error("file_read: path required")
    target = _resolve(eng_dir, p)
    if not target.exists():
        return StepOutcome.error(f"file_read: not found: {target}")
    try:
        data = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return StepOutcome.error(f"file_read: {e}")
    if len(data) > _MAX_READ:
        data = data[:_MAX_READ] + f"\n... [truncated, {len(data)-_MAX_READ} more chars]"
    return StepOutcome.cont(data=data, prompt="continue")


def _do_write(args: dict, ctx: dict) -> StepOutcome:
    eng_dir: Path = ctx["engagement_dir"]
    p = args.get("path", "")
    content = args.get("content", "")
    if not p:
        return StepOutcome.error("file_write: path required")
    target = _resolve(eng_dir, p)
    if not _within(eng_dir, target):
        return StepOutcome.error(f"file_write: '{target}' outside engagement dir")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return StepOutcome.cont(data={"path": str(target), "bytes": len(content)}, prompt="continue")


def _do_patch(args: dict, ctx: dict) -> StepOutcome:
    eng_dir: Path = ctx["engagement_dir"]
    p = args.get("path", "")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    if not p or not old:
        return StepOutcome.error("file_patch: path and old_string required")
    target = _resolve(eng_dir, p)
    if not _within(eng_dir, target):
        return StepOutcome.error(f"file_patch: '{target}' outside engagement dir")
    if not target.exists():
        return StepOutcome.error(f"file_patch: not found: {target}")
    body = target.read_text(encoding="utf-8")
    cnt = body.count(old)
    if cnt == 0:
        return StepOutcome.error("file_patch: old_string not found")
    if cnt > 1:
        return StepOutcome.error(f"file_patch: old_string appears {cnt} times; not unique")
    body = body.replace(old, new, 1)
    target.write_text(body, encoding="utf-8")
    return StepOutcome.cont(data={"path": str(target), "replaced": 1}, prompt="continue")


def register(reg: ToolRegistry) -> None:
    reg.register(
        name="file_read",
        description="Read a file. Path is relative to engagement dir (or absolute).",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        fn=_do_read,
        operation="file_read",
        side_effects="read",
        category="filesystem",
    )
    reg.register(
        name="file_write",
        description="Create or overwrite a file. Restricted to engagement directory.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        fn=_do_write,
        operation="file_write",
        side_effects="write",
        category="filesystem",
    )
    reg.register(
        name="file_patch",
        description=(
            "Replace exactly one occurrence of old_string with new_string. "
            "Prefer this over file_write to save tokens and avoid clobbering."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        fn=_do_patch,
        operation="file_write",
        side_effects="write",
        category="filesystem",
    )
