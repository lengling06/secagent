"""skill library — 可按需查阅的经验/招数库.

灵感: GenericAgent 的 L3 Task Skills + Cline 的 cline_rules + Cursor 的 .cursorrules.

存储位置 (按加载优先级):
  1. ~/.secagent/skills/*.md           — 用户自己积累的 (最高优先级)
  2. <pkg>/prompts/skills/*.md          — 内置起手包 (随发版更新)

每个 skill 是一个 markdown 文件:
  - 文件名 (无扩展) = skill 名
  - 第一行最好是 "# <Title>", 一句话讲它管什么
  - 后面正文是详细做法, 含坐标和反例

使用方式:
- 在 soul.md 里告诉 agent: "撞到陌生站点先 skill_list 看看有没有现成招数"
- agent 调 skill_list() 得到所有 skill 名 + 摘要
- agent 调 skill_read(name) 拿到全文

设计取舍:
- 不做自动匹配 (host-based 太弱, 不知道前置探测后再判断)
- 让 agent 自己决定何时查 skill, 类似真人查文档
- 写新 skill 用 `file_write ~/.secagent/skills/<name>.md`, 没有专用工具
  (避免给 agent 太多工具, 它能用 file_write 就行)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


def _skill_dirs() -> list[Path]:
    """Return all skill source dirs in load priority order (user first)."""
    user = Path.home() / ".secagent" / "skills"
    builtin = Path(__file__).resolve().parent.parent / "prompts" / "skills"
    return [d for d in (user, builtin) if d.exists()]


def _all_skills() -> dict[str, Path]:
    """Map skill_name -> Path. User skills shadow builtin with same name."""
    found: dict[str, Path] = {}
    # iterate in REVERSE priority order so user dir overwrites builtin
    for d in reversed(_skill_dirs()):
        for p in sorted(d.glob("*.md")):
            if p.stem.lower() == "readme":
                continue
            found[p.stem] = p
    return found


def _summary_of(p: Path) -> str:
    """First non-empty title or line as the skill's short summary."""
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                return line.lstrip("# ").strip()
            return line[:120]
    except Exception:
        pass
    return "(no summary)"


def list_skills_summary() -> str:
    """Render a one-line-per-skill summary, suitable for system prompt injection."""
    sk = _all_skills()
    if not sk:
        return "(no skills available; drop *.md files in ~/.secagent/skills/)"
    lines = []
    for name, p in sorted(sk.items()):
        origin = "user" if str(p).startswith(str(Path.home())) else "builtin"
        lines.append(f"- **{name}** _(_{origin}_)_ — {_summary_of(p)}")
    return "\n".join(lines)


def _do_skill_list(args: dict, ctx: dict) -> StepOutcome:
    sk = _all_skills()
    items = [
        {
            "name": name,
            "origin": "user" if str(p).startswith(str(Path.home())) else "builtin",
            "summary": _summary_of(p),
            "path": str(p),
        }
        for name, p in sorted(sk.items())
    ]
    return StepOutcome.cont(
        data={"count": len(items), "skills": items},
        prompt=(
            "if any skill looks relevant to current target, call "
            "skill_read(name) to get its full content."
        ),
    )


def _do_skill_read(args: dict, ctx: dict) -> StepOutcome:
    name = (args.get("name") or "").strip()
    if not name:
        return StepOutcome.error("skill_read: name is required")
    sk = _all_skills()
    p = sk.get(name)
    if p is None:
        avail = ", ".join(sorted(sk.keys())) or "(none)"
        return StepOutcome.error(
            f"skill_read: '{name}' not found. available: {avail}"
        )
    try:
        body = p.read_text(encoding="utf-8")
    except Exception as e:
        return StepOutcome.error(f"skill_read: {e}")
    return StepOutcome.cont(
        data={"name": name, "path": str(p), "content": body},
        prompt="apply this skill to the current task; cite it when relevant.",
    )


def register(reg: ToolRegistry) -> None:
    reg.register(
        name="skill_list",
        description=(
            "列出所有可用 skill (经验/招数库)。撞到陌生类型站点、不知道从哪入手时先调一次。"
            "返回 skill 名 + 一行摘要; 看到相关的再 skill_read 拿全文。"
        ),
        parameters={"type": "object", "properties": {}},
        fn=_do_skill_list,
        operation="skill_read",
        side_effects="read",
        category="memory",
    )
    reg.register(
        name="skill_read",
        description="读取某个 skill 的完整内容 (具体怎么做 / 反例 / 坐标)。",
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "skill 名 (不含 .md), 通过 skill_list 得到",
                },
            },
            "required": ["name"],
        },
        fn=_do_skill_read,
        operation="skill_read",
        side_effects="read",
        category="memory",
    )
