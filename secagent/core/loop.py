"""Agent main loop. ~200 lines.

Design (heavily inspired by GenericAgent/agent_loop.py):
- LLM session keeps full history internally; we only pass *new* messages each turn.
- Loop yields strings for streaming output; final return value is the exit reason.
- Periodic tool-schema reset (every N turns) to prevent context bloat.

Context management (B7 + B11):
- Big tool outputs (>OFFLOAD_THRESHOLD chars) are persisted to
  ``<engagement>/.cache/<tool>_<ts>_<hash>.txt``; the LLM sees only a
  head/tail summary + path.
- Each turn checks ``llm.approx_tokens() / llm.context_window``:
    * 70-78%  → 软警告, 提示 LLM 调 update_working_checkpoint
    * ≥ 78%   → 触发 llm.compact_if_needed (rolling summary)
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Generator, Optional

from secagent.core.outcome import StepOutcome


# B7 thresholds
OFFLOAD_THRESHOLD = 4000          # chars, single tool result
HEAD_LINES = 20
TAIL_LINES = 10

# B11 thresholds (override-able later via model registry)
WARN_RATIO    = 0.70
COMPACT_RATIO = 0.78


def _pretty(data: Any) -> str:
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False, indent=2)
    return str(data)


def _maybe_offload(
    name: str,
    content_str: str,
    engagement_dir: Optional[Path],
) -> str:
    """If content is bigger than threshold and we have a place to put it, write
    to disk and return a head/tail summary blob; otherwise return content as-is.
    """
    if engagement_dir is None or len(content_str) <= OFFLOAD_THRESHOLD:
        return content_str

    cache_dir = engagement_dir / ".cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return content_str  # if we can't write, fall back to raw (will get truncated below)

    h = hashlib.sha256(content_str.encode("utf-8", errors="replace")).hexdigest()[:12]
    ts = int(time.time())
    cache_file = cache_dir / f"{name}_{ts}_{h}.txt"
    try:
        cache_file.write_text(content_str, encoding="utf-8")
    except Exception:
        return content_str

    lines = content_str.splitlines()
    head = "\n".join(lines[:HEAD_LINES])
    tail = "\n".join(lines[-TAIL_LINES:]) if len(lines) > HEAD_LINES else ""
    rel = cache_file.relative_to(engagement_dir) if cache_file.is_relative_to(engagement_dir) else cache_file

    parts = [
        f"[output offloaded — {len(content_str)} chars / {len(lines)} lines saved to {rel}]",
        f"=== head (first {min(HEAD_LINES, len(lines))} lines) ===",
        head,
    ]
    if tail:
        parts += [
            f"=== tail (last {min(TAIL_LINES, len(lines))} lines) ===",
            tail,
        ]
    parts.append(
        "use file_read with start/end line numbers to inspect specific ranges; "
        "do NOT ask for the full file unless absolutely necessary."
    )
    return "\n".join(parts)


def run_loop(
    llm,                     # LLMSession
    handler,                 # Handler
    system_prompt: str,
    user_input: str,
    tools_schema: list,
    max_turns: int = 40,
    schema_reset_every: int = 10,
    engagement_dir: Optional[Path] = None,   # B7: offload destination
) -> Generator[str, None, dict]:
    """Run the agent loop.

    Yields chunks of user-visible text. Returns the final exit reason dict.
    """
    # If the caller didn't pass engagement_dir, try to read it from handler
    if engagement_dir is None:
        engagement_dir = getattr(handler, "engagement_dir", None)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input},
    ]
    handler.max_turns = max_turns
    exit_reason: dict = {}
    warned_compaction = False  # only warn once per session

    for turn in range(1, max_turns + 1):
        yield f"\n\n**Turn {turn}/{max_turns}**\n\n"

        # periodic tool-schema cache reset (fights schema-induced bloat)
        if turn % schema_reset_every == 0:
            llm.reset_tool_schema_cache()

        # === B11: context pressure check (before LLM call) ===
        try:
            cw = getattr(llm, "context_window", 200_000) or 200_000
            used = llm.approx_tokens()
            ratio = used / cw if cw else 0.0
        except Exception:
            ratio = 0.0
            used, cw = 0, 200_000

        if ratio >= COMPACT_RATIO:
            yield f"⚙️  上下文使用 {int(ratio*100)}% (~{used:,}/{cw:,}), 触发压缩...\n"
            try:
                did = llm.compact_if_needed(context_window=cw, ratio=COMPACT_RATIO)
                if did:
                    yield "   ✓ 已压缩, 关键事实保留在 checkpoint\n"
                else:
                    yield "   (无可压缩的边界, 跳过)\n"
            except Exception as e:
                yield f"   [warn] compaction failed: {e}\n"
        elif WARN_RATIO <= ratio < COMPACT_RATIO and not warned_compaction:
            warned_compaction = True
            warn_msg = (
                f"⚠️ 上下文已用 {int(ratio*100)}% (~{used:,}/{cw:,}), 即将达到压缩阈值。"
                f"如果有关键事实/坐标 (file:line, 函数名, 算法) 还没记下, "
                f"立刻调 update_working_checkpoint 把它们持久化, 否则可能在压缩中丢失。"
            )
            yield f"⚠️  {warn_msg}\n"
            # Inject it so the LLM sees it next call
            messages.append({"role": "user", "content": warn_msg})

        # === LLM call ===
        response = llm.chat(messages=messages, tools=tools_schema)
        if response.content:
            yield response.content + "\n"

        if not response.tool_calls:
            # plain text reply == task done (legacy exit; task_complete is preferred)
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

            # display: short preview to user (UX), full detail goes to LLM via tool_results
            if outcome.data is not None:
                yield f"```\n{_pretty(outcome.data)[:1500]}\n```\n"

            # accumulate
            if outcome.should_exit:
                should_exit = True
                exit_reason = {"result": "EXITED", "data": outcome.data}
                break
            if outcome.next_prompt:
                next_prompts.add(outcome.next_prompt)

            # B7: offload big outputs before returning to LLM
            if outcome.data is not None:
                content_str = _pretty(outcome.data)
                content_for_llm = _maybe_offload(name, content_str, engagement_dir)
                tool_results.append({
                    "tool_use_id": tid,
                    "content": content_for_llm,
                })

        if should_exit:
            break
        if not next_prompts:
            exit_reason = {"result": "TASK_DONE"}
            break
        if turn >= max_turns:
            exit_reason = {
                "result": "MAX_TURNS_EXCEEDED",
                "data": "Stopped after hitting the configured turn limit.",
            }
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
