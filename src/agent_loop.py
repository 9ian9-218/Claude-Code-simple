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
from background_task import should_run_background, start_background_task
from messageQueueManager import consume_pending_notifications
from loop_options import LoopOptions
from teammates.context import get_agent_context


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


def _inject_pending_notifications(messages: list, *, recipient: str | None = None) -> None:
    """消费命令队列中的 pending 通知（对齐 query.ts:1566-1593）。"""
    for notif in consume_pending_notifications(recipient=recipient):
        messages.append({"role": "user", "content": notif})
        print(f"  \033[32m[inject] task_notification\033[0m")


def _inject_teammate_inbox(messages: list) -> None:
    """Lead inbox poller 投递的队友消息 → 注入为 user turn。"""
    from teammates.poller import consume_pending_injections

    for content in consume_pending_injections():
        messages.append({"role": "user", "content": content})
        print(f"  \033[33m[inject] teammate inbox message\033[0m")


def _process_lead_permissions() -> None:
    """主线程消费 Teammate 权限请求（避免 poller 线程争抢 stdin）。"""
    ctx = get_agent_context()
    if not ctx.is_lead or not ctx.team_name:
        return
    from permission_sync import process_pending_lead_permissions

    process_pending_lead_permissions(ctx.team_name)


def agent_loop(
    messages: list,
    *,
    max_turn: int = 100,
    max_tokens: int = 8000,
    isSubagent: bool = False,
    loop_options: LoopOptions | None = None,
) -> str | None:
    """内层循环：反复调用 LLM，直到不再请求工具或达到 max_turn。"""
    opts = loop_options or LoopOptions.from_legacy_is_subagent(isSubagent)
    memories_content = "" if not opts.enable_memory else load_memories(messages)
    pre_compress = snapshot_messages(messages)
    recovery_state = RecoveryState()
    effective_max_tokens = max_tokens
    bg_recipient = None if opts.inject_lead_notifications else get_agent_context().agent_name

    for turn in range(max_turn):
        if opts.inject_lead_notifications:
            _process_lead_permissions()
            _inject_pending_notifications(messages, recipient=None)
            _inject_teammate_inbox(messages)
        elif opts.inject_background_notifications:
            _inject_pending_notifications(messages, recipient=bg_recipient)

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
            isSubagent=opts.exit_on_final_content and not opts.preserve_system,
            preserve_system=opts.preserve_system,
            quiet_output=opts.quiet_output,
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
                        block = SimpleNamespace(
                            name=tool_call.function.name,
                            input=args,
                            id=tool_call.id,
                        )
                        blocked = trigger_hooks("PreToolUse", block)
                        if blocked is not None:
                            tool_result = json.dumps({"status": "error", "message": str(blocked)})
                        else:
                            if opts.enable_background and should_run_background(tool_call.function.name, args):
                                bg_id = start_background_task(tool_call, args)
                                command = args.get("command", "")
                                tool_result = (
                                    f"[Background task {bg_id} started] "
                                    f"Command: {command}. "
                                    f"Output will arrive as a <task_notification> user message "
                                    f"when the task completes or stalls."
                                )
                            else:
                                tool_result = execute_tool_call(tool_call, args=args)
                                trigger_hooks("PostToolUse", block, tool_result)
                if not opts.quiet_output:
                    print(f"Tool >\t {tool_call.function.name}({tool_call.function.arguments}) -> {tool_result}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_result,
                })
            continue
        if message.content is not None:
            messages.append(message.model_dump(exclude_none=True))
            if opts.exit_on_final_content:
                return message.content
        if opts.exit_on_final_content:
            return "Subagent stopped after 30 turns without final answer."
        # 自然结束 → Stop hook：提取 memory + Dream（非 autoCompact 后）
        force = trigger_hooks("Stop", messages, pre_compress, opts.skip_memory_stop_hook)
        if force:
            messages.append({"role": "user", "content": force})
            continue
        return
