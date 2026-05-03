"""Mixin session: route across multiple backends with fallback.

v1 design (kept simple, on purpose):

- One backend is "primary"; calls go to it first.
- On failure (network / 4xx / 5xx / timeout), fall back to the next backend
  in `fallback_order`. The active backend "sticks" until the next failure or
  an explicit `switch_to(name)` call.
- History migration is best-effort: when switching, we copy a generic-shape
  history snapshot and the new backend re-imports. Anthropic ↔ OpenAI history
  shapes differ enough that 100% lossless replay is hard; v1 simply RESTARTS
  history on switch and re-sends the system prompt + the most recent user
  message (good enough for fallback continuation).
- For task-type routing (cheap model for summaries, strong model for exploits),
  use `route(task_kind)` from your application; not auto-detected here.

If you need richer behavior (true history replay, streaming fallback, cost
budgeting), this is the place to extend.
"""
from __future__ import annotations

from typing import Optional

from secagent.llm.base import LLMResponse, LLMSession


class MixinSession(LLMSession):
    def __init__(
        self,
        backends: dict[str, LLMSession],
        primary: str,
        fallback_order: Optional[list[str]] = None,
        verbose: bool = True,
    ):
        if primary not in backends:
            raise ValueError(f"primary '{primary}' not in backends")
        self.backends = backends
        self.primary = primary
        self.fallback_order = fallback_order or [n for n in backends if n != primary]
        self._active_name = primary
        self.verbose = verbose
        self.name = f"mixin({primary}+{','.join(self.fallback_order)})"
        # last user input we saw, used as fallback seed
        self._last_user_seed: str = ""

    @property
    def active(self) -> LLMSession:
        return self.backends[self._active_name]

    @property
    def model(self) -> str:
        return self.active.model

    @property
    def history(self) -> list:
        return self.active.history

    def reset_tool_schema_cache(self) -> None:
        for b in self.backends.values():
            b.reset_tool_schema_cache()

    def switch_to(self, name: str) -> None:
        if name not in self.backends:
            raise KeyError(name)
        if self.verbose:
            print(f"[mixin] switch {self._active_name} -> {name}")
        self._active_name = name

    def chat(self, messages: list[dict], tools=None) -> LLMResponse:
        # remember last user seed for fallback restart
        for m in messages:
            if m.get("role") == "user":
                c = m.get("content") or ""
                if c.strip():
                    self._last_user_seed = c

        # try active, then fallback_order in turn
        order = [self._active_name] + [n for n in self.fallback_order if n != self._active_name]
        last_err: Optional[Exception] = None

        for i, name in enumerate(order):
            backend = self.backends[name]
            try:
                if i > 0:
                    # fallback: restart with system+last user, drop broken history
                    if self.verbose:
                        print(f"[mixin] {self._active_name} failed; falling back to {name}")
                    self._active_name = name
                    # find the system message, if any, in incoming messages
                    sys_msg = next((m for m in messages if m.get("role") == "system"), None)
                    seed: list[dict] = []
                    if sys_msg:
                        seed.append(sys_msg)
                    seed.append({"role": "user", "content": self._last_user_seed or "(continue)"})
                    return backend.chat(seed, tools)
                return backend.chat(messages, tools)
            except Exception as e:
                last_err = e
                if self.verbose:
                    print(f"[mixin] backend '{name}' raised: {e!r}")

        assert last_err is not None
        raise last_err
