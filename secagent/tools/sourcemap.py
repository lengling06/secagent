"""sourcemap_fetch — pull a JS bundle's sourcemap, sometimes the entire fight.

很多站点忘了关 sourcemap。一拉, 整个加密逻辑还原为原始 TS / 带变量名的源码 —
"逆向"瞬间变成"读源码"。

发现策略 (依次尝试):
1. 先 GET 这个 .js 文件, 扫文件末尾 ``//# sourceMappingURL=...`` 注释
2. fallback: 试 ``<url>.map`` / ``<url with .js -> .js.map>``
3. 如果是 inline data URL 形式, 直接 base64 解出来

下载到 ``engagement/js/<host>/maps/<filename>.map``。

Scope: 用 target_keys=['js_url'], handler 会做 in_scope 检查。
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from urllib.parse import urlparse, urljoin

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


_SM_COMMENT = re.compile(
    rb"//[#@]\s*sourceMappingURL=([^\s'\"]+)",
)


def _http_get(url: str, timeout: int = 30) -> tuple[int, bytes, dict]:
    """Tiny dependency-free GET. Uses httpx if available, else urllib."""
    try:
        import httpx
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.get(url)
            return r.status_code, r.content, dict(r.headers)
    except ImportError:
        from urllib.request import Request, urlopen
        req = Request(url, headers={"User-Agent": "SecAgent/sourcemap_fetch"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)


def _extract_sm_pointer(js_bytes: bytes) -> str | None:
    """Return the URL or data: URI from a //# sourceMappingURL comment, or None."""
    # check the last ~4096 bytes first (most common location)
    tail = js_bytes[-4096:] if len(js_bytes) > 4096 else js_bytes
    m = _SM_COMMENT.search(tail)
    if m:
        return m.group(1).decode("utf-8", errors="replace").strip()
    # fallback: scan the whole file
    m = _SM_COMMENT.search(js_bytes)
    if m:
        return m.group(1).decode("utf-8", errors="replace").strip()
    return None


def _save_sourcemap(content: bytes, source_url: str, engagement_dir: Path) -> Path:
    parsed = urlparse(source_url)
    host = parsed.hostname or "unknown"
    name = Path(parsed.path).name or "anonymous.js"
    if not name.endswith(".map"):
        name = name + ".map"
    out_dir = engagement_dir / "js" / host / "maps"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / name
    out.write_bytes(content)
    return out


def _summarize_sm(content: bytes) -> dict:
    try:
        sm = json.loads(content.decode("utf-8", errors="replace"))
    except Exception as e:
        return {"valid_json": False, "error": str(e), "size": len(content)}
    sources = sm.get("sources") or []
    has_content = bool(sm.get("sourcesContent"))
    return {
        "valid_json":      True,
        "version":         sm.get("version"),
        "file":            sm.get("file"),
        "source_count":    len(sources),
        "sources_preview": sources[:8],
        "has_sources_content": has_content,
        "size":            len(content),
    }


