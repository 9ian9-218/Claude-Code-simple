"""Load MCP server definitions from .claude/mcp.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import CLAUDE_DIR

MCP_CONFIG_PATH = CLAUDE_DIR / "mcp.json"


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    auto_connect: bool = False


def _parse_server(name: str, raw: dict[str, Any]) -> MCPServerConfig:
    command = raw.get("command")
    if not command or not isinstance(command, str):
        raise ValueError(f"MCP server '{name}' missing string 'command'")
    args = raw.get("args") or []
    if not isinstance(args, list):
        raise ValueError(f"MCP server '{name}' 'args' must be a list")
    env = raw.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError(f"MCP server '{name}' 'env' must be an object")
    cwd = raw.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError(f"MCP server '{name}' 'cwd' must be a string")
    auto_connect = bool(raw.get("autoConnect") or raw.get("auto_connect"))
    return MCPServerConfig(
        name=name,
        command=command,
        args=[str(a) for a in args],
        env={str(k): str(v) for k, v in env.items()},
        cwd=cwd,
        auto_connect=auto_connect,
    )


def load_mcp_config(path: Path | None = None) -> dict[str, MCPServerConfig]:
    """Return server name -> config. Missing file yields empty dict."""
    config_path = path or MCP_CONFIG_PATH
    if not config_path.exists():
        return {}
    data = json.loads(config_path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or data.get("servers") or {}
    if not isinstance(servers, dict):
        raise ValueError("mcp.json: mcpServers must be an object")
    return {name: _parse_server(name, raw) for name, raw in servers.items()}


def get_server_config(name: str, path: Path | None = None) -> MCPServerConfig | None:
    return load_mcp_config(path).get(name)
