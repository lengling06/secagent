"""Filesystem tools: read / write / patch.

All write operations are confined to engagement_dir.
"""
from __future__ import annotations

from pathlib import Path

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


_MAX_READ = 12_000  # chars returned to the model
_DEFAULT_LINE_SPAN = 120


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
    if not _within(eng_dir, target):
        return StepOutcome.error(f"file_read: '{target}' outside engagement dir")
    if not target.exists():
        return StepOutcome.error(f"file_read: not found: {target}")
    try:
        data = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return StepOutcome.error(f"file_read: {e}")

    start_line = int(args.get("start_line") or 1)
    if start_line < 1:
        start_line = 1
    end_line = args.get("end_line")

    lines = data.splitlines()
    total_lines = len(lines)
    if end_line is None:
        end_line = min(total_lines, start_line + _DEFAULT_LINE_SPAN - 1)
    else:
        end_line = int(end_line)
    if end_line < start_line:
        return StepOutcome.error("file_read: end_line must be >= start_line")

    chunk = "\n".join(lines[start_line - 1:end_line])
    truncated = False
    if len(chunk) > _MAX_READ:
        chunk = chunk[:_MAX_READ] + f"\n... [truncated, {len(chunk)-_MAX_READ} more chars]"
        truncated = True

    return StepOutcome.cont(
        data={
            "path": str(target.relative_to(eng_dir) if target.is_relative_to(eng_dir) else target),
            "start_line": start_line,
            "end_line": min(end_line, total_lines),
            "total_lines": total_lines,
            "truncated": truncated or end_line < total_lines,
            "content": chunk,
        },
        prompt=(
            f"read {target.name}:{start_line}-{min(end_line, total_lines)}; "
            "if needed, request a narrower range instead of rereading the whole file."
        ),
    )


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
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "description": "1-based start line; default 1"},
                "end_line": {"type": "integer", "description": "1-based end line; default start_line + 119"},
            },
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
