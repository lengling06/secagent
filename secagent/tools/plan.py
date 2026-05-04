"""plan tool — 任务分解 + 步骤跟踪.

灵感: GenericAgent 的 plan-driven loop + Claude Code 的 TodoWrite.

工作流:
  1. 用户给一个大任务 (例: "逆向 talkai.info 的签名算法")
  2. agent 第一反应调 plan(goal=..., steps=[...]) 把任务拆开
  3. 后续每完成一步调 step_done(idx, summary=...)
  4. plan 状态落到 engagement/state/plan.md, 跨 session 存活

设计取舍:
- plan 是单一活跃文档 (不是栈/队列), 一个 engagement 同一时间一个 plan
- 重新调 plan() 会覆盖原 plan (agent 决定换路线时主动重规划)
- step_done 不强校验顺序 (agent 可以并行/跳步)
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


def _plan_path(eng_dir: Path) -> Path:
    return eng_dir / "state" / "plan.md"


def _render_plan(goal: str, steps: list[dict]) -> str:
    lines = [
        "# Plan",
        "",
        f"**Goal**: {goal}",
        "",
        f"_(updated {datetime.utcnow().isoformat(timespec='seconds')}Z)_",
        "",
        "## Steps",
        "",
    ]
    for i, s in enumerate(steps, 1):
        mark = "x" if s.get("done") else " "
        line = f"- [{mark}] **{i}.** {s['text']}"
        if s.get("done") and s.get("summary"):
            line += f"\n      > {s['summary']}"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)


def _parse_plan(text: str) -> tuple[str, list[dict]]:
    """Best-effort parser to round-trip plan.md back to (goal, steps)."""
    goal = ""
    steps: list[dict] = []
    in_steps = False
    current_summary_for: int = -1
    for line in text.splitlines():
        line_s = line.strip()
        if line_s.startswith("**Goal**:"):
            goal = line_s.removeprefix("**Goal**:").strip()
        elif line_s == "## Steps":
            in_steps = True
        elif in_steps and line_s.startswith("- ["):
            done = line_s.startswith("- [x]")
            # remove "- [x] **N.** " or "- [ ] **N.** "
            try:
                txt = line_s.split("**", 2)[2].lstrip(" .")
                # split on first ". " from "1. text"
                if ". " in txt:
                    txt = txt.split(". ", 1)[1] if txt[0].isdigit() else txt
            except Exception:
                txt = line_s
            steps.append({"text": txt, "done": done, "summary": ""})
            current_summary_for = len(steps) - 1
        elif in_steps and line_s.startswith(">") and current_summary_for >= 0:
            steps[current_summary_for]["summary"] = line_s.lstrip("> ").strip()
    return goal, steps


def _do_plan(args: dict, ctx: dict) -> StepOutcome:
    eng_dir: Path = ctx["engagement_dir"]
    goal = (args.get("goal") or "").strip()
    raw_steps = args.get("steps") or []
    if not goal:
        return StepOutcome.error("plan: goal is required")
    if not isinstance(raw_steps, list) or not raw_steps:
        return StepOutcome.error("plan: steps must be a non-empty list of strings")
    if len(raw_steps) > 12:
        return StepOutcome.error(
            f"plan: {len(raw_steps)} steps is too many. Aim for 3-7 high-level steps; "
            "expand sub-steps lazily as you progress."
        )

    steps: list[dict] = []
    for s in raw_steps:
        if isinstance(s, str):
            steps.append({"text": s.strip(), "done": False, "summary": ""})
        elif isinstance(s, dict) and "text" in s:
            steps.append({"text": str(s["text"]).strip(), "done": False, "summary": ""})
        else:
            return StepOutcome.error(f"plan: invalid step entry: {s!r}")

    p = _plan_path(eng_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_render_plan(goal, steps), encoding="utf-8")

    return StepOutcome.cont(
        data={
            "path": str(p.relative_to(eng_dir)),
            "goal": goal,
            "n_steps": len(steps),
            "steps": [s["text"] for s in steps],
        },
        prompt=(
            f"plan saved with {len(steps)} steps. now execute step 1, then call "
            f"step_done(idx=1, summary='...') when it's done."
        ),
    )


def _do_step_done(args: dict, ctx: dict) -> StepOutcome:
    eng_dir: Path = ctx["engagement_dir"]
    p = _plan_path(eng_dir)
    if not p.exists():
        return StepOutcome.error(
            "step_done: no plan exists yet. call plan(goal, steps) first."
        )
    idx = args.get("idx")
    summary = (args.get("summary") or "").strip()
    if idx is None:
        return StepOutcome.error("step_done: idx (1-based) required")
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return StepOutcome.error("step_done: idx must be an integer")

    text = p.read_text(encoding="utf-8")
    goal, steps = _parse_plan(text)
    if not steps:
        return StepOutcome.error("step_done: could not parse plan.md (corrupted?)")
    if not (1 <= idx <= len(steps)):
        return StepOutcome.error(f"step_done: idx={idx} out of range 1..{len(steps)}")

    steps[idx - 1]["done"] = True
    if summary:
        steps[idx - 1]["summary"] = summary

    p.write_text(_render_plan(goal, steps), encoding="utf-8")

    n_done = sum(1 for s in steps if s["done"])
    next_step = next((i + 1 for i, s in enumerate(steps) if not s["done"]), None)
    if next_step is None:
        prompt = "all plan steps completed. consider task_complete(summary)."
    else:
        prompt = f"step {idx} marked done ({n_done}/{len(steps)}). next: step {next_step} — {steps[next_step-1]['text']}"

    return StepOutcome.cont(
        data={
            "idx_done": idx,
            "n_done": n_done,
            "n_total": len(steps),
            "next_step_idx": next_step,
            "next_step_text": steps[next_step - 1]["text"] if next_step else None,
        },
        prompt=prompt,
    )


def _do_plan_show(args: dict, ctx: dict) -> StepOutcome:
    eng_dir: Path = ctx["engagement_dir"]
    p = _plan_path(eng_dir)
    if not p.exists():
        return StepOutcome.cont(
            data={"path": str(p.relative_to(eng_dir)), "exists": False, "content": ""},
            prompt="no plan yet. call plan(goal, steps) to create one.",
        )
    text = p.read_text(encoding="utf-8")
    return StepOutcome.cont(
        data={"path": str(p.relative_to(eng_dir)), "exists": True, "content": text},
        prompt="continue with the next undone step.",
    )


def register(reg: ToolRegistry) -> None:
    reg.register(
        name="plan",
        description=(
            "把当前任务拆成 3-7 个高层步骤, 落盘到 engagement/state/plan.md。"
            "复杂任务 (逆向某个签名 / 排查多接口) 必须先调 plan 再动手。"
            "重新调 plan() 会覆盖旧 plan (主动换路线时用)。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "本次任务的目标 (一句话)",
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "3-7 条高层步骤. 不要把每个工具调用都写成步骤, 那是 step 内部的事",
                },
            },
            "required": ["goal", "steps"],
        },
        fn=_do_plan,
        operation="plan",
        side_effects="write",
        category="memory",
    )
    reg.register(
        name="step_done",
        description=(
            "标记 plan 里某个步骤完成。idx 是 1-based。summary 写一句话讲这步学到了什么"
            "(file:line / 函数名 / 算法 等坐标级事实)。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "idx": {"type": "integer", "description": "1-based step index"},
                "summary": {
                    "type": "string",
                    "description": "这步的成果. 含坐标的事实最有价值.",
                },
            },
            "required": ["idx", "summary"],
        },
        fn=_do_step_done,
        operation="plan",
        side_effects="write",
        category="memory",
    )
    reg.register(
        name="plan_show",
        description="读取当前 plan 状态。开始一个会话或从压缩里恢复时调一次, 看上次卡在哪。",
        parameters={"type": "object", "properties": {}},
        fn=_do_plan_show,
        operation="plan_read",
        side_effects="read",
        category="memory",
    )
