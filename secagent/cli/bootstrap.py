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
        "First run will pull chrome-devtools-mcp via npx (~2 min one-time download).",
        "Edit freely; the manager re-reads this on each REPL start.",
        "",
        "Default config keeps ONLY chrome-devtools-mcp (Google official, observation-",
        "focused — best fit for JS reverse engineering). playwright-mcp is dropped",
        "because it overlaps with chrome-devtools and inflates LLM decision fatigue.",
        "If you need full automation (e2e tests, click flows), add it back manually.",
    ],
    "mcpServers": {
        "js-reverse": {
            "command": "npx",
            "args": ["-y", "chrome-devtools-mcp@latest"],
            "target_keys": {
                # chrome-devtools-mcp tool names (subset that touches network).
                # Keys here are used by the scope checker to extract the URL
                # before the MCP call goes out.
                "navigate":         ["url"],
                "navigate_page":    ["url"],
                "new_page":         ["url"],
                "fetch_resource":   ["url"],
            },
            "approval_required": [
                # High-risk operations that should pop ask_user before running.
                # Names match chrome-devtools-mcp's actual tool list — extras
                # here are harmless (matching is membership-based).
                "evaluate_script",
                "inject_before_load",
                "trace_function",
                "patch_function",
                "performance_start_trace",
            ],
        },
        # playwright-mcp deliberately omitted from defaults. Uncomment if you
        # need cross-browser scripted automation:
        # "playwright": {
        #     "command": "npx",
        #     "args": ["-y", "@playwright/mcp@latest"],
        #     "target_keys": {
        #         "browser_navigate":      ["url"],
        #         "browser_open":          ["url"],
        #     },
        #     "approval_required": [
        #         "browser_evaluate",
        #         "browser_file_upload",
        #         "browser_install",
        #     ],
        # },
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


def diagnose_mcp_config(mcp_path: Path) -> Optional[dict]:
    """Inspect an existing mcp.json and report drift from current defaults.

    Returns a dict with:
      - issues: list[str]   — human-readable problems found
      - migrations: dict    — proposed changes (server name -> {action, args})
      - has_legacy: bool    — whether anything needs migrating

    Returns None if mcp.json is missing / unparseable / has no issues.
    """
    if not mcp_path.exists():
        return None
    try:
        cfg = json.loads(mcp_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    servers = (cfg.get("mcpServers") or {})
    if not servers:
        return None

    issues: list[str] = []
    migrations: dict[str, dict] = {}

    for name, scfg in servers.items():
        args = scfg.get("args") or []
        cmd_str = " ".join([scfg.get("command", "")] + list(map(str, args)))

        # 1) legacy js-reverse-mcp package — replace with chrome-devtools-mcp
        if "js-reverse-mcp" in cmd_str:
            issues.append(
                f"server '{name}' uses legacy 'js-reverse-mcp' npm package; "
                "should be 'chrome-devtools-mcp@latest' (Google official)"
            )
            migrations[name] = {
                "action": "replace_args",
                "new_args": ["-y", "chrome-devtools-mcp@latest"],
            }

        # 2) playwright server — recommend removal (cuts decision fatigue)
        if name == "playwright" or "@playwright/mcp" in cmd_str:
            issues.append(
                f"server '{name}' is playwright-mcp; current js_reverse profile "
                "drops it via allowlist anyway. Recommend removing the entry "
                "from mcp.json to avoid downloading it."
            )
            migrations[name] = {"action": "remove"}

    if not issues:
        return None
    return {"issues": issues, "migrations": migrations, "has_legacy": True}


def apply_mcp_migrations(mcp_path: Path, migrations: dict) -> bool:
    """Apply the migrations dict from diagnose_mcp_config(). Returns True if
    the file was modified.

    Backs up the original to mcp.json.bak before writing.
    """
    if not migrations:
        return False
    cfg = json.loads(mcp_path.read_text(encoding="utf-8"))
    servers = cfg.get("mcpServers") or {}

    for name, op in migrations.items():
        if name not in servers:
            continue
        action = op.get("action")
        if action == "remove":
            del servers[name]
        elif action == "replace_args":
            servers[name]["args"] = op["new_args"]

    cfg["mcpServers"] = servers
    backup = mcp_path.with_suffix(mcp_path.suffix + ".bak")
    try:
        backup.write_text(mcp_path.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    mcp_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return True


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
