"""Stdio MCP server exposing portable built-in tools from tool.py."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Ensure src/ is importable when launched as subprocess
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from tool import LOCAL_MCP_TOOLS, TOOL_MAP

server = Server("claude-code-local")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name=tool.name,
            description=tool.description,
            inputSchema=tool.parameters,
        )
        for tool in LOCAL_MCP_TOOLS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    tool = TOOL_MAP.get(name)
    if tool is None:
        raise ValueError(f"Unknown tool: {name}")

    result = await asyncio.to_thread(tool.run, arguments)
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, ensure_ascii=False)
    return [TextContent(type="text", text=text)]


async def run_server() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
