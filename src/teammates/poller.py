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


def _permission_request_id(parsed: dict[str, Any], *, sandbox: bool) -> str:
    if sandbox:
        return str(parsed.get("requestId", ""))
    return str(parsed.get("request_id", ""))


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

    with _poller_lock:
        if msg_type == "idle_notification":
            _pending_idle_notifications.append(parsed)
            mark_message_as_read_by_index(lead_name, team_name, index)
        elif msg_type == "shutdown_approved":
            mark_message_as_read_by_index(lead_name, team_name, index)
            print(f"  \033[33m[team] {parsed.get('from')} shutdown approved\033[0m")
        elif msg_type == "shutdown_rejected":
            mark_message_as_read_by_index(lead_name, team_name, index)
            print(
                f"  \033[31m[team] {parsed.get('from')} rejected shutdown: "
                f"{parsed.get('reason')}\033[0m"
            )
        elif msg_type == "teammate_terminated":
            mark_message_as_read_by_index(lead_name, team_name, index)
        else:
            mark_message_as_read_by_index(lead_name, team_name, index)


def _lead_poll_loop() -> None:
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
    _lead_poller_thread = threading.Thread(
        target=_lead_poll_loop,
        daemon=True,
        name="lead-inbox-poller",
    )
    _lead_poller_thread.start()
    print(f"  \033[36m[poller] lead inbox poller started (team={team_name})\033[0m")


def stop_lead_inbox_poller() -> None:
    _lead_poller_stop.set()
