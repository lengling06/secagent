"""Scope: the soul of SecAgent.

Loaded from engagements/<name>/scope.yaml. Used by Handler to gate every
tool call.

Scope match rules:
- domains support wildcard "*.example.com"
- ips support CIDR ("203.0.113.0/24")
- urls match by host (parse with urllib.parse) then domain rule

A target is in scope iff:
1. it matches at least one in_scope rule, AND
2. it does NOT match any out_of_scope rule
"""
from __future__ import annotations

import fnmatch
import ipaddress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yaml


@dataclass
class Scope:
    engagement: str = ""
    authorized_by: str = ""
    authorized_at: str = ""
    expires_at: str = ""

    in_scope_domains: list[str] = field(default_factory=list)
    in_scope_ips: list[str] = field(default_factory=list)         # CIDR or single
    in_scope_apis: list[str] = field(default_factory=list)
    out_of_scope_domains: list[str] = field(default_factory=list)
    out_of_scope_ips: list[str] = field(default_factory=list)

    allowed_operations: set[str] = field(default_factory=set)
    forbidden_operations: set[str] = field(default_factory=set)
    require_approval: set[str] = field(default_factory=set)

    proxy: Optional[str] = None
    user_agent_tag: Optional[str] = None
    rate_limit_per_second: int = 5

    raw: dict = field(default_factory=dict)

    # ---------- predicates ----------

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at).replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                exp = datetime.strptime(self.expires_at, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except Exception:
                return False
        return datetime.now(timezone.utc) > exp

    def is_in_scope(self, target: str) -> bool:
        if not target:
            return False
        if self.is_expired():
            return False  # expired = nothing in scope

        host_or_ip = self._extract_host(target)
        if host_or_ip is None:
            return False

        # 1. out_of_scope wins
        if self._match_domain(host_or_ip, self.out_of_scope_domains):
            return False
        if self._match_ip(host_or_ip, self.out_of_scope_ips):
            return False

        # 2. in_scope rules
        if self._match_domain(host_or_ip, self.in_scope_domains):
            return True
        if self._match_ip(host_or_ip, self.in_scope_ips):
            return True
        # urls: full match by prefix (cheap)
        if any(target.startswith(api.rstrip("*")) for api in self.in_scope_apis):
            return True

        return False

    def operation_allowed(self, op: str) -> bool:
        if op in self.forbidden_operations:
            return False
        if self.allowed_operations and op not in self.allowed_operations:
            return False
        return True

    def requires_approval(self, op: str) -> bool:
        return op in self.require_approval

    # ---------- helpers ----------

    @staticmethod
    def _extract_host(target: str) -> Optional[str]:
        if "://" in target:
            return urlparse(target).hostname
        # plain host or IP
        return target.strip().split("/")[0]

    @staticmethod
    def _match_domain(host: str, patterns: list[str]) -> bool:
        for p in patterns:
            if fnmatch.fnmatch(host, p):
                return True
        return False

    @staticmethod
    def _match_ip(host: str, patterns: list[str]) -> bool:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        for p in patterns:
            try:
                net = ipaddress.ip_network(p, strict=False)
                if ip in net:
                    return True
            except ValueError:
                pass
        return False


def load_scope(engagement_dir: Path) -> Scope:
    path = engagement_dir / "scope.yaml"
    if not path.exists():
        raise FileNotFoundError(f"scope.yaml not found in {engagement_dir}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    in_scope = raw.get("in_scope") or {}
    out_of_scope = raw.get("out_of_scope") or {}
    network = raw.get("network") or {}

    return Scope(
        engagement=raw.get("engagement", engagement_dir.name),
        authorized_by=raw.get("authorized_by", ""),
        authorized_at=raw.get("authorized_at", ""),
        expires_at=raw.get("expires_at", ""),
        in_scope_domains=in_scope.get("domains") or [],
        in_scope_ips=in_scope.get("ips") or [],
        in_scope_apis=in_scope.get("apis") or [],
        out_of_scope_domains=out_of_scope.get("domains") or [],
        out_of_scope_ips=out_of_scope.get("ips") or [],
        allowed_operations=set(raw.get("allowed_operations") or []),
        forbidden_operations=set(raw.get("forbidden_operations") or []),
        require_approval=set(raw.get("require_approval") or []),
        proxy=network.get("proxy"),
        user_agent_tag=network.get("user_agent_tag"),
        rate_limit_per_second=int(network.get("rate_limit_per_second") or 5),
        raw=raw,
    )


def summarize_scope(scope: Scope) -> str:
    lines = [
        f"Engagement: {scope.engagement}",
        f"Authorized by: {scope.authorized_by}",
        f"Authorized at: {scope.authorized_at}",
        f"Expires at:    {scope.expires_at}  {'[EXPIRED]' if scope.is_expired() else ''}",
        "",
        "In-scope domains:",
    ] + [f"  - {d}" for d in scope.in_scope_domains] + [
        "In-scope IPs/CIDRs:",
    ] + [f"  - {i}" for i in scope.in_scope_ips] + [
        "Out-of-scope:",
    ] + [f"  - {d}" for d in scope.out_of_scope_domains + scope.out_of_scope_ips] + [
        "",
        f"Allowed ops:    {sorted(scope.allowed_operations)}",
        f"Forbidden ops:  {sorted(scope.forbidden_operations)}",
        f"Require approval: {sorted(scope.require_approval)}",
        "",
        f"Proxy: {scope.proxy or '(none)'}",
        f"Rate limit: {scope.rate_limit_per_second} req/s",
    ]
    return "\n".join(lines)
