"""Interactive REPL frontend.

Wires up: Scope, AuditLog, PolicyEngine, ToolRegistry, MCPManager, LLM
session, Handler, then runs the loop on each user line.

User-facing niceties beyond the dev-mode loop:
- Detects URLs the user pastes; if a URL's host is outside the current
  scope, offers to spawn (and switch into) a new engagement scoped to it.
- Prints an artifact summary at session end so the user knows where files
  landed.
- Accepts an `initial_input` so `secagent target <url>` can kick off the
  agent automatically without the user typing again.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from secagent.core.handler import Handler
from secagent.core.loop import run_loop
from secagent.llm.config import build_session, find_llm_config, load_llm_config
from secagent.mcp.manager import MCPManager
from secagent.safety.audit import AuditLog
from secagent.safety.policy import PolicyEngine
from secagent.tools.registry import build_default_registry
from secagent.tools.scope import Scope, load_scope, summarize_scope


_URL_RE = re.compile(r"https?://[^\s<>'\"\\)]+", re.IGNORECASE)


def _read_system_prompt() -> str:
    p = Path(__file__).resolve().parent.parent / "prompts" / "system_sec.md"
    return p.read_text(encoding="utf-8")


def _print_sandbox_status() -> None:
    try:
        from secagent.tools.js_analysis import detect_sandbox_capabilities
        caps = detect_sandbox_capabilities()
    except Exception as e:
        print(f"[sandbox] could not probe: {e}")
        return
    docker = caps["docker"]
    nodep  = caps["node_permission"]
    raw    = caps["raw"]
    ok = "[ok]"
    no = "[--]"
    print("js_execute sandbox:")
    print(f"  docker:           {ok + ' ready' if docker['available'] else no + ' ' + docker['reason']}")
    np_label = ok if nodep['available'] else no
    print(f"  node-permission:  {np_label} node {nodep['node_version']}")
    raw_label = ok + ' available' if raw['available'] else no + ' no node'
    print(f"  raw (no sandbox): {raw_label}")
    if not docker["available"]:
        print("  -> install Docker for full network isolation; node-permission alone does NOT block network.")


# ============================================================
# URL → engagement switch
# ============================================================

def _extract_first_external_host(text: str, scope: Scope) -> Optional[tuple[str, str]]:
    """Find the first URL in text whose host is NOT in the current scope.
    Returns (url, host) or None."""
    for url in _URL_RE.findall(text):
        host = (urlparse(url).hostname or "").lower()
        if not host:
            continue
        if scope.is_in_scope(url):
            continue
        return url, host
    return None


def _maybe_offer_switch(user_input: str, scope: Scope) -> Optional[Path]:
    """If the input contains an out-of-scope URL, offer to create+switch
    engagement. Returns new engagement dir or None."""
    hit = _extract_first_external_host(user_input, scope)
    if not hit:
        return None
    url, host = hit
    print()
    print(f"[detected target: {url}]")
    print(f"  host '{host}' is not in current engagement '{scope.engagement}' scope.")
    print(f"  options:")
    print(f"    [Y] 新建 engagement 'target_{host}_<today>' (推荐)")
    print(f"    [s] 留在当前 engagement (网络操作会被 scope 拒绝)")
    print(f"    [n] 取消这条输入")
    try:
        ans = input("  选 [Y/s/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if ans in ("n", "no"):
        return "CANCELLED"  # type: ignore[return-value]
    if ans in ("s", "stay"):
        return None
    # default Y
    from secagent.cli.bootstrap import (
        create_engagement_from_spec,
        suggest_engagement_for_url,
    )
    try:
        spec = suggest_engagement_for_url(url)
    except ValueError as e:
        print(f"  [error] {e}")
        return None
    print(f"  -> creating: {spec['path']}")
    print(f"     scope: {', '.join(spec['domains'])}")
    return create_engagement_from_spec(spec, authorized_by="self (local analysis)")


# ============================================================
# Session end summary
# ============================================================

def _print_session_summary(engagement_dir: Path, started_at: float) -> None:
    """Tell the user where things landed."""
    elapsed = int(time.time() - started_at)
    findings_dir = engagement_dir / "findings"
    n_findings = sum(1 for _ in findings_dir.glob("*.md")) if findings_dir.exists() else 0
    audit_log = engagement_dir / "audit.jsonl"
    n_audit = sum(1 for _ in audit_log.open("r", encoding="utf-8")) if audit_log.exists() else 0
    js_dir = engagement_dir / "js"
    n_js = sum(1 for _ in js_dir.rglob("*.js")) if js_dir.exists() else 0
    tmp_dir = engagement_dir / ".tmp"
    n_snippets = sum(1 for _ in tmp_dir.glob("js_run_*.js")) if tmp_dir.exists() else 0

    print()
    print("=" * 56)
    print(" Session over")
    print("=" * 56)
    print(f"  engagement:       {engagement_dir}")
    print(f"  duration:         {elapsed//60}m {elapsed%60}s")
    print(f"  findings written: {n_findings}    ({findings_dir})")
    print(f"  audit log lines:  {n_audit}    ({audit_log})")
    if n_js:
        print(f"  js files dumped:  {n_js}    ({js_dir})")
    if n_snippets:
        print(f"  js_execute runs:  {n_snippets}   ({tmp_dir})")
    print()
    if n_findings:
        print("  下次写报告时直接拿 findings/*.md 拼装。")
    print()


# ============================================================
# Main entry
# ============================================================

def run_repl(
    engagement_dir: Path,
    llm_name: Optional[str] = None,
    max_turns: int = 40,
    initial_input: Optional[str] = None,
) -> int:
    started_at = time.time()
    print(f"=== SecAgent — engagement: {engagement_dir.name} ===")

    # 1. Scope
    try:
        scope = load_scope(engagement_dir)
    except FileNotFoundError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        return 2
    if scope.is_expired():
        print("[FATAL] scope.yaml has expired. Update expires_at after re-authorization.")
        return 2

    # compact scope line for the user (full summary still gets attached to system prompt)
    in_doms = scope.in_scope_domains or []
    if in_doms:
        print(f"scope: {', '.join(in_doms[:4])}{' ...' if len(in_doms) > 4 else ''}")
    else:
        print("scope: (local only — no network targets)")

    # 2. LLM
    cfg_path = find_llm_config(engagement_dir)
    try:
        cfg = load_llm_config(engagement_dir)
        llm = build_session(cfg, backend_name=llm_name)
    except Exception as e:
        print(f"[FATAL] cannot build LLM session: {e}", file=sys.stderr)
        print(f"  config: {cfg_path}", file=sys.stderr)
        print("  hint: run `secagent init` to (re)configure", file=sys.stderr)
        return 3
    print(f"llm:   {getattr(llm, 'name', '?')} / {getattr(llm, 'model', '?')}")

    # 3. Safety
    audit = AuditLog(engagement_dir)
    audit.write(
        "session_start",
        engagement=scope.engagement,
        engagement_dir=str(engagement_dir),
        llm=getattr(llm, "name", "?"),
        model=getattr(llm, "model", "?"),
    )
    policy = PolicyEngine()

    # 4. Tools + MCP
    registry = build_default_registry()
    mcp = MCPManager(engagement_dir, registry)
    try:
        mcp.start()
    except Exception as e:
        print(f"[MCP] startup error (continuing without MCP): {e}")

    # 5. System prompt
    #    layout (top-down): base system_sec.md → JS reverse SOP (default
    #    methodology) → engagement-level sop.md (if user dropped one in) →
    #    last-session checkpoint (if exists, survives across compaction) →
    #    current scope summary.
    system_prompt = _read_system_prompt()

    # default JS reverse methodology, always loaded (this is the agent's
    # primary domain; engagement-level sop.md can override/extend if desired)
    sop_default = Path(__file__).resolve().parent.parent / "prompts" / "js_reverse_sop.md"
    if sop_default.exists():
        try:
            system_prompt += "\n\n## SOP — JS Reverse Engineering\n\n" + sop_default.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[warn] could not read js_reverse_sop.md: {e}")

    # engagement-level override / extension
    sop_path = engagement_dir / "sop.md"
    if sop_path.exists():
        try:
            system_prompt += "\n\n## Engagement SOP\n\n" + sop_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[warn] could not read sop.md: {e}")

    # last-session checkpoint (B9: survives across context compaction)
    ck = engagement_dir / "state" / "checkpoint.md"
    if ck.exists():
        try:
            ck_text = ck.read_text(encoding="utf-8").strip()
            if ck_text:
                system_prompt += "\n\n## Resume from last checkpoint\n\n" + ck_text
                print(f"checkpoint: 加载上次进度 ({len(ck_text)} chars from state/checkpoint.md)")
        except Exception as e:
            print(f"[warn] could not read checkpoint.md: {e}")

    system_prompt += "\n\n## Current scope\n\n" + summarize_scope(scope)

    # 6. Callbacks
    def approval_cb(prompt: str) -> bool:
        print(prompt)
        try:
            ans = input("approve [y/N]> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans in ("y", "yes")

    def prompt_cb(question: str, candidates: list) -> str:
        print(f"\n? {question}")
        for i, c in enumerate(candidates, 1):
            print(f"  [{i}] {c}")
        try:
            return input("answer> ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    # 7. Handler
    handler = Handler(
        registry=registry,
        scope=scope,
        policy=policy,
        audit=audit,
        engagement_dir=engagement_dir,
        approval_callback=approval_cb,
        prompt_callback=prompt_cb,
    )

    print(f"tools: {len(registry.names())} registered  (`/tools` to list)")
    _print_sandbox_status()
    print("type a request. empty line to exit.  /tools /llm /switch <name> /sandbox /quit")
    print()

    pending_input: Optional[str] = initial_input

    while True:
        # ---------- get input ----------
        if pending_input is not None:
            user_input = pending_input.strip()
            pending_input = None
            if user_input:
                print(f"> {user_input}")
        else:
            try:
                user_input = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_input:
                break
            if user_input.startswith("/"):
                if _handle_slash(user_input, llm, registry):
                    continue
                else:
                    break

        # ---------- URL → engagement switch ----------
        switch_to = _maybe_offer_switch(user_input, scope)
        if switch_to == "CANCELLED":
            continue
        if isinstance(switch_to, Path):
            audit.write("engagement_switch", to=str(switch_to))
            audit.write("session_end")
            mcp.stop()
            _print_session_summary(engagement_dir, started_at)
            print(f"-> 切到 engagement: {switch_to.name}")
            print()
            return run_repl(
                switch_to,
                llm_name=llm_name,
                max_turns=max_turns,
                initial_input=user_input,
            )

        # ---------- run agent loop ----------
        audit.write("user_input", text=user_input)
        gen = run_loop(
            llm=llm,
            handler=handler,
            system_prompt=system_prompt,
            user_input=user_input,
            tools_schema=registry.schemas(),
            max_turns=max_turns,
        )
        try:
            for chunk in gen:
                sys.stdout.write(chunk)
                sys.stdout.flush()
        except Exception as e:
            print(f"\n[loop error] {e}")
            audit.write("loop_error", error=str(e))
        print()

    audit.write("session_end")
    mcp.stop()
    _print_session_summary(engagement_dir, started_at)
    return 0


def _handle_slash(line: str, llm, registry) -> bool:
    """Returns True to continue REPL, False to quit."""
    parts = line.split()
    cmd = parts[0]
    if cmd == "/quit":
        return False
    if cmd == "/llm":
        active = getattr(llm, "_active_name", None) or getattr(llm, "name", "?")
        model = getattr(llm, "model", "?")
        print(f"  active backend: {active}")
        print(f"  model:          {model}")
        if hasattr(llm, "backends"):
            for n in llm.backends:
                marker = " *" if n == getattr(llm, "_active_name", None) else "  "
                print(f"  {marker} {n}: {llm.backends[n].model}")
        return True
    if cmd == "/switch" and len(parts) == 2:
        if hasattr(llm, "switch_to"):
            try:
                llm.switch_to(parts[1])
                print(f"  switched to {parts[1]}")
            except Exception as e:
                print(f"  switch failed: {e}")
        else:
            print("  current LLM is not a Mixin; cannot switch")
        return True
    if cmd == "/sandbox":
        _print_sandbox_status()
        return True
    if cmd == "/tools":
        print("  registered tools:")
        for n in registry.names():
            m = registry.meta(n)
            print(f"    - {n}  (op={m['operation']}, side={m['side_effects']})")
        return True
    print(f"  unknown command: {cmd}. available: /tools /llm /switch <name> /sandbox /quit")
    return True
