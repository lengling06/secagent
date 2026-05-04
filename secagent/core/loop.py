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
from collections import deque
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

# Narration gate (P0-B): tool_call with empty content is rejected.
# After MAX_NARR_RETRIES the LLM is allowed through with a warning so we don't
# infinite-loop against a stubborn model / proxy.
SOUL_TOKEN = "小霜大人"
MAX_NARR_RETRIES = 2

# P0-D: prepended to every next-turn user message so the rule survives long
# context (system prompt gets diluted; user-msg prefix is rock-solid on
# OpenAI-compat gateways).
SOUL_REMINDER = (
    f"[规则提醒] 工具调用前必须先用一句中文以 '{SOUL_TOKEN}，' 起头, "
    f"写'<现状>; <目的>; <方法>'三段式 narration。沉默调工具会被框架拒绝。\n\n"
)


def _pretty(data: Any) -> str:
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False, indent=2)
    return str(data)


def _user_preview(data: Any) -> str:
    if isinstance(data, dict) and "content" in data and isinstance(data["content"], str):
        meta = {k: v for k, v in data.items() if k != "content"}
        preview = data["content"]
        if len(preview) > 500:
            preview = preview[:500] + f"\n... [truncated, {len(data['content'])-500} more chars]"
        meta_text = json.dumps(meta, ensure_ascii=False, indent=2)
        return f"{meta_text}\n--- content preview ---\n{preview}"
    rendered = _pretty(data)
    if len(rendered) > 1000:
        rendered = rendered[:1000] + f"\n... [truncated, {len(rendered)-1000} more chars]"
    return rendered


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
    recent_calls: deque[str] = deque(maxlen=8)
    narration_retries = 0  # P0-B: reject silent tool_calls up to MAX_NARR_RETRIES

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

        # === P0-B: narration gate ===
        # 真 agent 必须先思考再行动. 拒绝沉默的 tool_calls (LLM 输出 content="" 但有
        # tool_calls 的情况). 最多重试 MAX_NARR_RETRIES 次, 之后放行避免死循环.
        content_str = (response.content or "").strip()
        has_soul = SOUL_TOKEN in content_str
        if response.tool_calls and (not content_str or not has_soul):
            if narration_retries < MAX_NARR_RETRIES:
                narration_retries += 1
                if not content_str:
                    yield f"\n🚫 [narration gate] 模型沉默调工具, 拒绝执行 (重试 {narration_retries}/{MAX_NARR_RETRIES})\n"
                else:
                    yield f"\n🚫 [narration gate] 没叫'{SOUL_TOKEN}', 拒绝执行 (重试 {narration_retries}/{MAX_NARR_RETRIES})\n"
                # pop the bogus assistant turn from llm.history so we can retry cleanly.
                # otherwise the orphan tool_calls in history will break OpenAI strict gateways.
                hist = getattr(llm, "history", None)
                if hist and hist[-1].get("role") == "assistant":
                    hist.pop()
                messages = [{
                    "role": "user",
                    "content": (
                        f"[拒绝执行] 你直接调了工具但没说话(或没叫'{SOUL_TOKEN}'). 这违反规则。\n"
                        f"重新来: 先用一句中文以 '{SOUL_TOKEN}，' 起头, "
                        f"写'<现状>; <目的>; <方法>'三段式, 然后再调工具。\n"
                        f"如果当前任务已完成, 直接调 task_complete(summary)。"
                    )
                }]
                continue
            else:
                # gave up; let it through but mark it
                yield f"\n⚠️ [narration gate] 重试 {MAX_NARR_RETRIES} 次仍无 narration, 放行避免死循环。换个模型试试。\n"

        if content_str:
            yield content_str + "\n"
            narration_retries = 0  # reset on successful narration

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

            try:
                fingerprint = f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"
            except TypeError:
                fingerprint = f"{name}:{repr(args)}"

            if recent_calls and fingerprint == recent_calls[-1]:
                outcome = StepOutcome.error(
                    f"Repeated identical tool call blocked: {name}. "
                    "Choose a narrower range, a different tool, or summarize and stop."
                )
            else:
                recent_calls.append(fingerprint)
                handler.current_turn = turn
                outcome = handler.dispatch(name, args)

            # display: short preview to user (UX), full detail goes to LLM via tool_results
            if outcome.data is not None:
                yield f"```\n{_user_preview(outcome.data)}\n```\n"

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
        # P0-D: prepend SOUL_REMINDER so rule survives long context
        messages = [{
            "role": "user",
            "content": SOUL_REMINDER + "\n".join(next_prompts),
            "tool_results": tool_results,
        }]

    if not exit_reason:
        exit_reason = {"result": "MAX_TURNS_EXCEEDED"}

    yield f"\n\n[Exit: {exit_reason.get('result')}]\n"
    return exit_reason
