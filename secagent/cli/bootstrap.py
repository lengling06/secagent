"""CLI helpers: bootstrap engagements, dynamically scope from a URL.

Lives apart from main.py so it can be imported by both the CLI dispatcher and
the REPL's URL-detection hook.

Layout used by the user-facing CLI:

    ~/.secagent/
      llm.yaml                       # written by `secagent init`
      engagements/
        default/                     # auto-created on first run; scratch space
          scope.yaml                 # local-only, no network targets
          mcp.json                   # empty by default
          sop.md                     # optional, agent SOP
          audit.jsonl
        target_<host>_<date>/        # auto-created when user pastes a URL
          scope.yaml                 # scope = host + *.host, full op set
          mcp.json                   # auto-filled from defaults if node present
          ...

Engagement creation deliberately does NOT touch the repo's `engagements/`
directory — that one is for the dev/example mode and stays untouched.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


# ============================================================
# Paths
# ============================================================

def user_secagent_home() -> Path:
    """`~/.secagent/`. Created if missing."""
    home = Path.home() / ".secagent"
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except (OSError, NotImplementedError):
        pass  # Windows / no-op
    return home


def user_engagements_dir() -> Path:
    d = user_secagent_home() / "engagements"
    d.mkdir(parents=True, exist_ok=True)
    return d


def user_llm_config_path() -> Path:
    return user_secagent_home() / "llm.yaml"


# ============================================================
# Scope templates
# ============================================================

def _today() -> str:
    return _dt.date.today().isoformat()


def _expires(days: int = 30) -> str:
    return (_dt.date.today() + _dt.timedelta(days=days)).isoformat()


_DEFAULT_SCRATCH_SCOPE = """\
# Default scratch engagement — local analysis only.
# Network operations (port_scan, http_probe, browser_navigate, etc.) will be
# rejected because in_scope is empty. To target a real site, paste a URL into
# the chat and SecAgent will offer to create a properly-scoped engagement.

engagement: default
authorized_by: "self"
authorized_at: "{today}"
expires_at: "2099-12-31"

in_scope:
  domains: []
  ips: []
  apis: []

out_of_scope:
  domains: []
  ips: []

# Local-only operations. Anything network-shaped is excluded on purpose.
allowed_operations:
  - file_read
  - file_write
  - ask_user
  - add_finding
  - checkpoint_write
  - task_complete
  - code_diff
  - har_analyze
  - js_beautify
  - js_execute      # sandbox-protected; require_approval applies
  - shell           # gated by target scope per-call

forbidden_operations:
  - dos
  - bruteforce_password
  - data_exfiltration
  - destructive_write
  - phishing

require_approval:
  - js_execute
  - shell

network:
  proxy: null
  user_agent_tag: "SecAgent/default"
  rate_limit_per_second: 5

notes: |
  Default scratch space. Use chat to load HARs, diff JS bundles, run sandboxed
  algorithms, etc. Hand SecAgent a target URL to spawn a real engagement.
"""


_TARGET_SCOPE = """\
# Auto-created from a URL the user pasted into chat.
# Review and tighten before doing anything sensitive.

engagement: {name}
authorized_by: {authorized_by}
authorized_at: "{today}"
expires_at: "{expires}"   # 30-day default

in_scope:
  domains:
{domain_lines}
  ips: []
  apis: []

out_of_scope:
  domains: []
  ips: []

allowed_operations:
  # === JS 逆向核心（默认开启） ===
  - http_request
  - http_probe          # 单点拉 JS / 主页 / sourcemap, 不是批量探活
  - js_reverse
  - js_execute
  - js_beautify
  - sourcemap_fetch
  - har_analyze
  - code_diff
  - browser_automation
  - shell
  - file_read
  - file_write
  - ask_user
  - add_finding
  - checkpoint_write
  - task_complete
  # === Recon (默认关闭, 需要时取消注释 + scope.yaml 同步) ===
  # - dns_resolve
  # - subdomain_enum
  # - port_scan
  # - vulnerability_scan

forbidden_operations:
  - dos
  - bruteforce_password
  - data_exfiltration
  - destructive_write
  - phishing

require_approval:
  - sql_injection_payload
  - rce_payload
  - file_upload_test
  - high_qps_scan
  - js_execute
  - js_reverse__evaluate_script
  - js_reverse__inject_before_load
  - js_reverse__trace_function
  # 默认 recon 不在 allowed 里; 若你取消注释开启 recon, 把这几条也加上 require_approval:
  # - subdomain_enum
  # - port_scan
  # - vulnerability_scan

network:
  proxy: null
  user_agent_tag: "SecAgent/{name}"
  rate_limit_per_second: 5

notes: |
  Auto-generated for: {seed_url}
  If this isn't your authorized target, edit / delete before running anything.
