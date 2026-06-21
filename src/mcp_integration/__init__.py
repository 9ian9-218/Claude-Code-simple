"""MCP integration for Claude-Code-simple."""

from mcp_integration.hub import (
    MCPHub,
    get_mcp_hub,
    init_mcp_hub,
    shutdown_mcp_hub,
)
from mcp_integration.names import (
    LOCAL_SERVER_NAME,
    build_prefixed_name,
    is_mcp_tool,
    normalize_mcp_name,
    parse_prefixed_name,
    underlying_tool_name,
)

from mcp_integration.schema_strict import (
    adapt_builtin_tool_args,
    sanitize_openai_tool,
    sanitize_parameters_for_api,
)

__all__ = [
    "MCPHub",
    "LOCAL_SERVER_NAME",
    "adapt_builtin_tool_args",
    "build_prefixed_name",
    "get_mcp_hub",
    "init_mcp_hub",
    "is_mcp_tool",
    "normalize_mcp_name",
    "parse_prefixed_name",
    "sanitize_openai_tool",
    "sanitize_parameters_for_api",
    "shutdown_mcp_hub",
    "underlying_tool_name",
]
