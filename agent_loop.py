"""
agent_loop.py — Agent 主循环

hook 挂载点（与 s04 对齐）：
  UserPromptSubmit → send_messages → PreToolUse → execute → PostToolUse → Stop
"""

import json
from types import SimpleNamespace
from hook import trigger_hooks
from tool import execute_tool_call
import compact
from memory import load_memories, find_memory_injection_index, snapshot_messages
from error_recovery import RecoveryState, send_messages_with_recovery


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



def agent_loop(messages: list, *, max_turn: int = 100, max_tokens: int = 8000, isSubagent=False) -> str | None:
    """内层循环：反复调用 LLM，直到不再请求工具或达到 max_turn。"""
    memories_content = "" if isSubagent else load_memories(messages)
    pre_compress = snapshot_messages(messages)
    recovery_state = RecoveryState()
    effective_max_tokens = max_tokens

    for turn in range(max_turn):
        #print(f"第{turn}步")
        messages[:] = compact.tool_result_budget(messages)    # L3: persist large results first
        messages[:] = compact.snip_compact(messages)          # L1: trim middle
        messages[:] = compact.micro_compact(messages)         # L2: old result placeholders

        if compact.estimate_messages_tokens(messages) > compact.CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact.compact_history(messages)
        request_messages = _build_request_messages(messages, memories_content)
        llm_result = send_messages_with_recovery(
            request_messages=request_messages,
            messages=messages,
            state=recovery_state,
            max_tokens=effective_max_tokens,
            isSubagent=isSubagent,
        )
        if llm_result.action == "retry":
            if llm_result.max_tokens is not None:
                effective_max_tokens = llm_result.max_tokens
            continue
        if llm_result.action == "abort":
            return
        message = llm_result.message
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
        # 自然结束 → Stop hook：提取 memory + Dream（非 autoCompact 后）
        force = trigger_hooks("Stop", messages, pre_compress, isSubagent)
        if force:
            messages.append({"role": "user", "content": force})
            continue
        return


