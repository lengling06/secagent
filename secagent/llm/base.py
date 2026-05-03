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
