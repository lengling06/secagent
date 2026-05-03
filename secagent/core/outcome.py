"""StepOutcome — controls the loop after each tool call.

Inspired by GenericAgent's three-state design:
  - should_exit=True   → terminate the entire loop immediately
  - next_prompt=None   → current task done, exit normally
  - next_prompt=str    → continue, this string becomes the next user msg
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class StepOutcome:
    data: Any = None
    next_prompt: Optional[str] = "continue"  # default: keep looping
    should_exit: bool = False

    @classmethod
    def done(cls, data: Any = None) -> "StepOutcome":
        """Tool finished, current task is complete."""
        return cls(data=data, next_prompt=None, should_exit=False)

    @classmethod
    def cont(cls, data: Any, prompt: str = "continue") -> "StepOutcome":
        """Tool finished, continue the loop with `prompt` as next user msg."""
        return cls(data=data, next_prompt=prompt, should_exit=False)

    @classmethod
    def exit(cls, reason: str) -> "StepOutcome":
        """Hard stop the entire loop."""
        return cls(data=reason, next_prompt=None, should_exit=True)

    @classmethod
    def error(cls, msg: str) -> "StepOutcome":
        """Tool failed; tell LLM what happened so it can adjust."""
        return cls(data=None, next_prompt=f"[ERROR] {msg}", should_exit=False)
