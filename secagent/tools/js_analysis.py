"""Web-reverse-engineering tools.

Three native tools for the daily JS reverse workflow:

  js_execute    — run a JS snippet INSIDE A SANDBOX to verify a hypothesis
                  about an encryption / signature algorithm. Modes:
                    * docker  (recommended) — --network none, read-only FS,
                              dropped capabilities, nobody user, cgroup limits
                    * node-permission — Node 20+ permission model, FS-confined
                              (NOTE: does NOT block network)
                    * raw     — explicit opt-in only, no isolation
                  ALWAYS goes through require_approval gate.
  har_analyze   — parse a HAR export from Chrome DevTools / Burp and pull
                  out interesting requests by host / url / method / status.
  code_diff     — unified diff between two files (typically two versions of
                  a minified JS bundle, to spot what changed across releases).

These complement the JS-reverse / Playwright MCP servers — those handle live
browser automation and AST manipulation; these handle the pieces that live
inside the engagement dir (HARs, dumped JS, sandbox verification scripts).
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


# ----------------------------- helpers ----------------------------------

def _truncate(s: str, cap: int = 4000) -> str:
    if not s:
        return ""
    if len(s) > cap:
        return s[:cap] + f"\n... [truncated, {len(s)-cap} more chars]"
    return s


def _resolve_in_engagement(eng_dir: Path, p: str) -> tuple[Optional[Path], Optional[str]]:
    """Resolve `p` (relative or absolute) and ensure it lives inside engagement_dir.
    Returns (path, None) on success or (None, error_message)."""
    pp = Path(p)
    target = pp.resolve() if pp.is_absolute() else (eng_dir / p).resolve()
    if not str(target).startswith(str(eng_dir.resolve())):
        return None, f"path '{p}' is outside the engagement directory"
    return target, None


# =========================================================================
# js_execute — sandboxed Node runner
# =========================================================================

_JS_WRAPPER = r"""
const __inputs = __INPUTS_JSON__;
(async () => {
  try {
    const __result = await (async () => {
      __USER_CODE__
    })();
    process.stdout.write("\n__SECAGENT_RESULT__" + JSON.stringify({
      ok: true,
      result: __result === undefined ? null : __result,
    }));
  } catch (e) {
    process.stdout.write("\n__SECAGENT_RESULT__" + JSON.stringify({
      ok: false,
      error: String(e && e.message || e),
      stack: (e && e.stack) ? String(e.stack) : null,
    }));
    process.exit(1);
  }
})();
"""

_DEFAULT_DOCKER_IMAGE = "node:20-alpine"


# ---------- capability detection ----------

def _docker_available() -> tuple[bool, str]:
    """Return (ok, reason)."""
    if not shutil.which("docker"):
        return False, "docker CLI not in PATH"
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        return False, "docker version timed out (daemon hung?)"
    except Exception as e:
        return False, f"docker probe error: {e}"
    if proc.returncode != 0:
        return False, f"docker daemon not responding: {(proc.stderr or proc.stdout)[:200].strip()}"
    return True, ""


def _node_version() -> tuple[int, int, int]:
    """Return (major, minor, patch). (0,0,0) means absent or unparseable."""
    if not shutil.which("node"):
        return (0, 0, 0)
    try:
        proc = subprocess.run(["node", "-v"], capture_output=True, text=True, timeout=5)
    except Exception:
        return (0, 0, 0)
    if proc.returncode != 0:
        return (0, 0, 0)
    v = (proc.stdout or "").strip().lstrip("v")
    parts = v.split(".")
    try:
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0, int(parts[2]) if len(parts) > 2 else 0)
    except (ValueError, IndexError):
        return (0, 0, 0)


def detect_sandbox_capabilities() -> dict:
    """Return what sandbox modes are available on this host. Used by REPL banner."""
    docker_ok, docker_reason = _docker_available()
    nv = _node_version()
    return {
        "docker": {
            "available": docker_ok,
            "reason":    docker_reason if not docker_ok else "ready",
        },
        "node_permission": {
            "available": nv >= (20, 0, 0),
            "node_version": ".".join(str(x) for x in nv) if nv != (0, 0, 0) else "(not installed)",
            "note": "FS-confined, but does NOT block network",
        },
        "raw": {
            "available": nv != (0, 0, 0),
            "warning":   "no isolation -- full network/filesystem access",
        },
    }


# ---------- mode runners ----------

def _run_docker_sandbox(
    script_path: Path,
    image: str,
    timeout: int,
    memory_mb: int,
    cpus: float,
) -> tuple[int, str, str]:
    """Run `script_path` inside a hardened docker container.

    Hardening:
      --network none           no network at all
      --read-only              root FS read-only
      --tmpfs /tmp:size=64m    a small writable scratch
      --memory                 memory limit
      --cpus                   CPU limit
      --pids-limit 64          fork-bomb resistance
      --cap-drop ALL           drop all Linux capabilities
      --security-opt no-new-privileges
      --user 65534:65534       run as nobody:nogroup
      script mounted read-only
    """
    # docker on Windows accepts forward slashes in -v paths.
    host_path = str(script_path.resolve()).replace("\\", "/")
    cmd = [
        "docker", "run", "--rm",
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:size=64m,mode=1777",
        "--memory", f"{memory_mb}m",
        "--cpus", str(cpus),
        "--pids-limit", "64",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", "65534:65534",
        "-v", f"{host_path}:/work/script.js:ro",
        "-w", "/work",
        image,
        "node", "--no-warnings", "script.js",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _run_node_permission(
    script_path: Path,
    tmp_dir: Path,
    timeout: int,
    node_version: tuple[int, int, int],
) -> tuple[int, str, str]:
    """Run via Node permission model. NOTE: does not block network."""
    # Flag rename: --experimental-permission (Node 20-22) → --permission (Node 23.5+).
    flag = "--permission" if node_version >= (23, 5, 0) else "--experimental-permission"
    cmd = [
        "node",
        flag,
        f"--allow-fs-read={script_path.resolve()}",
        f"--allow-fs-write={tmp_dir.resolve()}",
        "--no-warnings",
        str(script_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _run_raw(script_path: Path, timeout: int, cwd: Path) -> tuple[int, str, str]:
    """No isolation. Caller must have explicitly set sandbox_mode='raw'."""
    cmd = ["node", "--no-warnings", str(script_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(cwd))
    return proc.returncode, proc.stdout, proc.stderr


# ---------- main entry ----------

def _do_js_execute(args: dict, ctx: dict) -> StepOutcome:
    code = (args.get("code") or "").strip()
    if not code:
        return StepOutcome.error("js_execute: code is required")
    inputs       = args.get("inputs") or {}
    timeout      = int(args.get("timeout", 30))
    sandbox_mode = (args.get("sandbox_mode") or "auto").lower()

    eng_dir: Path = ctx["engagement_dir"]
    tmp_dir = eng_dir / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    ts = int(time.time() * 1000)
    script_path = tmp_dir / f"js_run_{ts}.js"

    wrapper = (
        _JS_WRAPPER
        .replace("__INPUTS_JSON__", json.dumps(inputs))
        .replace("__USER_CODE__", code)
    )
    script_path.write_text(wrapper, encoding="utf-8")

    # ---------- resolve "auto" ----------
    if sandbox_mode == "auto":
        ok, _ = _docker_available()
        if ok:
            sandbox_mode = "docker"
        elif _node_version() >= (20, 0, 0):
            sandbox_mode = "node-permission"
        else:
            return StepOutcome.error(
                "js_execute: no sandbox available. Install Docker (recommended), "
                "or upgrade Node to >= 20 for the (weaker) permission-model fallback. "
                "If you accept full network/filesystem access, pass sandbox_mode='raw' "
                "explicitly — but consider whether this is what you really want."
            )

    sandbox_info: dict = {"mode": sandbox_mode}
    rc: int = -1
    out: str = ""
    err_out: str = ""

    # ---------- dispatch ----------
    try:
        if sandbox_mode == "docker":
            ok, reason = _docker_available()
            if not ok:
                return StepOutcome.error(
                    f"js_execute: docker mode unavailable: {reason}. "
                    "Start Docker Desktop / docker engine, or pass "
                    "sandbox_mode='node-permission' (Node 20+ required, network NOT blocked)."
                )
            image     = args.get("docker_image") or _DEFAULT_DOCKER_IMAGE
            memory_mb = int(args.get("memory_mb", 256))
            cpus      = float(args.get("cpus", 1.0))
            sandbox_info.update({
                "image":     image,
                "memory_mb": memory_mb,
                "cpus":      cpus,
                "network":   "none",
                "fs":        "read-only root + tmpfs /tmp",
                "user":      "nobody (65534)",
            })
            rc, out, err_out = _run_docker_sandbox(script_path, image, timeout, memory_mb, cpus)

        elif sandbox_mode == "node-permission":
            nv = _node_version()
            if nv < (20, 0, 0):
                return StepOutcome.error(
                    f"js_execute: node-permission requires Node >= 20; found {nv if nv != (0,0,0) else 'no node'}. "
                    "Either upgrade Node, or use sandbox_mode='docker'."
                )
            sandbox_info.update({
                "node_version":  ".".join(str(x) for x in nv),
                "fs":            f"read={script_path.resolve()}; write={tmp_dir.resolve()}",
                "network":       "NOT blocked (Node permission model does not cover network -- use docker for network isolation)",
            })
            rc, out, err_out = _run_node_permission(script_path, tmp_dir, timeout, nv)

        elif sandbox_mode == "raw":
            if not shutil.which("node"):
                return StepOutcome.error("js_execute: Node not in PATH for raw mode")
            sandbox_info["warning"] = (
                "NO SANDBOX — this snippet ran with full network and filesystem access. "
                "Treat any output as untrusted."
            )
            rc, out, err_out = _run_raw(script_path, timeout, eng_dir)

        else:
            return StepOutcome.error(
                f"js_execute: unknown sandbox_mode '{sandbox_mode}'. "
                "Use 'auto' (default), 'docker', 'node-permission', or 'raw'."
            )
    except subprocess.TimeoutExpired:
        return StepOutcome.error(f"js_execute: timed out after {timeout}s ({sandbox_mode} mode)")
    except Exception as e:
        return StepOutcome.error(f"js_execute ({sandbox_mode}): {e}")

    # ---------- parse marker ----------
    marker = "__SECAGENT_RESULT__"
    idx = (out or "").rfind(marker)
    if idx >= 0:
        before = out[:idx]
        json_str = out[idx + len(marker):].strip()
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            parsed = {"ok": False, "error": "could not parse __SECAGENT_RESULT__ payload"}
    else:
        before = out
        parsed = {
            "ok":    False,
            "error": "no __SECAGENT_RESULT__ marker — script may have crashed before completion or was killed by sandbox",
        }

    return StepOutcome.cont(
        data={
            "ok":          parsed.get("ok", False),
            "result":      parsed.get("result"),
            "error":       parsed.get("error"),
            "stack":       _truncate(parsed.get("stack") or "", 1500),
            "stdout":      _truncate(before),
            "stderr":      _truncate(err_out or ""),
            "exit_code":   rc,
            "script_path": str(script_path.relative_to(eng_dir)),
            "sandbox":     sandbox_info,
        },
        prompt=(
            f"js_execute ok ({sandbox_mode}) — result type: {type(parsed.get('result')).__name__}; "
            f"compare against the target's actual response to verify"
            if parsed.get("ok") else
            f"js_execute failed ({sandbox_mode}): {(parsed.get('error') or '?')[:200]}"
        ),
    )


# =========================================================================
# har_analyze
# =========================================================================

def _do_har_analyze(args: dict, ctx: dict) -> StepOutcome:
    eng_dir: Path = ctx["engagement_dir"]
    p = args.get("path") or ""
    if not p:
        return StepOutcome.error("har_analyze: path is required")
    target, err = _resolve_in_engagement(eng_dir, p)
    if err:
        return StepOutcome.error(f"har_analyze: {err}")
    if not target.exists():
        return StepOutcome.error(f"har_analyze: not found: {target}")

    try:
        har = json.loads(target.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        return StepOutcome.error(f"har_analyze: invalid JSON: {e}")

    entries = (har.get("log") or {}).get("entries") or []
    if not isinstance(entries, list):
        return StepOutcome.error("har_analyze: log.entries is not a list — is this a real HAR file?")

    f = args.get("filter") or {}
    try:
        host_re = re.compile(f["host"]) if f.get("host") else None
        url_re = re.compile(f["url_pattern"]) if f.get("url_pattern") else None
    except re.error as e:
        return StepOutcome.error(f"har_analyze: bad regex in filter: {e}")
    method_filter = (f.get("method") or "").upper()
    status_min = int(f.get("status_min", 0))
    status_max = int(f.get("status_max", 999))

    max_results = int(args.get("max_results", 200))

    rows: list[dict] = []
    by_host: dict[str, int] = {}
    by_status: dict[int, int] = {}
    by_mime: dict[str, int] = {}

    for e in entries:
        if not isinstance(e, dict):
            continue
        req = e.get("request") or {}
        resp = e.get("response") or {}
        url = req.get("url") or ""
        method = (req.get("method") or "").upper()
        try:
            status = int(resp.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        try:
            host = urlparse(url).netloc
        except Exception:
            host = ""
        content = resp.get("content") or {}
        mime = content.get("mimeType") or ""
        size = content.get("size") or 0
        post = req.get("postData") or {}
        post_text = post.get("text") or ""

        if host_re and not host_re.search(host):
            continue
        if url_re and not url_re.search(url):
            continue
        if method_filter and method != method_filter:
            continue
        if not (status_min <= status <= status_max):
            continue

        rows.append({
            "method":       method,
            "url":          url,
            "host":         host,
            "status":       status,
            "mime":         mime,
            "size":         size,
            "has_post":     bool(post_text),
            "post_preview": post_text[:200] if post_text else None,
        })
        by_host[host]     = by_host.get(host, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        by_mime[mime]     = by_mime.get(mime, 0) + 1

    return StepOutcome.cont(
        data={
            "har_path":      str(target.relative_to(eng_dir)),
            "total_entries": len(entries),
            "matched":       len(rows),
            "rows":          rows[:max_results],
            "truncated":     len(rows) > max_results,
            "by_host":       dict(sorted(by_host.items(), key=lambda x: -x[1])[:20]),
            "by_status":     by_status,
            "by_mime":       dict(sorted(by_mime.items(), key=lambda x: -x[1])[:10]),
        },
        prompt=(
            f"har_analyze: matched {len(rows)}/{len(entries)} entries "
            f"across {len(by_host)} hosts; pick interesting endpoints to inspect"
        ),
    )


# =========================================================================
# code_diff
# =========================================================================

def _do_code_diff(args: dict, ctx: dict) -> StepOutcome:
    eng_dir: Path = ctx["engagement_dir"]
    pa = args.get("path_a") or ""
    pb = args.get("path_b") or ""
    if not pa or not pb:
        return StepOutcome.error("code_diff: path_a and path_b are required")

    def _read(p: str) -> tuple[Optional[list[str]], Optional[str]]:
        target, err = _resolve_in_engagement(eng_dir, p)
        if err:
            return None, err
        if not target.exists():
            return None, f"not found: {target}"
        try:
            return target.read_text(encoding="utf-8", errors="replace").splitlines(), None
        except Exception as e:
            return None, str(e)

    a, err = _read(pa)
    if err: return StepOutcome.error(f"code_diff: {err}")
    b, err = _read(pb)
    if err: return StepOutcome.error(f"code_diff: {err}")

    mode = args.get("mode") or "unified"
    n_ctx = int(args.get("context", 3))

    if mode == "stats":
        sm = difflib.SequenceMatcher(a=a, b=b)
        ratio = sm.ratio()
        return StepOutcome.cont(
            data={
                "path_a": pa, "path_b": pb,
                "lines_a": len(a), "lines_b": len(b),
                "similarity": round(ratio, 4),
                "identical":  ratio >= 0.9999,
            },
            prompt=f"code_diff stats: similarity={ratio:.3f}; lines {len(a)} vs {len(b)}",
        )

    if mode == "unified":
        diff = list(difflib.unified_diff(a, b, fromfile=pa, tofile=pb, n=n_ctx, lineterm=""))
        added   = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        joined = "\n".join(diff)
        return StepOutcome.cont(
            data={
                "path_a": pa, "path_b": pb,
                "lines_a": len(a), "lines_b": len(b),
                "added": added, "removed": removed,
                "diff":  _truncate(joined, 12000),
            },
            prompt=f"code_diff: +{added}/-{removed} lines",
        )

    return StepOutcome.error(f"code_diff: unknown mode '{mode}' (use 'unified' or 'stats')")


# =========================================================================
# register
# =========================================================================

def register(reg: ToolRegistry) -> None:
    reg.register(
        name="js_execute",
        description=(
            "Execute a JavaScript snippet INSIDE A SANDBOX to verify a hypothesis about an "
            "encryption / signing / encoding algorithm during reverse engineering. "
            "The snippet has access to a global `__inputs` object you provide; the captured "
            "return value (or last awaited expression of the async IIFE) is sent back. "
            "\n\n"
            "Sandbox modes (set via sandbox_mode):\n"
            "  - 'auto' (default): pick the strongest available — docker > node-permission > error.\n"
            "  - 'docker':         hardened container, --network none, FS read-only, nobody user, "
            "                      cgroup limits. Recommended.\n"
            "  - 'node-permission': Node 20+ permission model. FS confined to the script and "
            "                      <eng>/.tmp. WARNING: does NOT block network.\n"
            "  - 'raw':            no isolation. Only when you explicitly need network or "
            "                      capabilities the sandbox blocks; user will see this in the "
            "                      approval prompt.\n"
            "\n"
            "Always in require_approval. Snippet is also persisted at "
            "`<engagement>/.tmp/js_run_<ts>.js` for the audit trail."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "JS source. Wrapped in an async IIFE; you can use top-level await. "
                        "Available globals: __inputs (object you passed), Node built-ins. "
                        "Return the value you want captured."
                    ),
                },
                "inputs": {
                    "type": "object",
                    "description": "JSON-serializable object exposed as __inputs inside the snippet.",
                },
                "timeout": {"type": "integer", "default": 30},
                "sandbox_mode": {
                    "type": "string",
                    "enum": ["auto", "docker", "node-permission", "raw"],
                    "default": "auto",
                    "description": "Sandbox to run the snippet in. Default 'auto' picks the strongest available.",
                },
                "docker_image": {
                    "type": "string",
                    "default": _DEFAULT_DOCKER_IMAGE,
                    "description": "(docker mode only) image to run inside. Should have node binary on PATH.",
                },
                "memory_mb": {
                    "type": "integer",
                    "default": 256,
                    "description": "(docker mode only) memory cgroup limit, MB.",
                },
                "cpus": {
                    "type": "number",
                    "default": 1.0,
                    "description": "(docker mode only) CPU cgroup limit.",
                },
            },
            "required": ["code"],
        },
        fn=_do_js_execute,
        operation="js_execute",
        side_effects="exec",
        category="js_reverse",
    )

    reg.register(
        name="har_analyze",
        description=(
            "Parse a HAR file (HTTP Archive — File → Save All as HAR with content from "
            "Chrome DevTools Network panel, or export from Burp Suite) and return a filtered "
            "summary of HTTP exchanges. Use this when you have a recording but need to find "
            "the suspicious endpoint among hundreds of requests."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to a .har file, relative to the engagement dir.",
                },
                "filter": {
                    "type": "object",
                    "description": "Optional filters; all are AND-ed.",
                    "properties": {
                        "host":        {"type": "string", "description": "regex against request host"},
                        "url_pattern": {"type": "string", "description": "regex against full URL"},
                        "method":      {"type": "string", "description": "GET / POST / PUT / ..."},
                        "status_min":  {"type": "integer"},
                        "status_max":  {"type": "integer"},
                    },
                },
                "max_results": {"type": "integer", "default": 200},
            },
            "required": ["path"],
        },
        fn=_do_har_analyze,
        operation="har_analyze",
        side_effects="read",
        category="js_reverse",
    )

    reg.register(
        name="code_diff",
        description=(
            "Diff two text files. Typical use: compare two versions of a minified JS bundle "
            "across releases to spot what changed (often the part the target updated to break "
            "your previous reverse-engineered script). Modes: 'unified' (full diff) or 'stats' "
            "(similarity ratio only — cheap when you just want to know if anything changed)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path_a":  {"type": "string"},
                "path_b":  {"type": "string"},
                "mode":    {"type": "string", "enum": ["unified", "stats"], "default": "unified"},
                "context": {"type": "integer", "default": 3, "description": "context lines for unified mode"},
            },
            "required": ["path_a", "path_b"],
        },
        fn=_do_code_diff,
        operation="code_diff",
        side_effects="read",
        category="js_reverse",
    )
