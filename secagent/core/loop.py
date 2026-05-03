"""Agent main loop. ~150 lines.

Design (heavily inspired by GenericAgent/agent_loop.py):
- LLM session keeps full history internally; we only pass *new* messages each turn.
- Loop yields strings for streaming output; final return value is the exit reason.
- Periodic tool-schema reset (every N turns) to prevent context bloat.
"""
from __future__ import annotations

import json
from typing import Any, Generator

from secagent.core.outcome import StepOutcome


def _pretty(data: Any) -> str:
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False, indent=2)
    return str(data)


def run_loop(
    llm,                     # LLMSession
    handler,                 # Handler
    system_prompt: str,
    user_input: str,
    tools_schema: list,
    max_turns: int = 40,
    schema_reset_every: int = 10,
) -> Generator[str, None, dict]:
    """Run the agent loop.

    Yields chunks of user-visible text. Returns the final exit reason dict.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input},
    ]
    handler.max_turns = max_turns
    exit_reason: dict = {}

    for turn in range(1, max_turns + 1):
        yield f"\n\n**Turn {turn}**\n\n"

        # periodic reset to fight context bloat
        if turn % schema_reset_every == 0:
            llm.reset_tool_schema_cache()

        # === LLM call ===
        response = llm.chat(messages=messages, tools=tools_schema)
        if response.content:
            yield response.content + "\n"

        if not response.tool_calls:
            # plain text reply == task done
            exit_reason = {"result": "TASK_DONE", "data": response.content}
            break

        # === dispatch tool calls ===
        tool_results: list[dict] = []
        next_prompts: set[str] = set()
        should_exit = False

        for idx, tc in enumerate(response.tool_calls):
            name, args, tid = tc.name, tc.args, tc.id
            yield f"\n🛠️  `{name}` args={_pretty(args)[:200]}\n"

            handler.current_turn = turn
            outcome: StepOutcome = handler.dispatch(name, args)

            # show output to user
            if outcome.data is not None:
                yield f"```\n{_pretty(outcome.data)[:1500]}\n```\n"

            # accumulate
            if outcome.should_exit:
                should_exit = True
                exit_reason = {"result": "EXITED", "data": outcome.data}
                break
            if outcome.next_prompt:
                next_prompts.add(outcome.next_prompt)
            if outcome.data is not None:
                tool_results.append({
                    "tool_use_id": tid,
                    "content": _pretty(outcome.data)[:8000],  # cap per-result
                })

        if should_exit:
            break
        if not next_prompts:
            exit_reason = {"result": "TASK_DONE"}
            break

        # next turn: only pass new user message + tool results
        # full history is kept inside the LLM session
        messages = [{
            "role": "user",
            "content": "\n".join(next_prompts),
            "tool_results": tool_results,
        }]

    if not exit_reason:
        exit_reason = {"result": "MAX_TURNS_EXCEEDED"}

    yield f"\n\n[Exit: {exit_reason.get('result')}]\n"
    return exit_reason
