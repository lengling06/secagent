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

Context management (B10):
- ``compact_if_needed`` 调用前会用 cl100k_base 估 history token, 超过阈值就
  触发 rolling summary (用 ``self._client`` 自己, 同 model)。
  中转站不支持 Anthropic native compaction, 所以必须 client 端做。
- 边界保护: 不切 assistant→tool 之间, 不留孤儿 tool_call_id。
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

    # ---------- context management (B10) ----------

    def approx_tokens(self) -> int:
        """Estimate current history size in tokens. Backend-agnostic via tiktoken."""
        from secagent.llm.tokens import count_history, count_tokens
        return count_history(self.history) + count_tokens(self._system)

    def compact_if_needed(
        self,
        context_window: int = 200_000,
        ratio: float = 0.78,
        keep_recent: int = 6,
        summarizer_model: Optional[str] = None,
    ) -> bool:
        """Roll up old history into a summary if usage exceeds context_window * ratio.

        Returns True if compaction happened.
        """
        threshold = int(context_window * ratio)
        used = self.approx_tokens()
        if used < threshold:
            return False
        if len(self.history) <= keep_recent + 2:
            return False  # too short, not worth it

        # Find a safe cut point: cut must NOT split an assistant→tool pair.
        # OpenAI protocol requires every tool message immediately follow an
        # assistant message that has the matching tool_call_id.
        cut = max(0, len(self.history) - keep_recent)
        # back up while we'd be cutting through a tool sequence
        while cut > 0 and self.history[cut].get("role") == "tool":
            cut -= 1
        # also: if cut points just after an assistant with tool_calls, we must
        # keep the whole tool batch with that assistant — back up further.
        # Simplest rule: only cut on a 'user' boundary.
        while cut > 0 and self.history[cut].get("role") != "user":
            cut -= 1
        if cut <= 1:
            return False  # nothing meaningful to compact

        old = self.history[:cut]
        recent = self.history[cut:]

        old_text = self._serialize_for_summary(old)
        if len(old_text) > 80_000:
            old_text = old_text[:80_000] + "\n\n[…older content truncated for summarizer]"

        try:
            summary_resp = self._client.chat.completions.create(
                model=summarizer_model or self.model,
                messages=[
                    {"role": "system", "content": _COMPACT_PROMPT},
                    {"role": "user", "content": old_text},
                ],
                max_tokens=2000,
                temperature=0,
            )
            summary = summary_resp.choices[0].message.content or "(摘要为空)"
        except Exception as e:
            # 摘要失败的硬保险: 直接砍最老一半, 不要循环失败
            n_drop = len(old) // 2
            self.history = old[n_drop:] + recent
            return True

        self.history = [
            {"role": "user", "content": f"[历史会话压缩摘要 — 原 {len(old)} 条消息]\n\n{summary}"},
            {"role": "assistant", "content": "已读取摘要, 继续推进。"},
            *recent,
        ]
        return True

    def _serialize_for_summary(self, msgs: list[dict]) -> str:
        """Stringify history for the summarizer. Strip raw tool outputs to save tokens."""
        parts: list[str] = []
        for m in msgs:
            role = m.get("role", "?")
            c = m.get("content")
            if isinstance(c, list):
                # may contain blocks; flatten to text
                c = " ".join(
                    str(b.get("text") if isinstance(b, dict) else b) for b in c
                )
            text = (c or "")[:2000]
            entry = f"[{role}] {text}"
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args_repr = (fn.get("arguments") or "")[:200]
                entry += f"\n  -> tool_call: {fn.get('name')}({args_repr})"
            if role == "tool":
                entry = f"[tool_result for {m.get('tool_call_id','?')[-8:]}] {text[:1500]}"
            parts.append(entry)
        return "\n\n".join(parts)


_COMPACT_PROMPT = """\
你是 SecAgent 的会话摘要器。把下面这段会话压缩成 ≤800 字中文摘要,
严格分 6 节, 节标题用 Markdown ## :

## 已完成
（已确定的子任务结果, **必须含坐标**: file:line / URL / 函数名 / 算法名）

## 当前状态
（在做哪一步, 卡在哪）

## 进行中
（已经开始但还没完的）

## 下一步
（明确的下一动作 1-3 条）

## 约束
（用户偏好、scope 边界、不要做的事）

## 关键事实
（绝不能丢的: 入口文件、签名函数位置、key 派生方式、AES sbox 位置、
HMAC 标志、已尝试失败的路径）

要求:
- 丢弃工具的 raw 输出和被新发现取代的旧推断
- 保留所有 file:line / URL / 函数名 / 算法关键字 这种坐标级事实
- 第三人称客观陈述, 不要写 "我"
- 用户姓名是 "小霜", 但摘要里 **不要** 写 "小霜大人" 称呼 (那是给主对话用的, 摘要只记事实)
"""

