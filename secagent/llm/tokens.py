"""Token counting — backend-agnostic estimate.

中转站的 ``response.usage`` 字段 **不可信**: 有的按字符 ÷ 3 折算, 有的虚报,
有的根本不返回。所以我们自己在客户端估。

精度顺序: tiktoken (cl100k_base) > 字符 // 4 fallback。
对于 Claude / GPT / DeepSeek / Kimi, cl100k_base 估的偏差在 ±10% 以内,
做"该不该压缩"决策完全够用。
"""
from __future__ import annotations

import json
from typing import Any

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        if not text:
            return 0
        try:
            return len(_enc.encode(text, disallowed_special=()))
        except Exception:
            # tiktoken occasionally chokes on weird unicode
            return max(1, len(text) // 4)
except ImportError:
    _enc = None

    def count_tokens(text: str) -> int:
        return max(0, len(text or "") // 4)


def count_history(history: list[dict]) -> int:
    """Count tokens across an OpenAI-shape history list.

    Handles:
      - str content
      - list-of-blocks content (Anthropic-shape mixed in)
      - tool_calls (assistant)
      - tool role messages
    """
    total = 0
    for m in history or []:
        c = m.get("content")
        if isinstance(c, str):
            total += count_tokens(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    text = b.get("text") or b.get("content") or ""
                    if isinstance(text, list):
                        # nested (anthropic tool_result with rich content)
                        for item in text:
                            if isinstance(item, dict):
                                total += count_tokens(str(item.get("text") or item.get("content") or ""))
                            else:
                                total += count_tokens(str(item))
                    else:
                        total += count_tokens(str(text))
                else:
                    total += count_tokens(str(b))
        for tc in m.get("tool_calls") or []:
            try:
                total += count_tokens(json.dumps(tc, ensure_ascii=False))
            except Exception:
                total += count_tokens(str(tc))
        # tool role messages have a tool_call_id field; cheap
        if m.get("tool_call_id"):
            total += 16
    return total


def has_real_tiktoken() -> bool:
    return _enc is not None
