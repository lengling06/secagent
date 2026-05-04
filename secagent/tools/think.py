"""think tool — 强制结构化思考.

灵感: Anthropic 的 think tool 模式 (https://www.anthropic.com/engineering/claude-think-tool)
+ ReAct (Reason+Act) 论文里的 thought 字段.

这个工具**不做任何实质操作**, 只接受三个字段:
  - observation: 我刚看到/学到什么
  - plan:        我接下来打算怎么做
  - next_action: 下一个具体动作 (单独一句, 可执行)

存在意义:
- 给模型一个"被框架认可的思考通道", 比纯文本 narration 更结构化
- 落到 audit log 里, 后续可复盘 agent 怎么推理的
- 撞墙时是个停车点, 强迫 agent 显式 reflect 而不是瞎重试
"""
from __future__ import annotations

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


def _do_think(args: dict, ctx: dict) -> StepOutcome:
    obs = (args.get("observation") or "").strip()
    plan = (args.get("plan") or "").strip()
    nxt = (args.get("next_action") or "").strip()
    if not (obs and plan and nxt):
        return StepOutcome.error(
            "think: observation / plan / next_action 三个字段都必须填. "
            "如果你只想说一句话, 用 narration (assistant content) 即可, 不要调 think."
        )
    return StepOutcome.cont(
        data={
            "observation": obs,
            "plan": plan,
            "next_action": nxt,
            "note": "thought logged. now execute next_action.",
        },
        prompt=(
            f"你刚刚记录的 next_action: {nxt}\n"
            "现在去执行它. 不要重复调 think, 直接动手."
        ),
    )


def register(reg: ToolRegistry) -> None:
    reg.register(
        name="think",
        description=(
            "记录一次结构化思考。当你撞墙、需要换方向、或者在做关键决策前, 调这个工具显式"
            "推理一遍。不做任何外部操作。"
            "格式: observation(看到啥) + plan(打算怎么办) + next_action(下一步具体做什么)。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "observation": {
                    "type": "string",
                    "description": "你刚看到或学到什么 (一句话)",
                },
                "plan": {
                    "type": "string",
                    "description": "你接下来打算怎么做 (一句话)",
                },
                "next_action": {
                    "type": "string",
                    "description": "下一个具体动作, 应当是可立即执行的 (一句话)",
                },
            },
            "required": ["observation", "plan", "next_action"],
        },
        fn=_do_think,
        operation="think",
        side_effects="read",
        category="memory",
    )
