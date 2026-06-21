"""
Synchronized permission prompts for agent swarms (aligned with permissionSync.ts).

Mailbox-based flow:
  1. Worker (teammate/subagent) hits permission gate
  2. Worker sends permission_request → Lead inbox
  3. Lead inbox poller (1s) enqueues permission_request (no stdin on poller thread)
  4. Main thread (agent_loop / main idle) calls process_pending_lead_permissions → user y/N
  5. Lead sends permission_response → Worker inbox
  6. Worker polls inbox (500ms) and continues or aborts

Subagents (in-process): synchronous bubble — prompt shown immediately with
[Subagent] label since Lead loop is blocked inside subagent_task.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from teammates.constants import WORKER_PERMISSION_POLL_INTERVAL
from teammates.context import get_agent_context
from teammates.mailbox import send_structured_message, mark_message_as_read_by_index
from teammates.message_types import (
    create_permission_request_message,
    create_permission_response_message,
    create_sandbox_permission_response,
    parse_structured,
)
from teammates.poller import consume_pending_permission_requests
from teammates.team_helpers import get_leader_name

PERMISSION_POLL_TIMEOUT_SEC = 300
PERMISSION_POLL_INTERVAL_SEC = WORKER_PERMISSION_POLL_INTERVAL


@dataclass
class SwarmPermissionRequest:
    """
    权限请求类，用于封装权限请求的详细信息。
    """
    id: str
    worker_name: str
    worker_id: str
    team_name: str
    tool_name: str
    tool_use_id: str
    description: str
    input: dict[str, Any]
    worker_color: str | None = None
    permission_suggestions: list[Any] = field(default_factory=list)
    status: Literal["pending", "approved", "rejected"] = "pending"
    created_at: float = field(default_factory=lambda: time.time())


@dataclass
class PermissionResolution:
    decision: Literal["approved", "rejected"]
    resolved_by: Literal["worker", "leader"] = "leader"
    feedback: str | None = None
    updated_input: dict[str, Any] | None = None
    permission_updates: list[Any] | None = None


def generate_request_id() -> str:
    return f"perm-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def create_permission_request(
    *,
    tool_name: str,
    tool_use_id: str,
    input_data: dict[str, Any],
    description: str,
    team_name: str | None = None,
    worker_name: str | None = None,
    worker_id: str | None = None,
    worker_color: str | None = None,
) -> SwarmPermissionRequest:
    ctx = get_agent_context()
    return SwarmPermissionRequest(
        id=generate_request_id(),
        worker_name=worker_name or ctx.agent_name,
        worker_id=worker_id or ctx.agent_id or ctx.agent_name,
        team_name=team_name or ctx.team_name or "default",
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        description=description,
        input=input_data,
        worker_color=worker_color or ctx.color,
    )


def send_permission_request_via_mailbox(request: SwarmPermissionRequest) -> bool:
    leader = get_leader_name(request.team_name)
    payload = create_permission_request_message(
        request_id=request.id,
        agent_id=request.worker_name,
        tool_name=request.tool_name,
        tool_use_id=request.tool_use_id,
        description=request.description,
        input_data=request.input,
        permission_suggestions=request.permission_suggestions,
    )
    try:
        send_structured_message(
            from_agent=request.worker_name,
            to_agent=leader,
            payload=payload,
            team_name=request.team_name,
            color=request.worker_color,
        )
        return True
    except Exception as e:
        print(f"  \033[31m[permission] failed to send request: {e}\033[0m")
        return False


def send_permission_response_via_mailbox(
    worker_name: str,
    resolution: PermissionResolution,
    request_id: str,
    team_name: str,
) -> bool:
    subtype = "success" if resolution.decision == "approved" else "error"
    payload = create_permission_response_message(
        request_id=request_id,
        subtype=subtype,
        error=resolution.feedback,
        updated_input=resolution.updated_input,
        permission_updates=resolution.permission_updates,
    )
    try:
        send_structured_message(
            from_agent=get_leader_name(team_name),
            to_agent=worker_name,
            payload=payload,
            team_name=team_name,
        )
        return True
    except Exception as e:
        print(f"  \033[31m[permission] failed to send response: {e}\033[0m")
        return False


def _ask_user_for_permission(
    request: SwarmPermissionRequest,
    *,
    label: str | None = None,
) -> PermissionResolution:
    """Lead terminal prompt (Gate 3) with optional worker label/color."""
    color = request.worker_color or "white"
    prefix = label or f"Teammate [{request.worker_name}]"
    print(f"\n\033[33m⚠  Permission request from {prefix} ({color})\033[0m")
    print(f"   Tool: {request.tool_name}")
    print(f"   Reason: {request.description}")
    print(f"   Input: {json.dumps(request.input, ensure_ascii=False)[:200]}")
    choice = input("   Allow? [y/N] ").strip().lower()
    if choice in ("y", "yes"):
        return PermissionResolution(decision="approved", resolved_by="leader")
    return PermissionResolution(
        decision="rejected",
        resolved_by="leader",
        feedback="Permission denied by user",
    )


def process_pending_lead_permissions(team_name: str) -> None:
    """Process permission_request messages on the main thread (stdin-safe)."""
    for item in consume_pending_permission_requests():
        _resolve_permission_item(item, team_name)


def process_lead_permission_queue(team_name: str) -> None:
    """Deprecated alias — use process_pending_lead_permissions on the main thread."""
    process_pending_lead_permissions(team_name)


def _resolve_permission_item(item: dict[str, Any], team_name: str) -> None:
    """Resolve a single queued permission request (run on main thread)."""
    parsed = item["parsed"]
    entry = item["entry"]
    index = item["index"]
    is_sandbox = item.get("sandbox", False)

    if is_sandbox:
        worker = parsed.get("workerName", "unknown")
        host = parsed.get("hostPattern", {}).get("host", "unknown")
        print(f"\n\033[33m⚠  Sandbox network request from [{worker}]: {host}\033[0m")
        choice = input("   Allow network access? [y/N] ").strip().lower()
        allow = choice in ("y", "yes")
        payload = create_sandbox_permission_response(
            request_id=parsed.get("requestId", ""),
            host=host,
            allow=allow,
        )
        send_structured_message(
            from_agent=get_leader_name(team_name),
            to_agent=worker,
            payload=payload,
            team_name=team_name,
        )
        from teammates.mailbox import mark_message_as_read_by_index
        mark_message_as_read_by_index(get_leader_name(team_name), team_name, index)
        return

    request = SwarmPermissionRequest(
        id=parsed.get("request_id", generate_request_id()),
        worker_name=parsed.get("agent_id", entry.get("from", "unknown")),
        worker_id=parsed.get("agent_id", ""),
        team_name=team_name,
        tool_name=parsed.get("tool_name", ""),
        tool_use_id=parsed.get("tool_use_id", ""),
        description=parsed.get("description", "Permission required"),
        input=parsed.get("input", {}),
        worker_color=entry.get("color"),
        permission_suggestions=parsed.get("permission_suggestions", []),
    )

    resolution = _ask_user_for_permission(request)
    send_permission_response_via_mailbox(
        request.worker_name, resolution, request.id, team_name
    )
    from teammates.mailbox import mark_message_as_read_by_index
    mark_message_as_read_by_index(get_leader_name(team_name), team_name, index)


def poll_for_permission_response(
    request_id: str,
    agent_name: str,
    team_name: str,
    timeout_sec: float = PERMISSION_POLL_TIMEOUT_SEC,
) -> PermissionResolution | None:
    """Worker-side: poll own inbox for permission_response (500ms interval)."""
    from teammates.mailbox import read_mailbox

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        full_mailbox = read_mailbox(agent_name, team_name)
        for i, entry in enumerate(full_mailbox):
            if entry.get("read"):
                continue
            parsed = parse_structured(entry.get("text", ""))
            if not parsed or parsed.get("type") != "permission_response":
                continue
            if parsed.get("request_id") != request_id:
                continue
            mark_message_as_read_by_index(agent_name, team_name, i)
            if parsed.get("subtype") == "success":
                resp = parsed.get("response") or {}
                return PermissionResolution(
                    decision="approved",
                    resolved_by="leader",
                    updated_input=resp.get("updated_input"),
                    permission_updates=resp.get("permission_updates"),
                )
            return PermissionResolution(
                decision="rejected",
                resolved_by="leader",
                feedback=parsed.get("error", "Permission denied"),
            )
        time.sleep(PERMISSION_POLL_INTERVAL_SEC)
    return None


def _bubble_subagent_permission(
    tool_name: str,
    args: dict[str, Any],
    reason: str,
    tool_use_id: str,
) -> str | None:
    """Synchronous permission bubble for in-process subagents."""
    ctx = get_agent_context()
    request = create_permission_request(
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        input_data=args,
        description=reason,
        worker_name=ctx.agent_name,
        worker_id=ctx.agent_id,
    )
    resolution = _ask_user_for_permission(
        request,
        label=f"Subagent [{ctx.agent_name}]",
    )
    if resolution.decision == "approved":
        return None
    return f"Permission denied: {resolution.feedback or reason}"


def _bubble_teammate_permission(
    tool_name: str,
    args: dict[str, Any],
    reason: str,
    tool_use_id: str,
) -> str | None:
    """Async mailbox permission bubble for background teammate threads."""
    ctx = get_agent_context()
    if not ctx.team_name:
        return f"Permission denied: teammate has no team context"

    request = create_permission_request(
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        input_data=args,
        description=reason,
    )
    if not send_permission_request_via_mailbox(request):
        return "Permission denied: failed to send permission request to lead"

    print(f"  \033[33m[permission] {ctx.agent_name} waiting for lead approval...\033[0m")
    resolution = poll_for_permission_response(
        request.id, ctx.agent_name, ctx.team_name
    )
    if resolution is None:
        return "Permission denied: timed out waiting for lead response"
    if resolution.decision == "approved":
        return None
    return f"Permission denied: {resolution.feedback or reason}"


def check_permission_with_bubble(
    tool_name: str,
    args: dict[str, Any],
    reason: str,
    *,
    tool_use_id: str = "",
) -> str | None:
    """
    Unified permission check with bubbling.
    Returns None if allowed, error string if denied.
    """
    ctx = get_agent_context()

    if ctx.is_subagent:
        return _bubble_subagent_permission(tool_name, args, reason, tool_use_id)

    if ctx.is_teammate:
        return _bubble_teammate_permission(tool_name, args, reason, tool_use_id)

    # Lead: direct terminal prompt (existing behavior)
    request = create_permission_request(
        tool_name=tool_name,
        tool_use_id=tool_use_id or generate_request_id(),
        input_data=args,
        description=reason,
    )
    resolution = _ask_user_for_permission(request, label="Lead")
    if resolution.decision == "approved":
        return None
    return f"Permission denied: {resolution.feedback or reason}"


def permission_hook_with_bubble(block) -> str | None:
    """
    这是 PreToolUse 阶段调用的权限钩子函数。它的作用是先执行拒绝列表和自定义规则检查，
    如果需要，还会向上汇报（bubble）以请求更高级别的人工批准。
    参数:
        block: 包含工具调用信息的对象，通常有 name（工具名）、input（参数）、id（调用ID）
    返回:
        - None: 表示允许本次工具调用继续执行
        - str: 包含错误或拒绝原因的字符串，表示被拒绝
    逻辑步骤:
    1. 导入 check_deny_list（命令黑名单检查）和 check_rules（自定义规则检查）方法。
    2. 提取工具名、参数和工具调用ID。
    3. 针对 "run_bash" 工具，先走黑名单检查（比如禁止某类危险命令），如果有理由直接拒绝并返回原因。
    4. 执行自定义规则检查（如需特定参数、敏感操作提醒等），通过则直接返回 None。
    5. 获取当前 agent 的上下文。如果是单机的 lead（未加入任何 team），
       则直接本地弹窗（ask_user）让用户做出决定。
    6. 如果是 team 中的 lead/teammate/subagent 等，需要进一步上报请求（bubble），直到人工批准或拒绝。

    """
    from check_permissions import check_deny_list, check_rules
    from mcp_integration.names import underlying_tool_name

    tool_name = block.name
    effective_name = underlying_tool_name(tool_name)
    args = block.input
    tool_use_id = getattr(block, "id", "") or generate_request_id()

    # Step 1: Bash 黑名单拦截
    if effective_name == "run_bash":
        reason = check_deny_list(args.get("command", ""))
        if reason:
            print(f"\n\033[31m⛔ {reason}\033[0m")
            return reason

    # Step 2: 通用自定义规则检查
    reason = check_rules(effective_name, args)
    if not reason:
        return None  # 规则通过

    # Step 3: 判断当前 agent 身份
    ctx = get_agent_context()
    if ctx.is_lead and not ctx.team_name:
        # 独立 lead：本地人工询问允许与否
        from check_permissions import ask_user
        decision = ask_user(tool_name, args, reason)
        if decision == "deny":
            return f"Permission denied: {reason}"
        return None  # 允许

    # Step 4: Team 场景，走 bubbling 权限上报与审批
    return check_permission_with_bubble(
        tool_name, args, reason, tool_use_id=tool_use_id
    )
