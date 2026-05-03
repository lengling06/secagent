"""Findings: structured vulnerability records.

Each finding is a markdown file under engagement_dir/findings/ with YAML frontmatter.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


_VALID_SEVERITY = {"info", "low", "medium", "high", "critical"}


def _next_id(findings_dir: Path) -> str:
    findings_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in findings_dir.glob("F-*.md"):
        m = re.match(r"F-(\d+)", p.stem)
        if m:
            n = max(n, int(m.group(1)))
    return f"F-{n+1:03d}"


def _do_add_finding(args: dict, ctx: dict) -> StepOutcome:
    eng_dir: Path = ctx["engagement_dir"]
    findings_dir = eng_dir / "findings"

    severity = (args.get("severity") or "info").lower()
    if severity not in _VALID_SEVERITY:
        return StepOutcome.error(f"severity must be one of {_VALID_SEVERITY}")

    fid = args.get("id") or _next_id(findings_dir)
    title = args.get("title") or "(untitled)"
    target = args.get("target") or ""
    category = args.get("category") or "misc"
    body = args.get("body") or ""

    # scope check on target (Handler also does this via target_keys)
    if target:
        scope = ctx["scope"]
        if not scope.is_in_scope(target):
            return StepOutcome.error(f"finding target '{target}' is out of scope")

    fm = {
        "id": fid,
        "severity": severity,
        "category": category,
        "status": args.get("status", "draft"),
        "target": target,
        "title": title,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }
    md = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body

    out = findings_dir / f"{fid}-{re.sub(r'[^a-z0-9]+', '-', title.lower())[:40]}.md"
    out.write_text(md, encoding="utf-8")

    return StepOutcome.cont(
        data={"id": fid, "path": str(out)},
        prompt=f"finding {fid} saved",
    )


def register(reg: ToolRegistry) -> None:
    reg.register(
        name="add_finding",
        description=(
            "Record a security finding (vulnerability / suspicious behavior) "
            "as a structured markdown file. Use for any confirmed or suspected issue."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "severity": {
                    "type": "string",
                    "enum": ["info", "low", "medium", "high", "critical"],
                },
                "category": {
                    "type": "string",
                    "description": "e.g. xss / sqli / ssrf / auth / info-leak / ...",
                },
                "target": {"type": "string", "description": "URL or host"},
                "status": {
                    "type": "string",
                    "enum": ["draft", "confirmed", "false_positive", "fixed"],
                },
                "body": {"type": "string", "description": "Markdown body: PoC, repro steps, impact, fix"},
                "id": {"type": "string", "description": "Optional explicit id"},
            },
            "required": ["title", "severity", "body"],
        },
        fn=_do_add_finding,
        target_keys=["target"],
        operation="add_finding",
        side_effects="write",
        category="findings",
    )
