"""Recon tools — built-in native tools for SecAgent.

Wraps ProjectDiscovery-style CLIs (subfinder / httpx / dnsx) plus nmap into
clean tool calls with normalized JSON output.

Why built-in (rather than a separate MCP):
- They need to read scope.yaml and write engagement audit.jsonl, which is
  cheap when in-process and clunky when out-of-process.
- They reuse the agent's policy/approval pipeline (target_keys → scope check,
  policy needs_approval, audit before/after) for free.

Required external CLIs (install once on your VM):

  # ProjectDiscovery toolchain (Go)
  go install -v github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest
  go install -v github.com/projectdiscovery/httpx/cmd/httpx@latest
  go install -v github.com/projectdiscovery/dnsx/cmd/dnsx@latest

  # nmap (apt)
  sudo apt-get install -y nmap

If a CLI is missing the tool returns a clear error telling the LLM what to
install (so the agent can `ask_user` rather than retry forever).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from typing import Any, Optional

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


# ---------------------------- common helpers ----------------------------

_INSTALL_HINT = {
    "subfinder": "go install -v github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest",
    "httpx":     "go install -v github.com/projectdiscovery/httpx/cmd/httpx@latest",
    "dnsx":      "go install -v github.com/projectdiscovery/dnsx/cmd/dnsx@latest",
    "nmap":      "sudo apt-get install -y nmap   # or: brew install nmap",
    "dig":       "sudo apt-get install -y dnsutils",
}

# Conservative argument whitelist for nmap — anything not in here is rejected.
_NMAP_SAFE_FLAGS = {
    "-sT", "-sS", "-sU",        # scan kinds (sS/sU need root)
    "-Pn",                       # treat as up
    "-n",                        # no DNS
    "--max-retries", "--max-rtt-timeout",
    "--host-timeout",
    "-oX", "-",                  # XML to stdout (we always force this)
}
_NMAP_TIMING_OK = {"T2", "T3", "T4"}     # T0/T1 too slow; T5 too aggressive


def _which_or_error(cli: str) -> Optional[str]:
    if shutil.which(cli):
        return None
    hint = _INSTALL_HINT.get(cli, "")
    return f"required CLI '{cli}' not found in PATH. Install: {hint}"


def _run(
    cmd: list[str],
    *,
    stdin: Optional[str] = None,
    timeout: int = 60,
) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _truncate(s: str, cap: int = 6000) -> str:
    if len(s) > cap:
        return s[:cap] + f"\n... [truncated, {len(s)-cap} more chars]"
    return s


# --------------------------- subdomain_enum -----------------------------

def _do_subdomain_enum(args: dict, ctx: dict) -> StepOutcome:
    domain = (args.get("domain") or "").strip()
    if not domain:
        return StepOutcome.error("subdomain_enum: domain is required")

    err = _which_or_error("subfinder")
    if err:
        return StepOutcome.error(err)

    timeout = int(args.get("timeout", 90))
    sources = args.get("sources") or []   # passed as -sources
    all_sources = bool(args.get("all_sources", False))

    cmd = ["subfinder", "-d", domain, "-silent", "-oJ"]
    if sources:
        cmd += ["-sources", ",".join(sources)]
    if all_sources:
        cmd += ["-all"]

    try:
        rc, out, err_out = _run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return StepOutcome.error(f"subdomain_enum: timed out after {timeout}s")
    except Exception as e:
        return StepOutcome.error(f"subdomain_enum: {e}")

    subs = []
    seen = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            host = obj.get("host") or obj.get("subdomain")
            src = obj.get("source") or obj.get("input")
            if host and host not in seen:
                seen.add(host)
                subs.append({"host": host, "source": src})
        except json.JSONDecodeError:
            # subfinder sometimes prints non-JSON on errors; skip
            continue

    return StepOutcome.cont(
        data={
            "domain": domain,
            "count": len(subs),
            "subdomains": subs[:500],   # hard cap to protect tokens
            "truncated": len(subs) > 500,
            "stderr": _truncate(err_out, 800) if rc != 0 else "",
            "exit_code": rc,
        },
        prompt=(
            f"subdomain_enum on {domain} found {len(subs)} subdomains; "
            f"decide next probe (e.g. http_probe on the live ones)"
        ),
    )


# ----------------------------- http_probe -------------------------------

def _do_http_probe(args: dict, ctx: dict) -> StepOutcome:
    targets = args.get("targets") or []
    if not targets:
        return StepOutcome.error("http_probe: targets is required (list of host/url)")

    err = _which_or_error("httpx")
    if err:
        return StepOutcome.error(err)

    timeout = int(args.get("timeout", 60))
    follow_redirects = bool(args.get("follow_redirects", True))
    ports = args.get("ports") or ""    # e.g. "80,443,8080,8443"

    cmd = [
        "httpx",
        "-silent", "-json",
        "-title", "-tech-detect", "-server", "-status-code", "-ip",
        "-timeout", "10",
        "-rl", str(max(int(ctx["scope"].rate_limit_per_second), 1)),  # rate-limit
    ]
    if follow_redirects:
        cmd.append("-follow-redirects")
    if ports:
        cmd += ["-ports", ports]

    stdin = "\n".join(targets)
    try:
        rc, out, err_out = _run(cmd, stdin=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        return StepOutcome.error(f"http_probe: timed out after {timeout}s")
    except Exception as e:
        return StepOutcome.error(f"http_probe: {e}")

    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append({
            "url":         obj.get("url"),
            "input":       obj.get("input"),
            "status":      obj.get("status_code") or obj.get("status-code"),
            "title":       obj.get("title"),
            "server":      obj.get("webserver") or obj.get("server"),
            "tech":        obj.get("tech") or obj.get("technologies") or [],
            "content_len": obj.get("content_length") or obj.get("content-length"),
            "ip":          obj.get("host") if "host" in obj else obj.get("a") or obj.get("ip"),
        })

    return StepOutcome.cont(
        data={
            "count": len(rows),
            "rows": rows[:300],
            "truncated": len(rows) > 300,
            "stderr": _truncate(err_out, 800) if rc != 0 else "",
            "exit_code": rc,
        },
        prompt=(
            f"http_probe: {len(rows)} live entries; "
            f"pick next target(s) for deeper inspection"
        ),
    )


# ----------------------------- port_scan --------------------------------

def _do_port_scan(args: dict, ctx: dict) -> StepOutcome:
    target = (args.get("target") or "").strip()
    if not target:
        return StepOutcome.error("port_scan: target is required (host or IP)")

    err = _which_or_error("nmap")
    if err:
        return StepOutcome.error(err)

    ports = (args.get("ports") or "top100").strip()
    timing = (args.get("timing") or "T3").strip()
    udp = bool(args.get("udp", False))
    timeout = int(args.get("timeout", 300))

    if timing not in _NMAP_TIMING_OK:
        return StepOutcome.error(f"port_scan: timing '{timing}' not allowed (use T2/T3/T4)")

    cmd = ["nmap", "-Pn", "-n", f"-{timing}"]
    if udp:
        cmd.append("-sU")
    else:
        cmd.append("-sT")    # connect scan, no root needed

    if ports == "top100":
        cmd += ["--top-ports", "100"]
    elif ports == "top1000":
        cmd += ["--top-ports", "1000"]
    elif re.fullmatch(r"[\d\-,]+", ports):
        cmd += ["-p", ports]
    else:
        return StepOutcome.error(
            f"port_scan: ports '{ports}' invalid; use 'top100' / 'top1000' / "
            f"comma-separated or ranges (e.g. '80,443,8000-8100')"
        )

    cmd += ["-oX", "-", target]

    try:
        rc, out, err_out = _run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return StepOutcome.error(f"port_scan: timed out after {timeout}s")
    except Exception as e:
        return StepOutcome.error(f"port_scan: {e}")

    open_ports = []
    try:
        root = ET.fromstring(out)
        for host in root.findall("host"):
            for ports_node in host.findall("ports"):
                for port_node in ports_node.findall("port"):
                    state = port_node.find("state")
                    if state is None or state.get("state") != "open":
                        continue
                    service = port_node.find("service")
                    open_ports.append({
                        "port":    int(port_node.get("portid")),
                        "proto":   port_node.get("protocol"),
                        "service": (service.get("name") if service is not None else None),
                        "product": (service.get("product") if service is not None else None),
                        "version": (service.get("version") if service is not None else None),
                    })
    except ET.ParseError as e:
        return StepOutcome.error(f"port_scan: failed to parse nmap XML: {e}")

    return StepOutcome.cont(
        data={
            "target":     target,
            "ports_arg":  ports,
            "open_count": len(open_ports),
            "open_ports": open_ports,
            "stderr":     _truncate(err_out, 800) if rc != 0 else "",
            "exit_code":  rc,
        },
        prompt=(
            f"port_scan {target}: {len(open_ports)} open ports; "
            f"decide if any need protocol-level follow-up"
        ),
    )


# ---------------------------- dns_resolve -------------------------------

def _do_dns_resolve(args: dict, ctx: dict) -> StepOutcome:
    hosts = args.get("hosts") or []
    if not hosts:
        return StepOutcome.error("dns_resolve: hosts is required (list)")

    record_types = [t.upper() for t in (args.get("record_types") or ["A"])]
    valid = {"A", "AAAA", "MX", "TXT", "CNAME", "NS", "SOA"}
    bad = [t for t in record_types if t not in valid]
    if bad:
        return StepOutcome.error(f"dns_resolve: unsupported record_types: {bad}")

    timeout = int(args.get("timeout", 30))

    err = _which_or_error("dnsx")
    if err:
        # fallback to dig if available
        if _which_or_error("dig") is None:
            return _dns_via_dig(hosts, record_types, timeout)
        return StepOutcome.error(err + "  (or install dig)")

    cmd = ["dnsx", "-silent", "-json", "-resp"]
    for t in record_types:
        cmd.append(f"-{t.lower()}")

    stdin = "\n".join(hosts)
    try:
        rc, out, err_out = _run(cmd, stdin=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        return StepOutcome.error(f"dns_resolve: timed out after {timeout}s")
    except Exception as e:
        return StepOutcome.error(f"dns_resolve: {e}")

    records = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        host = obj.get("host")
        for t in record_types:
            key = t.lower()
            vals = obj.get(key) or []
            if isinstance(vals, str):
                vals = [vals]
            for v in vals:
                records.append({"host": host, "type": t, "value": v})

    return StepOutcome.cont(
        data={
            "queried":      len(hosts),
            "record_count": len(records),
            "records":      records[:500],
            "truncated":    len(records) > 500,
            "stderr":       _truncate(err_out, 800) if rc != 0 else "",
            "exit_code":    rc,
        },
        prompt=f"dns_resolve: {len(records)} records across {len(hosts)} hosts",
    )


def _dns_via_dig(hosts: list[str], types: list[str], timeout: int) -> StepOutcome:
    """Fallback when dnsx isn't available."""
    records = []
    for h in hosts:
        for t in types:
            try:
                rc, out, _ = _run(["dig", "+short", h, t], timeout=timeout)
            except Exception:
                continue
            for line in out.splitlines():
                v = line.strip()
                if v:
                    records.append({"host": h, "type": t, "value": v})
    return StepOutcome.cont(
        data={
            "queried":      len(hosts),
            "record_count": len(records),
            "records":      records[:500],
            "truncated":    len(records) > 500,
            "tool":         "dig (fallback)",
        },
        prompt=f"dns_resolve via dig: {len(records)} records",
    )


