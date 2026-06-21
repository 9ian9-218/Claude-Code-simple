"""
Inbox pollers — Lead polls every 1s; workers poll permission responses every 500ms.

Lead poller routes structured messages (permission_request, idle_notification, etc.)
to in-process queues consumed by permission_sync (main thread) and agent_loop injection.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from teammates.constants import LEAD_INBOX_POLL_INTERVAL
from teammates.mailbox import mark_message_as_read_by_index, read_mailbox
from teammates.message_types import (
    format_teammate_messages,
    is_structured_protocol_message,
    parse_structured,
)
from teammates.team_helpers import get_leader_name

# Queues populated by lead inbox poller
_pending_injections: deque[str] = deque()
_pending_permission_requests: deque[dict[str, Any]] = deque()
_pending_idle_notifications: deque[dict[str, Any]] = deque()
_poller_lock = threading.Lock()

_lead_poller_thread: threading.Thread | None = None
_lead_poller_stop = threading.Event()
_team_name: str | None = None


def consume_pending_injections() -> list[str]:
    with _poller_lock:
        items = list(_pending_injections)
        _pending_injections.clear()
    return items


def consume_pending_permission_requests() -> list[dict[str, Any]]:
    with _poller_lock:
        items = list(_pending_permission_requests)
        _pending_permission_requests.clear()
    return items


def consume_pending_idle_notifications() -> list[dict[str, Any]]:
    with _poller_lock:
        items = list(_pending_idle_notifications)
        _pending_idle_notifications.clear()
    return items


# _permission_request_id 函数用于从结构化的权限请求消息（parsed，为dict）中提取该请求的唯一ID。
# 其中，如果是沙盒权限请求（sandbox=True）则使用 "requestId" 字段，否则使用普通的 "request_id" 字段。
def _permission_request_id(parsed: dict[str, Any], *, sandbox: bool) -> str:
    if sandbox:
        return str(parsed.get("requestId", ""))
    return str(parsed.get("request_id", ""))

# _enqueue_permission_request 函数将一个解析好的权限请求（字典）加入到队列_pending_permission_requests。
# 具体步骤：
# 1. 先通过_request_permission_id计算当前请求的唯一ID。
# 2. 加锁后，从已有的_pending_permission_requests中提取所有已有ID，去重。
# 3. 若本次请求ID在已有集合里则不添加，保证每个权限请求仅被处理一次。
# 4. 否则将该item加入队列，等待主线程/业务流程处理。
def _enqueue_permission_request(item: dict[str, Any]) -> None:
    parsed = item["parsed"]
    sandbox = item.get("sandbox", False)
    req_id = _permission_request_id(parsed, sandbox=sandbox)
    with _poller_lock:
        existing = {
            _permission_request_id(i["parsed"], sandbox=i.get("sandbox", False))
            for i in _pending_permission_requests
        }
        if req_id and req_id in existing:
            return
        _pending_permission_requests.append(item)
   


def _route_message(entry: dict[str, Any], index: int, team_name: str, lead_name: str) -> None:
    """
    负责将收到的结构化消息（entry）根据类型分发到对应的处理函数。

    参数:
        entry:       当前邮箱中的消息字典，包含text等字段
        index:       此消息在邮箱中的下标，用于“标记已读”
        team_name:   当前团队名
        lead_name:   团队lead成员ID
    """
    text = entry.get("text", "")
    if not is_structured_protocol_message(text):
        return

    parsed = parse_structured(text)
    if not parsed:
        return

    msg_type = parsed.get("type")
    if msg_type == "permission_request":
        _enqueue_permission_request({
            "entry": entry,
            "parsed": parsed,
            "index": index,
        })
        return

    if msg_type == "sandbox_permission_request":
        _enqueue_permission_request({
            "entry": entry,
            "parsed": parsed,
            "index": index,
            "sandbox": True,
        })
        return

    from teammates.protocol import (
        ProtocolState,
        format_plan_approval_injection,
        match_response,
        register_request,
    )

    with _poller_lock:
        if msg_type == "idle_notification":
            _pending_idle_notifications.append(parsed)
            mark_message_as_read_by_index(lead_name, team_name, index)
        elif msg_type == "plan_approval_request":
            req_id = parsed.get("requestId") or parsed.get("request_id", "")
            from_agent = entry.get("from") or parsed.get("from", "unknown")
            register_request(ProtocolState(
                request_id=req_id,
                type="plan_approval",
                sender=from_agent,
                target=lead_name,
                payload=parsed.get("planContent", ""),
            ))
            _pending_injections.append(format_plan_approval_injection(parsed))
            mark_message_as_read_by_index(lead_name, team_name, index)
        elif msg_type == "shutdown_approved":
            match_response(
                response_type="shutdown_approved",
                request_id=parsed.get("requestId", ""),
                approved=True,
            )
            mark_message_as_read_by_index(lead_name, team_name, index)
            print(f"  \033[33m[team] {parsed.get('from')} shutdown approved\033[0m")
        elif msg_type == "shutdown_rejected":
            match_response(
                response_type="shutdown_rejected",
                request_id=parsed.get("requestId", ""),
                approved=False,
            )
            mark_message_as_read_by_index(lead_name, team_name, index)
            print(
                f"  \033[31m[team] {parsed.get('from')} rejected shutdown: "
                f"{parsed.get('reason')}\033[0m"
            )
        elif msg_type == "teammate_terminated":
            mark_message_as_read_by_index(lead_name, team_name, index)
            agent = parsed.get("agentName", "unknown")
            print(f"  \033[33m[team] teammate terminated: {agent}\033[0m")
        else:
            mark_message_as_read_by_index(lead_name, team_name, index)


def _lead_poll_loop() -> None:
    """
    主轮询循环，负责从团队主管（lead）的邮箱中轮询消息，并处理这些消息。
    这个循环会一直运行，直到 _lead_poller_stop 事件被设置（即停止标志被触发）。
    """
    while not _lead_poller_stop.is_set():
        team_name = _team_name
        if not team_name:
            _lead_poller_stop.wait(LEAD_INBOX_POLL_INTERVAL)
            continue
        try:
            lead_name = get_leader_name(team_name)
            mailbox = read_mailbox(lead_name, team_name)
            plain_batch: list[dict[str, Any]] = []

            for i, entry in enumerate(mailbox):
                if entry.get("read"):
                    continue
                text = entry.get("text", "")
                if is_structured_protocol_message(text):
                    _route_message(entry, i, team_name, lead_name)
                else:
                    plain_batch.append(entry)

            if plain_batch:
                formatted = format_teammate_messages(plain_batch)
                if formatted:
                    with _poller_lock:
                        _pending_injections.append(formatted)
                for i, entry in enumerate(mailbox):
                    if entry in plain_batch:
                        mark_message_as_read_by_index(lead_name, team_name, i)

        except Exception as e:
            print(f"  \033[31m[poller] error: {e}\033[0m")

        _lead_poller_stop.wait(LEAD_INBOX_POLL_INTERVAL)


def start_lead_inbox_poller(team_name: str) -> None:
    global _lead_poller_thread, _team_name
    _team_name = team_name
    if _lead_poller_thread and _lead_poller_thread.is_alive():
        return
    _lead_poller_stop.clear()
    # 创建并初始化一个新线程，用于轮询（poll）团队lead邮箱队列（lead inbox）。
    # 该线程的目标函数是 _lead_poll_loop，线程名称设为 "lead-inbox-poller"，并以守护线程（daemon）的方式运行。
    _lead_poller_thread = threading.Thread(
        target=_lead_poll_loop,
        daemon=True,
        name="lead-inbox-poller",
    )
    _lead_poller_thread.start()
    print(f"  \033[36m[poller] lead inbox poller started (team={team_name})\033[0m")


def stop_lead_inbox_poller() -> None:
    _lead_poller_stop.set()
