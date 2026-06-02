"""
agent_loop.py — Agent 主循环

hook 挂载点（与 s04 对齐）：
  UserPromptSubmit → send_messages → PreToolUse → execute → PostToolUse → Stop
"""

import json
from types import SimpleNamespace
from client import send_messages
from hook import trigger_hooks
from tool import execute_tool_call
import compact
from memory import load_memories, find_memory_injection_index, snapshot_messages


def _build_request_messages(messages: list, memories_content: str) -> list:
    if not memories_content:
        return messages
    memory_turn = find_memory_injection_index(messages)
    if memory_turn is None:
        return messages
    request_messages = messages.copy()
    original = messages[memory_turn]["content"]
    from prompt import RELEVANT_MEMORIES_OPEN
    if original.startswith(RELEVANT_MEMORIES_OPEN):
        return messages
    request_messages[memory_turn] = {
        **messages[memory_turn],
        "content": memories_content + "\n\n" + original,
    }
    return request_messages


def agent_loop(messages: list, *, max_turn: int = 100, max_tokens: int = 10000, isSubagent=False) -> str | None:
    """内层循环：反复调用 LLM，直到不再请求工具或达到 max_turn。"""
    memories_content = "" if isSubagent else load_memories(messages)
    pre_compress = snapshot_messages(messages)
    reactive_retries = 0

    for turn in range(max_turn):
        #print(f"第{turn}步")
        messages[:] = compact.tool_result_budget(messages)    # L3: persist large results first
        messages[:] = compact.snip_compact(messages)          # L1: trim middle
        messages[:] = compact.micro_compact(messages)         # L2: old result placeholders

        if compact.estimate_messages_tokens(messages) > compact.CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact.compact_history(messages)
        try:
            request_messages = _build_request_messages(messages, memories_content)
            message = send_messages(request_messages, max_tokens=max_tokens, isSubagent=isSubagent)
            reactive_retries = 0
        except Exception as e:
            if (
                "prompt_too_long" in str(e).lower() or "too many tokens" in str(e).lower()
            ) and reactive_retries < compact.MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = compact.reactive_compact(messages)
                reactive_retries += 1
                continue
            raise
        if message.tool_calls:
            messages.append(message.model_dump(exclude_none=True))
            for tool_call in message.tool_calls:
                try:
                    args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as e:
                    tool_result = json.dumps({"status": "error", "message": f"Invalid arguments JSON: {e}"})
                else:
                    if not isinstance(args, dict):
                        tool_result = json.dumps({"status": "error", "message": "Arguments must be a JSON object"})
                    else:
                        block = SimpleNamespace(name=tool_call.function.name, input=args)
                        blocked = trigger_hooks("PreToolUse", block)
                        if blocked is not None:
                            tool_result = json.dumps({"status": "error", "message": str(blocked)})
                        else:
                            tool_result = execute_tool_call(tool_call, args=args)
                            trigger_hooks("PostToolUse", block, tool_result)
                if not isSubagent:
                    print(f"Tool >\t {tool_call.function.name}({tool_call.function.arguments}) -> {tool_result}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_result,
                })
            continue
        if message.content is not None:
            messages.append(message.model_dump(exclude_none=True))
            if isSubagent:
                return message.content
        if isSubagent:
            return "Subagent stopped after 30 turns without final answer."
        # 自然结束（无 tool_calls）→ Stop hook：提取 memory + Dream（非 autoCompact 后）
        force = trigger_hooks("Stop", messages, pre_compress, isSubagent)
        if force:
            messages.append({"role": "user", "content": force})
            continue
        return


