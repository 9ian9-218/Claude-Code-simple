"""Sanitize JSON Schemas for OpenAI strict mode (DeepSeek-compatible).

Transforms schemas at API export time only — does not modify tool.py definitions.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from config import TOOL_STRICT


def is_tool_strict_enabled() -> bool:
    return TOOL_STRICT

_CONNECT_MCP_ENV_JSON = {
    "type": "string",
    "description": (
        "Optional environment variables as a JSON object string, "
        'e.g. {"FOO":"bar"}. Use empty string if none.'
    ),
}


def sanitize_schema_for_strict(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively enforce strict-mode rules: additionalProperties=false, required=all props."""
    if not isinstance(schema, dict):
        return schema

    out = copy.deepcopy(schema)
    node_type = out.get("type")

    if node_type == "object":
        props = out.get("properties")
        if isinstance(props, dict):
            out["properties"] = {
                key: sanitize_schema_for_strict(value)
                for key, value in props.items()
            }
        ap = out.get("additionalProperties")
        if isinstance(ap, dict) or ap is None:
            out["additionalProperties"] = False
        if isinstance(out.get("properties"), dict):
            out["required"] = list(out["properties"].keys())
        return out

    if node_type == "array":
        items = out.get("items")
        if isinstance(items, dict):
            out["items"] = sanitize_schema_for_strict(items)
        return out

    return out


def sanitize_connect_mcp_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace map-style env object with env_json string for strict APIs."""
    out = sanitize_schema_for_strict(schema)
    props = dict(out.get("properties") or {})
    if "env" in props:
        props.pop("env")
        props["env_json"] = _CONNECT_MCP_ENV_JSON
        out["properties"] = props
        required = [k for k in out.get("required", []) if k != "env"]
        if "env_json" not in required:
            required.append("env_json")
        out["required"] = required
    return out


def sanitize_parameters_for_api(tool_name: str, parameters: dict[str, Any]) -> dict[str, Any]:
    if not is_tool_strict_enabled():
        return parameters
    if tool_name == "connect_mcp":
        return sanitize_connect_mcp_schema(parameters)
    return sanitize_schema_for_strict(parameters)


def sanitize_openai_tool(tool_name: str, openai_tool: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(openai_tool)
    fn = out.get("function")
    if not isinstance(fn, dict):
        return out
    if not is_tool_strict_enabled():
        fn.pop("strict", None)
        return out
    fn["strict"] = True
    params = fn.get("parameters")
    if isinstance(params, dict):
        fn["parameters"] = sanitize_parameters_for_api(tool_name, params)
    return out


def adapt_connect_mcp_args(args: dict[str, Any]) -> dict[str, Any]:
    """Map API-facing env_json back to env dict for tool execution."""
    if "env_json" not in args:
        return args
    out = dict(args)
    env_json = out.pop("env_json", "") or ""
    if not env_json:
        out["env"] = {}
        return out
    try:
        parsed = json.loads(env_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"env_json must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("env_json must be a JSON object")
    out["env"] = {str(k): str(v) for k, v in parsed.items()}
    return out


def adapt_builtin_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "connect_mcp":
        return adapt_connect_mcp_args(args)
    return args
