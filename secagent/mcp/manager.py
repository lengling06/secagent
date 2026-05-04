"""MCP integration: read engagement-level mcp.json, spawn stdio subprocesses,
expose every MCP tool as a regular ToolRegistry entry.

Each MCP tool becomes a registry tool named `<server>__<tool>`, e.g.:
    js-reverse-mcp's "evaluate_script" → registry tool "js_reverse__evaluate_script"

Scope checks for MCP tools: by default we do NOT auto-derive targets (MCP
tools have free-form schemas), so each MCP server should declare which arg
keys are targets via mcp.json. Example:

    {
      "mcpServers": {
        "js-reverse": {
          "command": "npx",
          "args": ["js-reverse-mcp"],
          "target_keys": {
            "navigate_page": ["url"],
            "new_page":      ["url"]
          },
          "approval_required": ["evaluate_script", "inject_before_load"]
        }
      }
    }

This file gives a minimal viable implementation; productionize as needed.
"""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional

from secagent.core.outcome import StepOutcome
from secagent.tools.registry import ToolRegistry


class MCPManager:
    """Owns a background asyncio loop that talks to MCP server subprocesses."""

    def __init__(self, engagement_dir: Path, registry: ToolRegistry):
        self.engagement_dir = engagement_dir
        self.registry = registry
        self.config_path = engagement_dir / "mcp.json"
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._exit_stack: Optional[AsyncExitStack] = None
        self._sessions: dict[str, Any] = {}      # server_name -> ClientSession
        self._tools_meta: dict[str, dict] = {}   # server_name -> {target_keys, approval_required}

    # ---------- public sync API ----------

    def start(self) -> None:
        if not self.config_path.exists():
            return  # no MCPs configured; that's fine
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        fut = asyncio.run_coroutine_threadsafe(self._connect_all(), self._loop)
        fut.result(timeout=30)

    def stop(self) -> None:
        if not self._loop:
            return
        if self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
            try:
                fut.result(timeout=10)
            except Exception as e:
                print(f"[MCP] shutdown warning: {e}")
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        if not self._loop.is_closed():
            self._loop.close()
        self._loop = None
        self._thread = None

    # ---------- async internals ----------

    async def _connect_all(self) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            print("[MCP] `mcp` package not installed; skipping MCP integration.")
            return

        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self._exit_stack = AsyncExitStack()
        for name, scfg in (cfg.get("mcpServers") or {}).items():
            try:
                params = StdioServerParameters(
                    command=scfg["command"],
                    args=scfg.get("args", []),
                    env=scfg.get("env"),
                )
                read, write = await self._exit_stack.enter_async_context(stdio_client(params))
                session = await self._exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session
                self._tools_meta[name] = {
                    "target_keys": scfg.get("target_keys", {}),
                    "approval_required": set(scfg.get("approval_required") or []),
                }
                print(f"[MCP] connected: {name}")

                # discover tools
                tools = await session.list_tools()
                for t in tools.tools:
                    self._register_mcp_tool(name, t)
            except Exception as e:
                print(f"[MCP] failed to connect {name}: {e}")

    async def _shutdown(self) -> None:
        self._sessions.clear()
        self._tools_meta.clear()
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None

    def _register_mcp_tool(self, server_name: str, tool: Any) -> None:
        registry_name = f"{server_name.replace('-', '_')}__{tool.name}"
        meta_for_server = self._tools_meta[server_name]
        target_keys = meta_for_server["target_keys"].get(tool.name, [])
        needs_approval = tool.name in meta_for_server["approval_required"]

        operation = registry_name if not needs_approval else f"{registry_name}__approval"

        def fn(args: dict, ctx: dict) -> StepOutcome:
            return self._call_mcp(server_name, tool.name, args)

        # tool.inputSchema may be a dict already (mcp lib gives JSON schema)
        params = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}

        self.registry.register(
            name=registry_name,
            description=f"[{server_name}] {tool.description or tool.name}",
            parameters=params,
            fn=fn,
            target_keys=target_keys,
            operation=operation,
            side_effects="exec",
            category=f"mcp:{server_name}",
        )

    def _call_mcp(self, server_name: str, tool_name: str, args: dict) -> StepOutcome:
        if not self._loop:
            return StepOutcome.error("MCP loop not running")
        session = self._sessions.get(server_name)
        if not session:
            return StepOutcome.error(f"MCP server not connected: {server_name}")
        fut = asyncio.run_coroutine_threadsafe(
            session.call_tool(tool_name, args),
            self._loop,
        )
        try:
            result = fut.result(timeout=120)
        except Exception as e:
            return StepOutcome.error(f"MCP call failed: {e}")
        # stringify content blocks
        text_parts: list[str] = []
        for block in (getattr(result, "content", None) or []):
            text = getattr(block, "text", None)
            if text:
                text_parts.append(text)
        data = "\n".join(text_parts) if text_parts else str(result)
        return StepOutcome.cont(data=data, prompt="continue")