"""


# ============================================================
# MCP defaults
# ============================================================

def _has_cmd(name: str) -> bool:
    return shutil.which(name) is not None


def _node_npx_available() -> bool:
    return _has_cmd("node") and _has_cmd("npx")


_DEFAULT_MCP_WITH_NODE = {
    "_README": [
        "Auto-generated by `secagent` because node + npx were detected.",
        "First run will pull js-reverse-mcp and playwright-mcp via npx (~slow once).",
        "Edit freely; the manager re-reads this on each REPL start.",
    ],
    "mcpServers": {
        "js-reverse": {
            "command": "npx",
            "args": ["-y", "js-reverse-mcp"],
            "target_keys": {
                "navigate":      ["url"],
                "new_page":      ["url"],
                "navigate_page": ["url"],
                "fetch_resource": ["url"],
            },
            "approval_required": [
                "evaluate_script",
                "inject_before_load",
                "trace_function",
                "patch_function",
            ],
        },
        "playwright": {
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest"],
            "target_keys": {
                "browser_navigate":      ["url"],
                "browser_open":          ["url"],
                "browser_navigate_back": [],
            },
            "approval_required": [
                "browser_evaluate",
                "browser_file_upload",
                "browser_install",
            ],
        },
    },
}

_EMPTY_MCP = {"mcpServers": {}}


# ============================================================
# Engagement creation
# ============================================================

def ensure_default_engagement() -> Path:
    """Make sure ~/.secagent/engagements/default/ exists. Return its path."""
    ed = user_engagements_dir() / "default"
    if (ed / "scope.yaml").exists():
        return ed
    ed.mkdir(parents=True, exist_ok=True)
    (ed / "scope.yaml").write_text(
        _DEFAULT_SCRATCH_SCOPE.format(today=_today()),
        encoding="utf-8",
    )
    _write_default_mcp(ed)
    return ed


def _write_default_mcp(eng_dir: Path) -> None:
    target = eng_dir / "mcp.json"
    if target.exists():
        return
    cfg = _DEFAULT_MCP_WITH_NODE if _node_npx_available() else _EMPTY_MCP
    target.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _slug(s: str) -> str:
    """Make a host string safe for a directory name."""
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    return s.strip("._-") or "target"


def suggest_engagement_for_url(url: str, *, base_dir: Optional[Path] = None) -> dict:
    """Compute (don't create yet) an engagement spec from a URL.

    Returns a dict with: name, path, host, domains (list of fnmatch patterns),
    seed_url. Caller shows this to the user, then calls create_engagement_from_spec.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or parsed.path).split("@")[-1].split(":")[0].strip().lower()
    if not host:
        raise ValueError(f"could not extract host from URL: {url}")
    base = base_dir or user_engagements_dir()
    name = f"{_slug(host)}_{_dt.date.today().strftime('%Y%m%d')}"
    # Avoid collision: append -2, -3, ...
    path = base / name
    n = 2
    while path.exists():
        path = base / f"{name}-{n}"
        n += 1
    domains = [host, f"*.{host}"] if not host.startswith("*.") else [host]
    return {
        "name":     path.name,
        "path":     path,
        "host":     host,
        "domains":  domains,
        "seed_url": url,
    }


def create_engagement_from_spec(spec: dict, *, authorized_by: str = "self (local analysis)") -> Path:
    """Materialize the engagement directory from a suggest_engagement_for_url() spec."""
    path: Path = spec["path"]
    path.mkdir(parents=True, exist_ok=True)
    domain_lines = "\n".join(f'    - "{d}"' for d in spec["domains"])
    scope_yaml = _TARGET_SCOPE.format(
        name=spec["name"],
        authorized_by=json.dumps(authorized_by),  # quote-safe
        today=_today(),
        expires=_expires(30),
        domain_lines=domain_lines,
        seed_url=spec["seed_url"],
    )
    (path / "scope.yaml").write_text(scope_yaml, encoding="utf-8")
    _write_default_mcp(path)
    return path


# ============================================================
# Connection / sandbox probes (used by init wizard)
# ============================================================

def probe_llm_connection(cfg: dict) -> tuple[bool, str]:
    """Send a 1-token ping to verify the LLM works. Returns (ok, message)."""
    try:
        from secagent.llm.config import build_session
        sess = build_session(cfg)
    except Exception as e:
        return False, f"build_session failed: {e}"
    try:
        resp = sess.chat(
            messages=[
                {"role": "system", "content": "reply with the single word: ok"},
                {"role": "user", "content": "ping"},
            ],
            tools=[],
        )
        text = (getattr(resp, "content", "") or "").strip().lower()
        if not text:
            return False, "LLM returned empty text"
        return True, f"reply: {text[:80]}"
    except Exception as e:
        return False, f"chat failed: {e}"


def probe_sandbox() -> dict:
    from secagent.tools.js_analysis import detect_sandbox_capabilities
    return detect_sandbox_capabilities()
