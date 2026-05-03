"""OpenAI-compatible session.

Works with:
- OpenAI directly
- Any 中转站 / proxy that exposes OpenAI-compatible /v1/chat/completions
- Self-hosted vLLM / sglang / Ollama (with caveats)
- DeepSeek / Kimi (Moonshot) / MiniMax / SiliconFlow / Together / Groq /
  OpenRouter / Fireworks / 等

Config knobs:
- base_url:        中转站 URL，例如 https://api.deepseek.com/v1
- api_key:         API key
- model:           中转站规定的模型名
- default_headers: 中转站要求的额外 header（如 X-Foo: bar）
- extra_body:      请求体里要加的额外字段（少数中转站要 user_id 之类）
- timeout:         单次请求超时（秒）
- max_retries:     openai SDK 自带重试

Tool calling:
- 默认走 OpenAI 原生 `tools` 协议，绝大多数中转站支持
- 如果你的中转站不支持，未来可以扩展 `tool_calling_mode="json_in_text"` 降级
  方案（让模型把 tool call 写在文本里，我们再解析）。当前只实现 native。
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from secagent.llm.base import LLMResponse, LLMSession, ToolCall


class OpenAICompatSession(LLMSession):
    """OpenAI-compatible chat session.

    The internal `history` list uses OpenAI message shape:
        {"role": "system" | "user" | "assistant" | "tool",
         "content": str | None,
         "tool_calls": [ {id, type, function: {name, arguments}} ],   # for assistant
         "tool_call_id": "..."}                                        # for tool
    """

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        base_url: str = "https://api.openai.com/v1",
        default_headers: Optional[dict] = None,
        extra_body: Optional[dict] = None,
        max_tokens: int = 8192,
        temperature: float = 0.2,
        timeout: float = 120.0,
        max_retries: int = 2,
        name: Optional[str] = None,
    ):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("pip install openai") from e

        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.extra_body = extra_body or {}
        self.name = name or f"oai:{base_url}:{model}"

        # api_key may be in env var; allow caller to pass literal "ENV:VAR_NAME"
        key = api_key or os.environ.get("OPENAI_API_KEY") or ""
        if isinstance(key, str) and key.startswith("ENV:"):
            key = os.environ.get(key[4:], "")
        if not key:
            raise ValueError(f"No API key for backend {self.name}")

        self._client = OpenAI(
            base_url=base_url,
            api_key=key,
            default_headers=default_headers or {},
            timeout=timeout,
            max_retries=max_retries,
        )

        # internal state
        self.history: list[dict] = []
        self._system: str = ""
        self._tool_schema_dirty = True   # force re-send schema first call

    # ---------- LLMSession protocol ----------

    def reset_tool_schema_cache(self) -> None:
        self._tool_schema_dirty = True

    def chat(self, messages: list[dict], tools: Optional[list] = None) -> LLMResponse:
        # 1) absorb generic messages into self.history (OpenAI shape)
        self._absorb_generic(messages)

        # 2) build request
        req_messages = []
        if self._system:
            req_messages.append({"role": "system", "content": self._system})
        req_messages.extend(self.history)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": req_messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            kwargs["tool_choice"] = "auto"
        if self.extra_body:
            kwargs.update(self.extra_body)

        # 3) call
        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        # 4) push assistant turn back into history (preserving tool_calls)
        assistant_entry: dict[str, Any] = {
            "role": "assistant",
            "content": msg.content or "",
        }
        if getattr(msg, "tool_calls", None):
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
            # OpenAI protocol: when tool_calls present, content can be empty string
            # but must exist on the message
            if assistant_entry["content"] is None:
                assistant_entry["content"] = ""
        self.history.append(assistant_entry)

        # 5) parse to our generic LLMResponse
        tool_calls = []
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    # some 中转站偶尔吐出非法 JSON,降级成空 dict 让上层报错
                    args = {"_raw_arguments": tc.function.arguments}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))

        return LLMResponse(content=msg.content or "", tool_calls=tool_calls)

    # ---------- helpers ----------

    def _absorb_generic(self, messages: list[dict]) -> None:
        """Translate our generic 'messages' shape into OpenAI history shape."""
        for m in messages:
            role = m.get("role")
            if role == "system":
                if not self._system:
                    self._system = m.get("content", "")
                continue

            if role == "user":
                tool_results = m.get("tool_results") or []
                if tool_results:
                    # In OpenAI protocol, tool_result is a separate message
                    # with role=tool, tool_call_id=..., content=...
                    for tr in tool_results:
                        self.history.append({
                            "role": "tool",
                            "tool_call_id": tr["tool_use_id"],
                            "content": tr["content"],
                        })
                    # Optional extra user note alongside the tool_results
                    extra = m.get("content") or ""
                    if extra.strip():
                        self.history.append({"role": "user", "content": extra})
                else:
                    self.history.append({"role": "user", "content": m.get("content", "")})
            elif role == "assistant":
                # rarely passed directly; supported for replay/import
                self.history.append({"role": "assistant", "content": m.get("content", "")})
            else:
                # unknown role: skip silently
                pass

    @staticmethod
    def _convert_tools(tools_schema: list) -> list:
        """Our internal schema (OpenAI-ish) → strict OpenAI `tools` array."""
        out = []
        for t in tools_schema:
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                },
            })
        return out

    # ---------- history I/O for Mixin migration ----------

    def export_history(self) -> dict:
        return {"system": self._system, "history": list(self.history)}

    def import_history(self, blob: dict) -> None:
        self._system = blob.get("system", "")
        self.history = list(blob.get("history") or [])