# ------------------------------ register --------------------------------

def register(reg: ToolRegistry) -> None:
    reg.register(
        name="recon_subdomain_enum",
        description=(
            "Enumerate subdomains of a parent domain via subfinder. "
            "Use early in recon to discover attack surface. The parent domain "
            "must be in the engagement scope."
        ),
        parameters={
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "parent domain, e.g. example.com"},
                "timeout": {"type": "integer", "default": 90},
                "all_sources": {"type": "boolean", "default": False, "description": "use -all (slower, more sources)"},
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "specific subfinder sources, e.g. ['crtsh','virustotal']",
                },
            },
            "required": ["domain"],
        },
        fn=_do_subdomain_enum,
        target_keys=["domain"],
        operation="subdomain_enum",
        side_effects="network",
        category="recon",
    )

    reg.register(
        name="recon_http_probe",
        description=(
            "Probe a list of hosts/URLs via httpx — returns status, title, "
            "server, detected technologies, and resolved IP for each. Use "
            "after subdomain enum to find live web surfaces."
        ),
        parameters={
            "type": "object",
            "properties": {
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "hosts or URLs to probe",
                },
                "ports": {
                    "type": "string",
                    "description": "comma-separated ports, e.g. '80,443,8080'; default httpx behavior if omitted",
                },
                "follow_redirects": {"type": "boolean", "default": True},
                "timeout": {"type": "integer", "default": 60},
            },
            "required": ["targets"],
        },
        fn=_do_http_probe,
        target_keys=["targets"],
        operation="http_probe",
        side_effects="network",
        category="recon",
    )

    reg.register(
        name="recon_port_scan",
        description=(
            "TCP/UDP port scan via nmap with safe defaults: -Pn -sT, timing "
            "T2/T3/T4 only, no -A / no --script / no -O. Use 'top100' for "
            "quick recon or specific ports for targeted checks."
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "host or IP"},
                "ports": {
                    "type": "string",
                    "default": "top100",
                    "description": "'top100' / 'top1000' / comma-list / ranges (e.g. '80,443,8000-8100')",
                },
                "timing": {
                    "type": "string",
                    "enum": ["T2", "T3", "T4"],
                    "default": "T3",
                },
                "udp":     {"type": "boolean", "default": False},
                "timeout": {"type": "integer", "default": 300},
            },
            "required": ["target"],
        },
        fn=_do_port_scan,
        target_keys=["target"],
        operation="port_scan",
        side_effects="network",
        category="recon",
    )

    reg.register(
        name="recon_dns_resolve",
        description=(
            "Bulk DNS resolution via dnsx (or dig fallback). Supports A / "
            "AAAA / MX / TXT / CNAME / NS / SOA records."
        ),
        parameters={
            "type": "object",
            "properties": {
                "hosts": {"type": "array", "items": {"type": "string"}},
                "record_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["A", "AAAA", "MX", "TXT", "CNAME", "NS", "SOA"]},
                    "default": ["A"],
                },
                "timeout": {"type": "integer", "default": 30},
            },
            "required": ["hosts"],
        },
        fn=_do_dns_resolve,
        target_keys=["hosts"],
        operation="dns_resolve",
        side_effects="network",
        category="recon",
    )
