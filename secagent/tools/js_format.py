"""js_beautify — pretty-print minified JS bundles.

逆向第一步永远是它。webpack/vite/rollup 的产物压缩到一行几 MB, 必须先美化才能
读、grep、定位函数。

实现:
- 用 jsbeautifier (Python) — 不依赖 node, 跨平台
- 输入: engagement 内的 JS 文件路径
- 输出: 默认写到同目录 ``<name>.beauty.js``; 也可指定 output_path
- scope check: input/output 必须在 engagement_dir 内 (走 filesystem 那一套)
"""
from __future__ import annotations

from pathlib import Path

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


def _resolve_in_engagement(p: str, engagement_dir: Path) -> Path | None:
    """Resolve a path; if relative, anchor at engagement_dir. Reject if it
    escapes engagement_dir."""
    path = Path(p)
    if not path.is_absolute():
        path = engagement_dir / path
    try:
        path = path.resolve()
        engagement_dir.resolve().relative_to(engagement_dir.resolve())
        path.relative_to(engagement_dir.resolve())
    except ValueError:
        return None
    return path


def _do_js_beautify(args, ctx):
    src = (args.get("path") or "").strip()
    if not src:
        return StepOutcome.error("js_beautify: path is required")

    eng = ctx["engagement_dir"]
    src_path = _resolve_in_engagement(src, eng)
    if src_path is None:
        return StepOutcome.error(f"js_beautify: path '{src}' is outside engagement_dir, rejected")
    if not src_path.exists():
        return StepOutcome.error(f"js_beautify: file not found: {src_path}")
    if not src_path.is_file():
        return StepOutcome.error(f"js_beautify: not a file: {src_path}")

    try:
        import jsbeautifier
    except ImportError:
        return StepOutcome.error(
            "js_beautify: jsbeautifier not installed. Run: pip install jsbeautifier"
        )

    out_arg = (args.get("output_path") or "").strip()
    if out_arg:
        dst_path = _resolve_in_engagement(out_arg, eng)
        if dst_path is None:
            return StepOutcome.error(f"js_beautify: output_path '{out_arg}' is outside engagement_dir")
    else:
        # 默认: <stem>.beauty.js 同目录
        if src_path.suffix == ".js":
            dst_path = src_path.with_suffix(".beauty.js")
        else:
            dst_path = src_path.with_name(src_path.name + ".beauty.js")

    try:
        raw = src_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return StepOutcome.error(f"js_beautify: read failed: {e}")

    opts = jsbeautifier.default_options()
    opts.indent_size = int(args.get("indent_size", 2))
    opts.preserve_newlines = True
    opts.max_preserve_newlines = 2
    opts.keep_array_indentation = False
    opts.brace_style = "collapse"
    opts.space_after_anon_function = True
    opts.space_in_empty_paren = False

    try:
        beautified = jsbeautifier.beautify(raw, opts)
    except Exception as e:
        return StepOutcome.error(f"js_beautify: beautifier raised: {e}")

    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.write_text(beautified, encoding="utf-8")
    except Exception as e:
        return StepOutcome.error(f"js_beautify: write failed: {e}")

    in_size = len(raw)
    out_size = len(beautified)
    in_lines = raw.count("\n") + 1
    out_lines = beautified.count("\n") + 1
    return StepOutcome.cont(
        data={
            "input":      str(src_path.relative_to(eng) if src_path.is_relative_to(eng) else src_path),
            "output":     str(dst_path.relative_to(eng) if dst_path.is_relative_to(eng) else dst_path),
            "in_size":    in_size,
            "out_size":   out_size,
            "in_lines":   in_lines,
            "out_lines":  out_lines,
            "expansion":  f"{out_size/max(in_size,1):.1f}x",
        },
        prompt=(
            f"beautified -> {dst_path.name} ({in_lines} -> {out_lines} lines). "
            f"用 file_read 看具体行范围, 或 grep 找符号 (sign/encrypt/hmac/aes/sha/base64)。"
        ),
    )


def register(reg: ToolRegistry) -> None:
    reg.register(
        name="js_beautify",
        description=(
            "Pretty-print minified JS to readable form (jsbeautifier)。"
            "逆向第一步: 几 MB 单行的 webpack bundle 美化成可读多行形式, 之后才能 grep / "
            "ast 搜函数 / 数行号。\n"
            "输入文件必须在当前 engagement 目录内。默认输出到 <name>.beauty.js 同目录。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "engagement-relative 或绝对路径, 待美化的 .js 文件",
                },
                "output_path": {
                    "type": "string",
                    "description": "可选, 输出路径; 默认 <stem>.beauty.js",
                },
                "indent_size": {
                    "type": "integer",
                    "default": 2,
                },
            },
            "required": ["path"],
        },
        fn=_do_js_beautify,
        operation="js_beautify",
        side_effects="write",
        category="js_reverse",
    )
