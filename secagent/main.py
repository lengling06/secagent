"""CLI entry point.

User-facing flow:
    secagent init              # one-shot wizard: pick LLM, save config, test
    secagent                   # chat in the default engagement
    secagent target <url>      # spawn an engagement scoped to <url>'s host, chat
    secagent chat [-e <name>]  # chat in a specific user engagement

Power-user / dev flow:
    secagent repl  -e <name>   # like chat but resolves <name> in repo engagements/
    secagent audit -e <name>   # tail audit.jsonl
    secagent scope -e <name>   # print scope summary
    secagent llm               # show resolved llm.yaml

Engagement name resolution (for chat / audit / scope):
    1. If --engagement looks like a path (contains / or \\, or is absolute), use as-is.
    2. Else try ~/.secagent/engagements/<name>.
    3. Else fall back to <repo>/engagements/<name> (legacy).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional


def _resolve_engagement(name: Optional[str]) -> Optional[Path]:
    """Resolve --engagement / target arg to an absolute path. None means: use default."""
    if not name:
        return None
    p = Path(name)
    # path-like?
    if p.is_absolute() or "/" in name or "\\" in name:
        return p.resolve()
    # try ~/.secagent first
    from secagent.cli.bootstrap import user_engagements_dir
    user_p = user_engagements_dir() / name
    if user_p.exists():
        return user_p
    # fall back to repo engagements/
    repo_p = Path(__file__).resolve().parent.parent / "engagements" / name
    if repo_p.exists():
        return repo_p
    # default: assume user wanted to create-or-use under ~/.secagent
    return user_p


def cli() -> int:
    parser = argparse.ArgumentParser(prog="secagent", description="Security-domain agent")
    sub = parser.add_subparsers(dest="cmd")
    default_max_turns = 40

    # `secagent init`
    sub.add_parser("init", help="One-shot setup wizard (LLM config + default engagement)")

    # `secagent` (no subcommand) and `secagent chat` both go here
    p_chat = sub.add_parser("chat", help="Chat with the agent (default engagement unless -e given)")
    p_chat.add_argument("--engagement", "-e", default=None,
                        help="Engagement name (under ~/.secagent/engagements/) or path. Default: 'default'.")
    p_chat.add_argument("--llm", "-l", default=None,
                        help="Backend name in llm.yaml; omit for default_backend / mixin.")
    p_chat.add_argument("--max-turns", type=int, default=default_max_turns)

    # `secagent target <url>`
    p_target = sub.add_parser("target", help="Spawn an engagement scoped to a URL, then chat")
    p_target.add_argument("url", help="https://… of the target")
    p_target.add_argument("--authorized-by", default=None,
                          help="Free-text. Default: 'self (local analysis)'.")
    p_target.add_argument("--llm", "-l", default=None)
    p_target.add_argument("--max-turns", type=int, default=default_max_turns)

    # power-user / dev (repo-relative engagements/)
    p_repl = sub.add_parser("repl", help="(dev) chat with engagement under <repo>/engagements/")
    p_repl.add_argument("--engagement", "-e", required=True)
    p_repl.add_argument("--llm", "-l", default=None)
    p_repl.add_argument("--max-turns", type=int, default=default_max_turns)

    p_audit = sub.add_parser("audit", help="Tail audit log of an engagement")
    p_audit.add_argument("--engagement", "-e", default=None)

    p_scope = sub.add_parser("scope", help="Show scope summary")
    p_scope.add_argument("--engagement", "-e", default=None)

    p_llm = sub.add_parser("llm", help="Show resolved LLM config")
    p_llm.add_argument("--engagement", "-e", default=None)

    # `secagent doctor` — diagnose + migrate config drift (mcp.json mainly)
    p_doc = sub.add_parser("doctor", help="检查 ~/.secagent 下所有配置, 报告/修复过时项 (mcp.json)")
    p_doc.add_argument("--fix", action="store_true",
                       help="不仅报告, 自动应用迁移 (会备份原文件到 *.bak)")

    args = parser.parse_args()

    # Default subcommand: chat
    if args.cmd is None:
        # rebuild Namespace to look like chat with defaults
        args = parser.parse_args(["chat"])

    # ============================================================
    # init
    # ============================================================
    if args.cmd == "init":
        from secagent.cli.init_wizard import run_init
        return run_init()

    # ============================================================
    # chat (the new default flow)
    # ============================================================
    if args.cmd == "chat":
        from secagent.cli.bootstrap import (
            ensure_default_engagement,
            user_llm_config_path,
        )
        if not user_llm_config_path().exists() and args.engagement is None:
            print("没找到 ~/.secagent/llm.yaml。先跑 `secagent init` 设置 LLM。", file=sys.stderr)
            return 1

        if args.engagement:
            eng_dir = _resolve_engagement(args.engagement)
            if eng_dir is None or not eng_dir.exists():
                print(f"[ERROR] engagement not found: {args.engagement}", file=sys.stderr)
                print(f"        looked at: {eng_dir}", file=sys.stderr)
                return 1
        else:
            eng_dir = ensure_default_engagement()

        from secagent.frontends.repl import run_repl
        return run_repl(eng_dir, llm_name=args.llm, max_turns=args.max_turns)

    # ============================================================
    # target <url>
    # ============================================================
    if args.cmd == "target":
        from secagent.cli.bootstrap import (
            create_engagement_from_spec,
            suggest_engagement_for_url,
            user_llm_config_path,
        )
        if not user_llm_config_path().exists():
            print("没找到 ~/.secagent/llm.yaml。先跑 `secagent init` 设置 LLM。", file=sys.stderr)
            return 1
        try:
            spec = suggest_engagement_for_url(args.url)
        except ValueError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            return 1
        print()
        print(f"目标 URL: {args.url}")
        print(f"建议 engagement:")
        print(f"  名称:   {spec['name']}")
        print(f"  路径:   {spec['path']}")
        print(f"  scope:  {', '.join(spec['domains'])}")
        print(f"  授权方: {args.authorized_by or 'self (local analysis)'}")
        print()
        try:
            ans = input("确认创建? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 130
        if ans and ans not in ("y", "yes"):
            print("取消。")
            return 0
        eng_dir = create_engagement_from_spec(
            spec,
            authorized_by=args.authorized_by or "self (local analysis)",
        )
        print(f"已创建: {eng_dir}")
        # seed the first user message so the agent kicks off automatically
        from secagent.frontends.repl import run_repl
        return run_repl(
            eng_dir,
            llm_name=args.llm,
            max_turns=args.max_turns,
            initial_input=(
                f"目标: {args.url}\n"
                f"开始 JS 逆向。先做最小探测 (主页可达性 + 列出加载的 JS 资源 + "
                f"猜测哪个 bundle 负责签名/加密)，给出初步判断后等我确认再继续深入。"
            ),
        )

    # ============================================================
    # repl (legacy / dev mode — repo-relative engagements/)
    # ============================================================
    if args.cmd == "repl":
        repo_root = Path(__file__).resolve().parent.parent
        eng_dir = repo_root / "engagements" / args.engagement
        if not eng_dir.exists():
            print(f"[ERROR] engagement directory not found: {eng_dir}", file=sys.stderr)
            return 1
        from secagent.frontends.repl import run_repl
        return run_repl(eng_dir, llm_name=args.llm, max_turns=args.max_turns)

    # ============================================================
    # audit / scope / llm — accept either user or repo path
    # ============================================================
    if args.cmd in ("audit", "scope", "llm"):
        if args.engagement:
            eng_dir = _resolve_engagement(args.engagement)
        else:
            from secagent.cli.bootstrap import ensure_default_engagement
            eng_dir = ensure_default_engagement()
        if eng_dir is None or not eng_dir.exists():
            print(f"[ERROR] engagement not found: {eng_dir}", file=sys.stderr)
            return 1

        if args.cmd == "audit":
            log = eng_dir / "audit.jsonl"
            if not log.exists():
                print("(no audit log yet)")
                return 0
            print(log.read_text(encoding="utf-8"))
            return 0
        if args.cmd == "scope":
            from secagent.tools.scope import load_scope, summarize_scope
            scope = load_scope(eng_dir)
            print(summarize_scope(scope))
            return 0
        if args.cmd == "llm":
            from secagent.llm.config import find_llm_config, load_llm_config
            path = find_llm_config(eng_dir)
            print(f"resolved llm.yaml: {path}")
            cfg = load_llm_config(eng_dir)
            backends = cfg.get("backends") or {}
            mixin = cfg.get("mixin")
            default = cfg.get("default_backend")
            print(f"default_backend: {default}")
            if mixin:
                print(f"mixin primary:   {mixin.get('primary')}")
                print(f"mixin fallback:  {mixin.get('fallback_order')}")
            print("backends:")
            for name, b in backends.items():
                print(f"  - {name}: type={b.get('type')} model={b.get('model')} base_url={b.get('base_url','-')}")
            return 0

    # ============================================================
    # doctor — scan all engagements for config drift
    # ============================================================
    if args.cmd == "doctor":
        from secagent.cli.bootstrap import (
            apply_mcp_migrations,
            diagnose_mcp_config,
            user_engagements_dir,
        )
        engs = sorted(user_engagements_dir().glob("*"))
        engs = [e for e in engs if e.is_dir()]
        if not engs:
            print("(no engagements yet under ~/.secagent/engagements/)")
            return 0

        any_issue = False
        for eng in engs:
            mcp_path = eng / "mcp.json"
            diag = diagnose_mcp_config(mcp_path)
            if diag is None:
                print(f"  [ok]  {eng.name}: {mcp_path.name} clean")
                continue
            any_issue = True
            print(f"  [!!]  {eng.name}: {len(diag['issues'])} issues")
            for i in diag["issues"]:
                print(f"          - {i}")
            if args.fix:
                changed = apply_mcp_migrations(mcp_path, diag["migrations"])
                if changed:
                    print(f"          ✓ migrated (backup: {mcp_path}.bak)")

        if any_issue and not args.fix:
            print()
            print("加 --fix 自动修复: secagent doctor --fix")
        return 0

    return 0
    sys.exit(cli())
