"""Hook 注册表 — 扩展逻辑挂载在事件上，不侵入主循环。"""

import threading
from typing import Any, Callable

from permission_sync import permission_hook_with_bubble as permission_hook
from console_lock import locked_print


def register_hook(event: str, callback: Callable[..., Any]) -> None:
    HOOKS.setdefault(event, []).append(callback)

def trigger_hooks(event: str, *args: Any) -> Any:
    """依次执行 hook；任一 hook 返回非 None 则短路并返回该值。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# ── UserPromptSubmit ──────────────────────────────────────────────────────
from config import get_workdir
def context_inject_hook(query: str) -> None:
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {get_workdir()}\033[0m")


# ── PreToolUse / PostToolUse ──────────────────────────────────────────────

def validate_args(args: dict[str, Any], schema: dict[str, Any]) -> str | None:
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
        if expected == "boolean" and not isinstance(value, bool):
            return f"Parameter '{key}' must be a boolean"

    if "path" in properties and isinstance(args.get("path"), str):
        from tool import _check_path
        return _check_path(args["path"])

    return None


def validate_hook(block) -> str | None:
    """PreToolUse：schema + 路径校验（须在 permission_hook 之前）。"""
    from tool import get_tool_parameters

    schema = get_tool_parameters(block.name)
    if schema is None:
        return f"Unknown tool: {block.name}"
    return validate_args(block.input, schema)


def log_hook(block) -> None:
    from teammates.context import get_agent_context

    ctx = get_agent_context()
    if ctx.is_teammate:
        locked_print(f"\033[90m[{ctx.agent_name}] {block.name}(...)\033[0m")
        return
    locked_print(f"\033[90m[HOOK] {block.name}(...)\033[0m")


def large_output_hook(block, output) -> None:
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}\033[0m")


# ── Stop ──────────────────────────────────────────────────────────────────

def summary_hook(messages: list, *args: Any, **kwargs: Any) -> None:
    """统计本轮 tool 调用次数。"""
    tool_count = sum(1 for m in messages if m.get("role") == "tool")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")


def memory_stop_hook(
    messages: list,
    pre_compress: list | None = None,
    is_subagent: bool = False,
    **kwargs: Any,
) -> None:
    """
    Stop hook：在模型自然结束（无 tool_calls）
    使用 compact 前快照提取；fire-and-forget，不阻塞主循环。
    不在 autoCompact 之后调用。
    """
    if is_subagent or pre_compress is None:
        return
    def _run() -> None:
        from memory import extract_memories, consolidate_memories
        extract_memories(pre_compress)
        consolidate_memories()
    threading.Thread(target=_run, daemon=True).start()


# ── Hook 注册表 ───────────────────────────────────────────────────────────

HOOKS: dict[str, list[Callable[..., Any]]] = {
    "UserPromptSubmit": [context_inject_hook],
    "PreToolUse": [validate_hook, permission_hook, log_hook],
    "PostToolUse": [large_output_hook],
    "Stop": [summary_hook, memory_stop_hook],
}
