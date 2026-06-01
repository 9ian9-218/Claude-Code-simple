"""Hook 注册表 — 扩展逻辑挂载在事件上，不侵入主循环。"""


from typing import Any, Callable

from check_permissions import permission_hook


def register_hook(event: str, callback: Callable[..., Any]) -> None:
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args: Any) -> Any:
    """依次执行 hook；任一 hook 返回非 None 则短路并返回该值。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# ── UserPromptSubmit ──────────────────────────────────────────────────────
from config import WORKDIR
def context_inject_hook(query: str) -> None:
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")


# ── PreToolUse / PostToolUse ──────────────────────────────────────────────

def validate_args(args: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """schema + 路径安全校验。"""
    """schema + 路径安全校验。"""
    required = schema.get("required", [])
    properties = schema.get("properties", {})

    for key in required:
        if key not in args:
            return f"Missing required parameter: {key}"

    if schema.get("additionalProperties") is False:
        extra = set(args) - set(properties)
        if extra:
            return f"Unexpected parameters: {', '.join(sorted(extra))}"

    for key, value in args.items():
        prop = properties.get(key)
        if prop is None:
            continue
        expected = prop.get("type")
        if expected == "string" and not isinstance(value, str):
            return f"Parameter '{key}' must be a string"
        if expected == "integer" and not isinstance(value, int):
            return f"Parameter '{key}' must be an integer"
        if expected == "number" and not isinstance(value, (int, float)):
            return f"Parameter '{key}' must be a number"
        if expected == "array" and not isinstance(value, list):
            return f"Parameter '{key}' must be a array"

    if "path" in properties and isinstance(args.get("path"), str):
        from tool import _check_path
        return _check_path(args["path"])

    return None


def validate_hook(block) -> str | None:
    """PreToolUse：schema + 路径校验（须在 permission_hook 之前）。"""
    from tool import _get_tool_map
    tool = _get_tool_map().get(block.name)
    if tool is None:
        return f"Unknown tool: {block.name}"
    return validate_args(block.input, tool.parameters)


def log_hook(block) -> None:
    print(f"\033[90m[HOOK] {block.name}(...)\033[0m")


def large_output_hook(block, output) -> None:
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}\033[0m")


# ── Stop ──────────────────────────────────────────────────────────────────

def summary_hook(messages: list) -> None:
    """
    Stop 阶段的 hook，用于会话结束时统计和输出本次会话用过多少次工具调用（即 tool role 的消息数量）。
    主要作用是：辅助统计和调试，便于观察 Agent 在一次对话中实际调用工具的次数。
    """
    tool_count = sum(1 for m in messages if m.get("role") == "tool")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")

# from memory import extract_memories,consolidate_memories
# def update_memory_hook(messages:list) -> None:
#     extract_memories(messages)
#     consolidate_memories()
# register_hook("stop",update_memory_hook)

# ── Hook 注册表 ───────────────────────────────────────────────────────────

HOOKS: dict[str, list[Callable[..., Any]]] = {
    "UserPromptSubmit": [context_inject_hook],
    "PreToolUse": [validate_hook, permission_hook, log_hook],
    "PostToolUse": [large_output_hook],
    "Stop": [summary_hook],
}
