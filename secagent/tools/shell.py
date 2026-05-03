"""Shell tool. Run CLI commands with scope/policy gating.

The Handler already runs scope+policy checks before us via target_keys=["targets"].
We additionally enforce:
- working directory must be within engagement_dir
- timeout (default 60s)
- rate limit (sleep based on scope.rate_limit_per_second)
"""
from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


_last_call_at = 0.0


def _do_shell(args: dict, ctx: dict) -> StepOutcome:
    global _last_call_at
    cmd: str = args.get("cmd", "").strip()
    if not cmd:
        return StepOutcome.error("shell: cmd is required")

    cwd_arg = args.get("cwd")
    eng_dir: Path = ctx["engagement_dir"]
    if cwd_arg:
        cwd = (eng_dir / cwd_arg).resolve()
        # do not allow escaping engagement dir
        if not str(cwd).startswith(str(eng_dir.resolve())):
            return StepOutcome.error(f"cwd '{cwd_arg}' escapes engagement dir")
    else:
        cwd = eng_dir

    timeout = int(args.get("timeout", 60))

    # rate limit (global per scope)
    scope = ctx["scope"]
    min_interval = 1.0 / max(scope.rate_limit_per_second, 1)
    elapsed = time.time() - _last_call_at
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_call_at = time.time()

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return StepOutcome.error(f"shell: timeout after {timeout}s")
    except Exception as e:
        return StepOutcome.error(f"shell: {e}")

    out = proc.stdout
    err = proc.stderr
    # truncate huge outputs
    cap = 6000
    if len(out) > cap:
        out = out[:cap] + f"\n... [truncated, {len(out)-cap} more chars]"
    if len(err) > cap:
        err = err[:cap] + f"\n... [truncated, {len(err)-cap} more chars]"

    return StepOutcome.cont(
        data={
            "exit_code": proc.returncode,
            "stdout": out,
            "stderr": err,
            "cmd": cmd,
            "cwd": str(cwd),
        },
        prompt=f"shell exit={proc.returncode}; review output and decide next step",
    )


def register(reg: ToolRegistry) -> None:
    reg.register(
        name="shell",
        description=(
            "Run a shell command inside the current engagement directory. "
            "Use for CLI tools (nmap/sqlmap/ffuf/curl/...). "
            "REQUIRED: pass 'targets' (list of host/IP/URL the command will touch) so the "
            "scope checker can verify authorization. Without targets, the call will be rejected."
        ),
        parameters={
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Shell command to run"},
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Hosts/IPs/URLs this command will touch (for scope check)",
                },
                "cwd": {"type": "string", "description": "Relative cwd inside engagement dir"},
                "timeout": {"type": "integer", "default": 60},
            },
            "required": ["cmd", "targets"],
        },
        fn=_do_shell,
        target_keys=["targets"],
        operation="shell",
        side_effects="exec",
        category="shell",
    )
