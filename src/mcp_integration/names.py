"""MCP tool naming: mcp__{server}__{tool} with normalization."""

from __future__ import annotations

import re

_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
MCP_PREFIX = "mcp__"
LOCAL_SERVER_NAME = "local"


def normalize_mcp_name(name: str) -> str:
    """Replace non [a-zA-Z0-9_-] characters with underscore."""
    return _DISALLOWED_CHARS.sub("_", name)


def build_prefixed_name(server_name: str, tool_name: str) -> str:
    safe_server = normalize_mcp_name(server_name)
    safe_tool = normalize_mcp_name(tool_name)
    return f"{MCP_PREFIX}{safe_server}__{safe_tool}"


def parse_prefixed_name(prefixed_name: str) -> tuple[str, str]:
    """Return (server_name, tool_name) from mcp__server__tool."""
    if not prefixed_name.startswith(MCP_PREFIX):
        raise ValueError(f"Not an MCP tool name: {prefixed_name}")
    rest = prefixed_name[len(MCP_PREFIX) :]
    if "__" not in rest:
        raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
    server, tool = rest.split("__", 1)
    return server, tool


def is_mcp_tool(name: str) -> bool:
    return name.startswith(MCP_PREFIX)


def underlying_tool_name(name: str) -> str:
    """Map mcp__local__run_bash -> run_bash for permission/background checks."""
    if not is_mcp_tool(name):
        return name
    server, tool = parse_prefixed_name(name)
    if server == normalize_mcp_name(LOCAL_SERVER_NAME):
        return tool
    return name
