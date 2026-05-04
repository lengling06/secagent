"""LLM config loader + session factory.

Reads `llm.yaml` from (in order):
  1. <engagement_dir>/llm.yaml         — per-engagement override
  2. ~/.secagent/llm.yaml              — user global
  3. <repo>/secagent/llm.example.yaml  — last-resort default

Schema (see llm.example.yaml for the canonical example):

    default_backend: claude_main
    backends:
      claude_main:
        type: anthropic                 # or "openai_compat"
        model: claude-sonnet-4-5-20250929
        api_key_env: ANTHROPIC_API_KEY
      deepseek:
        type: openai_compat
        model: deepseek-chat
        base_url: https://api.deepseek.com/v1
        api_key_env: DEEPSEEK_API_KEY
        default_headers: {}
        extra_body: {}
      proxy_pool:                       # 你的中转站
        type: openai_compat
        model: claude-sonnet-4-5
        base_url: https://your-proxy.example.com/v1
        api_key_env: PROXY_API_KEY
        default_headers:
          X-User-Id: secagent

    mixin:
      primary: claude_main
      fallback_order: [proxy_pool, deepseek]
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

from secagent.llm.base import LLMSession


def find_llm_config(engagement_dir: Path) -> Optional[Path]:
    candidates = [
        engagement_dir / "llm.yaml",
        Path.home() / ".secagent" / "llm.yaml",
        Path(__file__).resolve().parent.parent / "llm.example.yaml",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def load_llm_config(engagement_dir: Path) -> dict:
    p = find_llm_config(engagement_dir)
    if p is None:
        raise FileNotFoundError(
            "No llm.yaml found. Create one at "
            f"{engagement_dir/'llm.yaml'} or ~/.secagent/llm.yaml."
        )
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def build_session(cfg: dict, backend_name: Optional[str] = None) -> LLMSession:
    """Build an LLMSession.

    - If `backend_name` given, build a single-backend session of that type.
    - Else, if `cfg['mixin']` present, build a MixinSession with all referenced
      backends.
    - Else, build the `default_backend`.
    """
    backends_cfg = cfg.get("backends") or {}
    if not backends_cfg:
        raise ValueError("llm.yaml: no backends configured")

    if backend_name:
        if backend_name not in backends_cfg:
            raise KeyError(f"backend '{backend_name}' not in llm.yaml backends")
        return _build_single(backend_name, backends_cfg[backend_name])

    mixin_cfg = cfg.get("mixin")
    if mixin_cfg:
        primary = mixin_cfg["primary"]
        fallback = mixin_cfg.get("fallback_order") or []
        # build primary + every fallback
        names = [primary] + [n for n in fallback if n != primary]
        backends: dict[str, LLMSession] = {}
        for n in names:
            if n not in backends_cfg:
                raise KeyError(f"mixin references unknown backend '{n}'")
            backends[n] = _build_single(n, backends_cfg[n])
        from secagent.llm.mixin import MixinSession
        return MixinSession(backends=backends, primary=primary, fallback_order=fallback)

    # no mixin: use default_backend
    default = cfg.get("default_backend")
    if not default:
        # take the first one
        default = next(iter(backends_cfg.keys()))
    return _build_single(default, backends_cfg[default])


def _build_single(name: str, bcfg: dict) -> LLMSession:
    btype = bcfg.get("type", "openai_compat")
    api_key = _resolve_api_key(bcfg)

    if btype == "anthropic":
        from secagent.llm.anthropic_session import AnthropicSession
        sess = AnthropicSession(
            model=bcfg["model"],
            api_key=api_key,
            max_tokens=int(bcfg.get("max_tokens", 8192)),
        )
    elif btype in ("openai", "openai_compat"):
        from secagent.llm.openai_session import OpenAICompatSession
        sess = OpenAICompatSession(
            model=bcfg["model"],
            api_key=api_key,
            base_url=bcfg.get("base_url", "https://api.openai.com/v1"),
            default_headers=bcfg.get("default_headers") or {},
            extra_body=bcfg.get("extra_body") or {},
            max_tokens=int(bcfg.get("max_tokens", 8192)),
            temperature=float(bcfg.get("temperature", 0.2)),
            timeout=float(bcfg.get("timeout", 120)),
            max_retries=int(bcfg.get("max_retries", 2)),
            name=name,
        )
    else:
        raise ValueError(f"unknown backend type: {btype}")

    # Per-backend override of context window (used by 70%/78% ratio thresholds).
    # 中转站经常偷偷截断比模型官方窗口小, 在 llm.yaml 里用 context_window 设保守值。
    if "context_window" in bcfg and bcfg["context_window"]:
        sess.context_window = int(bcfg["context_window"])
    return sess


def _resolve_api_key(bcfg: dict) -> str:
    if "api_key" in bcfg and bcfg["api_key"]:
        return str(bcfg["api_key"])
    env = bcfg.get("api_key_env")
    if env:
        v = os.environ.get(env, "")
        if not v:
            raise EnvironmentError(f"env var '{env}' is not set")
        return v
    raise ValueError("backend missing both 'api_key' and 'api_key_env'")
