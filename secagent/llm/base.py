"""LLM Session abstraction.

Each Session keeps the FULL conversation history internally so we can:
- pass only new messages each turn (saves tokens)
- swap providers (Claude ⇄ OAI ⇄ Mixin) without losing history
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMSession:
    """Abstract LLM session interface."""

    name: str = "abstract"
    model: str = ""
    history: list[dict] = []
    # Approximate context window in tokens. Subclasses override; the loop
    # uses this to compute warn_at_ratio / compact_at_ratio thresholds.
    # 现在硬编码默认值; 后续会被 model registry 替换。
    context_window: int = 200_000

    def chat(self, messages: list[dict], tools: Optional[list] = None) -> LLMResponse:
        """Send `messages` (which extends history), return parsed response.

        Implementations must:
        - APPEND `messages` to `self.history`
        - APPEND assistant response to `self.history`
        - Return parsed LLMResponse with tool_calls properly extracted
        """
        raise NotImplementedError

    def reset_tool_schema_cache(self) -> None:
        """Force re-send tool schema next turn (every N turns to fight bloat)."""
        pass

    # -------- context management (B10) --------
    # Subclasses override these. Default no-op so loop.py can always call them.

    def approx_tokens(self) -> int:
        """Estimate current in-context token usage."""
        return 0

    def compact_if_needed(self, context_window: int = 200_000, ratio: float = 0.78,
                          keep_recent: int = 6, summarizer_model: Optional[str] = None) -> bool:
        """Roll up old history into a summary if usage > context_window * ratio.
        Returns True if compaction happened, False otherwise."""
        return False
