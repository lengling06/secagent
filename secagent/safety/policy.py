"""Policy: pattern-based dangerous-command/payload guard.

This is a sanity layer in addition to scope checks. Even if scope is wide
open, we still refuse outright destructive commands.
"""
from __future__ import annotations

import re
from typing import Optional


# Hard-blocked shell command patterns (cannot be overridden)
_HARD_BLOCK_SHELL = [
    re.compile(r"\brm\s+-rf\s+/(?!\s|$)", re.I),     # rm -rf / (anything not just root token)
    re.compile(r"\brm\s+-rf\s+--no-preserve-root", re.I),
    re.compile(r"\bmkfs\.\w+\b", re.I),
    re.compile(r"\bdd\s+if=.*\bof=/dev/", re.I),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.I),  # fork bomb
    re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b|\binit\s+0\b", re.I),
    re.compile(r"\bchmod\s+-R\s+777\s+/", re.I),
    re.compile(r"\b>\s*/dev/sd[a-z]\b", re.I),
]

# Patterns that REQUIRE approval (handled by Handler's approval gate, here just flag)
_APPROVAL_REQUIRED_PATTERNS = {
    "shell": [
        re.compile(r"\bsqlmap\b.*\b--dbs\b", re.I),
        re.compile(r"\bhydra\b|\bmedusa\b", re.I),                  # bruteforce
        re.compile(r"\bnmap\b.*-(s[SUFTNX]|p-)\b.*-T(4|5)\b", re.I),  # aggressive
        re.compile(r"\b(masscan|zmap)\b", re.I),
        re.compile(r"\bhping3?\b", re.I),
        re.compile(r"\bmsfconsole\b|\bmetasploit\b", re.I),
    ],
}


class PolicyEngine:
    def __init__(self):
        pass

    def check(self, tool_name: str, args: dict) -> tuple[bool, Optional[str]]:
        """Return (ok, reason). ok=False means hard block."""
        if tool_name == "shell":
            cmd = args.get("cmd", "")
            for p in _HARD_BLOCK_SHELL:
                if p.search(cmd):
                    return False, f"hard-blocked dangerous shell pattern: {p.pattern}"
        # add more tool-specific checks here as needed
        return True, None

    def needs_approval(self, tool_name: str, args: dict) -> bool:
        """Pattern-level approval flagging (in addition to scope.require_approval)."""
        pats = _APPROVAL_REQUIRED_PATTERNS.get(tool_name, [])
        if tool_name == "shell":
            cmd = args.get("cmd", "")
            for p in pats:
                if p.search(cmd):
                    return True
        return False
