"""
MCP Hub — connect to MCP servers via stdio, discover tools, invoke tools.

Runs a dedicated asyncio event loop in a background thread so synchronous
agent code can call MCP without restructuring the main loop.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool as McpTool

from config import PROJECT_ROOT
from mcp_integration.config import MCPServerConfig, get_server_config
from mcp_integration.names import (
    LOCAL_SERVER_NAME,
    build_prefixed_name,
    normalize_mcp_name,
    parse_prefixed_name,
)
from mcp_integration.schema_strict import sanitize_openai_tool, sanitize_parameters_for_api


@dataclass
class RegisteredMcpTool:
    """One MCP tool exposed to the LLM under a prefixed name."""

    prefixed_name: str
    server_name: str
    safe_server_name: str
    original_tool_name: str
    description: str
    parameters: dict[str, Any]
    is_read_only: bool = False


@dataclass
class _ServerState:
    name: str
    safe_name: str
    session: ClientSession
    stack: AsyncExitStack
    tools: list[McpTool] = field(default_factory=list)
    tool_by_prefixed: dict[str, RegisteredMcpTool] = field(default_factory=dict)


class MCPHub:
    """Central interface for MCP server connections and tool calls."""

    DEFAULT_TIMEOUT = 120.0

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="mcp-hub")
        self._ready = threading.Event()
        self._servers: dict[str, _ServerState] = {}
        self._tools: dict[str, RegisteredMcpTool] = {}
        self._lock = threading.Lock()
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("MCP hub event loop failed to start")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def _run(self, coro, timeout: float | None = None):
        timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    @staticmethod
    def _tool_read_only(description: str) -> bool:
        lowered = description.lower()
        return "(readonly)" in lowered or "(read-only)" in lowered or "read only" in lowered

    @staticmethod
    def _format_call_result(result) -> str:
        if result.isError:
            parts = []
            for block in result.content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
            return json.dumps(
                {"status": "error", "message": "\n".join(parts) or "MCP tool error"},
                ensure_ascii=False,
            )
        parts = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        if not parts and result.structuredContent is not None:
            return json.dumps(result.structuredContent, ensure_ascii=False)
        return "\n".join(parts) if parts else "(no output)"

    async def _connect_stdio_async(
        self,
        name: str,
        *,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> _ServerState:
        safe_name = normalize_mcp_name(name)
        if safe_name in self._servers:
            raise ValueError(f"MCP server '{name}' already connected")

        stack = AsyncExitStack()
        params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
            cwd=cwd or str(PROJECT_ROOT),
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listed = await session.list_tools()

        state = _ServerState(
            name=name,
            safe_name=safe_name,
            session=session,
            stack=stack,
            tools=list(listed.tools),
        )
        for tool in listed.tools:
            prefixed = build_prefixed_name(name, tool.name)
            reg = RegisteredMcpTool(
                prefixed_name=prefixed,
                server_name=name,
                safe_server_name=safe_name,
                original_tool_name=tool.name,
                description=tool.description or "",
                parameters=tool.inputSchema or {"type": "object", "properties": {}},
                is_read_only=self._tool_read_only(tool.description or ""),
            )
            state.tool_by_prefixed[prefixed] = reg
            self._tools[prefixed] = reg

        self._servers[safe_name] = state
        return state

    async def _disconnect_async(self, name: str) -> None:
        safe_name = normalize_mcp_name(name)
        state = self._servers.pop(safe_name, None)
        if state is None:
            raise ValueError(f"MCP server '{name}' is not connected")
        for prefixed in list(state.tool_by_prefixed):
            self._tools.pop(prefixed, None)
        await state.stack.aclose()

    async def _call_tool_async(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        safe_name = normalize_mcp_name(server_name)
        state = self._servers.get(safe_name)
        if state is None:
            raise ValueError(f"MCP server '{server_name}' is not connected")
        result = await state.session.call_tool(tool_name, arguments)
        return self._format_call_result(result)

    def connect_stdio(
        self,
        name: str,
        *,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> str:
        with self._lock:
            state = self._run(
                self._connect_stdio_async(
                    name,
                    command=command,
                    args=args,
                    env=env,
                    cwd=cwd,
                )
            )
        tool_names = [t.name for t in state.tools]
        print(f"  \033[31m[mcp] connected: {name} → {tool_names}\033[0m")
        return (
            f"Connected to MCP server '{name}'. "
            f"Discovered {len(state.tools)} tools: {', '.join(tool_names)}"
        )

    def connect_from_config(self, name: str) -> str:
        cfg = get_server_config(name)
        if cfg is None:
            return f"MCP server '{name}' not found in {PROJECT_ROOT / '.claude' / 'mcp.json'}"
        return self.connect_stdio(
            cfg.name,
            command=cfg.command,
            args=cfg.args,
            env=cfg.env or None,
            cwd=cfg.cwd,
        )

    def connect_local_server(self) -> str:
        """Connect built-in local tools MCP server via stdio subprocess."""
        script = PROJECT_ROOT / "local_mcp_server.py"
        if not script.exists():
            raise FileNotFoundError(f"Local MCP server script not found: {script}")
        return self.connect_stdio(
            LOCAL_SERVER_NAME,
            command=sys.executable,
            args=[str(script)],
            cwd=str(PROJECT_ROOT),
        )

    def disconnect(self, name: str) -> str:
        with self._lock:
            self._run(self._disconnect_async(name))
        print(f"  \033[31m[mcp] disconnected: {name}\033[0m")
        return f"Disconnected MCP server '{name}'"

    def list_servers(self) -> list[str]:
        return [state.name for state in self._servers.values()]

    def list_tools(self) -> list[RegisteredMcpTool]:
        return list(self._tools.values())

    def get_tool(self, prefixed_name: str) -> RegisteredMcpTool | None:
        return self._tools.get(prefixed_name)

    def call_prefixed_tool(self, prefixed_name: str, arguments: dict[str, Any]) -> str:
        reg = self._tools.get(prefixed_name)
        if reg is None:
            server, tool = parse_prefixed_name(prefixed_name)
            return self._run(
                self._call_tool_async(server, tool, arguments),
                timeout=self.DEFAULT_TIMEOUT,
            )
        return self._run(
            self._call_tool_async(reg.server_name, reg.original_tool_name, arguments),
            timeout=self.DEFAULT_TIMEOUT,
        )

    def to_openai_tools(self, excluded: frozenset[str] | set[str] | None = None) -> list[dict[str, Any]]:
        excluded = excluded or frozenset()
        out: list[dict[str, Any]] = []
        for reg in sorted(self._tools.values(), key=lambda t: t.prefixed_name):
            if reg.prefixed_name in excluded:
                continue
            out.append(
                sanitize_openai_tool(
                    reg.prefixed_name,
                    {
                        "type": "function",
                        "function": {
                            "name": reg.prefixed_name,
                            "description": reg.description,
                            "strict": True,
                            "parameters": reg.parameters,
                        },
                    },
                )
            )
        return out

    def shutdown(self) -> None:
        with self._lock:
            for name in list(self._servers):
                try:
                    self._run(self._disconnect_async(name), timeout=30)
                except Exception:
                    pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


# Process-wide singleton
_hub: MCPHub | None = None
_hub_lock = threading.Lock()


def get_mcp_hub() -> MCPHub:
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = MCPHub()
        return _hub


def init_mcp_hub(*, connect_local: bool = True, connect_config: bool = True) -> MCPHub:
    """Initialize hub, connect local stdio server and auto-connect config entries."""
    hub = get_mcp_hub()
    if connect_local:
        safe_local = normalize_mcp_name(LOCAL_SERVER_NAME)
        if safe_local not in hub._servers:
            hub.connect_local_server()
    if connect_config:
        from mcp_integration.config import load_mcp_config

        for name, cfg in load_mcp_config().items():
            if not cfg.auto_connect:
                continue
            safe = normalize_mcp_name(name)
            if safe in hub._servers:
                continue
            try:
                hub.connect_stdio(
                    cfg.name,
                    command=cfg.command,
                    args=cfg.args,
                    env=cfg.env or None,
                    cwd=cfg.cwd,
                )
            except Exception as exc:
                print(f"  \033[33m[mcp] auto-connect '{name}' failed: {exc}\033[0m")
    return hub


def shutdown_mcp_hub() -> None:
    global _hub
    with _hub_lock:
        if _hub is not None:
            _hub.shutdown()
            _hub = None
