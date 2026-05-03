"""Anthropic Claude session."""
from __future__ import annotations

import os
from typing import Optional

from secagent.llm.base import LLMResponse, LLMSession, ToolCall


class AnthropicSession(LLMSession):
    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-sonnet-4-5-20250929",
        api_key: Optional[str] = None,
        max_tokens: int = 8192,
    ):
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise ImportError("pip install anthropic") from e

        self.model = model
        self.max_tokens = max_tokens
        self._client = Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
        self.history: list[dict] = []
        self._system: str = ""
        self._cached_tools_hash: Optional[int] = None

    def reset_tool_schema_cache(self) -> None:
        self._cached_tools_hash = None

    def chat(self, messages: list[dict], tools=None) -> LLMResponse:
        # Extract system prompt (only on first call)
        for m in messages:
            if m.get("role") == "system" and not self._system:
                self._system = m["content"]

        # Append non-system messages to history
        for m in messages:
            if m.get("role") == "system":
                continue
            tool_results = m.get("tool_results") or []
            if tool_results:
                # tool_results travel as user message blocks per Anthropic API
                blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": tr["tool_use_id"],
                        "content": tr["content"],
                    }
                    for tr in tool_results
                ]
                if m.get("content"):
                    blocks.insert(0, {"type": "text", "text": m["content"]})
                self.history.append({"role": "user", "content": blocks})
            else:
                self.history.append({"role": m["role"], "content": m["content"]})

        # Convert tools schema to Anthropic format
        anthropic_tools = self._convert_tools(tools) if tools else None

        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self._system,
            messages=self.history,
            tools=anthropic_tools,
        )

        # Append assistant response to history (raw blocks for tool_use round-trip)
        self.history.append({"role": "assistant", "content": resp.content})

        # Parse blocks
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, args=dict(block.input)))

        return LLMResponse(content="".join(text_parts), tool_calls=tool_calls)

    def _convert_tools(self, tools_schema: list) -> list:
        """OpenAI-style schema → Anthropic tools format."""
        anthropic = []
        for t in tools_schema:
            anthropic.append({
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t["parameters"],
            })
        return anthropic