def _do_sourcemap_fetch(args, ctx):
    js_url = (args.get("js_url") or "").strip()
    if not js_url:
        return StepOutcome.error("sourcemap_fetch: js_url is required")

    eng: Path = ctx["engagement_dir"]
    timeout = int(args.get("timeout", 30))

    # Step 1: GET the js file
    try:
        status, body, headers = _http_get(js_url, timeout=timeout)
    except Exception as e:
        return StepOutcome.error(f"sourcemap_fetch: failed to GET js_url: {e}")
    if status >= 400:
        return StepOutcome.error(f"sourcemap_fetch: js_url returned {status}")

    attempts: list[dict] = []

    # Step 2: extract //# sourceMappingURL pointer
    pointer = _extract_sm_pointer(body)
    if pointer:
        # 2a: inline data URL?
        if pointer.startswith("data:"):
            # data:application/json;base64,<...>
            try:
                _, _, payload = pointer.partition(",")
                if ";base64" in pointer.split(",")[0]:
                    sm_bytes = base64.b64decode(payload)
                else:
                    from urllib.parse import unquote
                    sm_bytes = unquote(payload).encode("utf-8")
                saved = _save_sourcemap(sm_bytes, js_url, eng)
                attempts.append({"strategy": "inline_data_url", "ok": True, "path": str(saved)})
                return StepOutcome.cont(
                    data={
                        "js_url":   js_url,
                        "saved_to": str(saved.relative_to(eng) if saved.is_relative_to(eng) else saved),
                        "summary":  _summarize_sm(sm_bytes),
                        "attempts": attempts,
                    },
                    prompt=(
                        f"sourcemap (inline) saved -> {saved.name}. "
                        "如果 has_sources_content=true, 直接 file_read 该 .map 拿原始 source。"
                    ),
                )
            except Exception as e:
                attempts.append({"strategy": "inline_data_url", "ok": False, "error": str(e)})
        else:
            # 2b: relative or absolute URL, resolve against js_url
            sm_url = urljoin(js_url, pointer)
            try:
                s, sm_bytes, _ = _http_get(sm_url, timeout=timeout)
                if s < 400:
                    saved = _save_sourcemap(sm_bytes, sm_url, eng)
                    attempts.append({"strategy": "comment_pointer", "url": sm_url, "ok": True, "path": str(saved)})
                    return StepOutcome.cont(
                        data={
                            "js_url":   js_url,
                            "sm_url":   sm_url,
                            "saved_to": str(saved.relative_to(eng) if saved.is_relative_to(eng) else saved),
                            "summary":  _summarize_sm(sm_bytes),
                            "attempts": attempts,
                        },
                        prompt=(
                            f"sourcemap saved -> {saved.name}. "
                            f"sources={len(_summarize_sm(sm_bytes).get('sources_preview') or [])} preview。"
                            " 如果 has_sources_content=true 整个原始源码就在里面。"
                        ),
                    )
                else:
                    attempts.append({"strategy": "comment_pointer", "url": sm_url, "ok": False, "status": s})
            except Exception as e:
                attempts.append({"strategy": "comment_pointer", "url": sm_url, "ok": False, "error": str(e)})

    # Step 3: fallback — try common conventions
    candidates = []
    if js_url.endswith(".js"):
        candidates.append(js_url + ".map")          # foo.js.map
        candidates.append(js_url[:-3] + ".js.map")  # idempotent if same as above
    candidates.append(js_url + ".map")
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    for candidate in candidates:
        try:
            s, sm_bytes, _ = _http_get(candidate, timeout=timeout)
            if s < 400 and sm_bytes:
                # quick sanity: does it parse?
                try:
                    json.loads(sm_bytes.decode("utf-8", errors="replace"))
                except Exception:
                    attempts.append({"strategy": "guess", "url": candidate, "ok": False, "reason": "not json"})
                    continue
                saved = _save_sourcemap(sm_bytes, candidate, eng)
                attempts.append({"strategy": "guess", "url": candidate, "ok": True, "path": str(saved)})
                return StepOutcome.cont(
                    data={
                        "js_url":   js_url,
                        "sm_url":   candidate,
                        "saved_to": str(saved.relative_to(eng) if saved.is_relative_to(eng) else saved),
                        "summary":  _summarize_sm(sm_bytes),
                        "attempts": attempts,
                    },
                    prompt=f"sourcemap (guessed url) saved -> {saved.name}",
                )
            else:
                attempts.append({"strategy": "guess", "url": candidate, "ok": False, "status": s})
        except Exception as e:
            attempts.append({"strategy": "guess", "url": candidate, "ok": False, "error": str(e)})

    return StepOutcome.cont(
        data={
            "js_url":   js_url,
            "saved_to": None,
            "attempts": attempts,
            "note":     "no sourcemap found via comment or common naming. "
                        "It may simply not exist (站点关掉了 sourcemap)。"
                        "Try sister hosts (staging.* / dev.* / canary.*) — they often forget.",
        },
        prompt="sourcemap not found. 试试同站的 staging/dev 子域 (它们常忘关), 或者直接 js_beautify 硬上。",
    )


def register(reg: ToolRegistry) -> None:
    reg.register(
        name="sourcemap_fetch",
        description=(
            "拉取一个 JS bundle 的 sourcemap (.js.map)。"
            "策略: 先扫文件末尾 //# sourceMappingURL= 指针 (含 inline data: URL), "
            "失败时试常见命名 (foo.js.map)。\n"
            "成功一次 = 整个加密逻辑可能直接还原成原始 TS 源码 (如果 sourcesContent 存在)。"
            "失败也别灰心: 试 staging/dev/canary 子域同名 bundle, 它们经常忘关。\n"
            "保存到 engagement/js/<host>/maps/<filename>.map。"
            "js_url 必须在 scope 内。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "js_url": {
                    "type": "string",
                    "description": "JS bundle 的完整 URL, 例如 https://example.com/static/app.min.js",
                },
                "timeout": {
                    "type": "integer",
                    "default": 30,
                },
            },
            "required": ["js_url"],
        },
        fn=_do_sourcemap_fetch,
        target_keys=["js_url"],
        operation="sourcemap_fetch",
        side_effects="network",
        category="js_reverse",
    )
