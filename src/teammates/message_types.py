"""Structured teammate mailbox message types (15 types, aligned with teammateMailbox.ts)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id(prefix: str = "req") -> str:
    return f"{prefix}-{int(datetime.now(timezone.utc).timestamp() * 1000)}-{uuid.uuid4().hex[:8]}"


# ── Plain text is wrapped in TeammateMessage.text, not a separate JSON type ──

STRUCTURED_TYPES = frozenset({
    "idle_notification",
    "permission_request",
    "permission_response",
    "plan_approval_request",
    "plan_approval_response",
    "shutdown_request",
    "shutdown_approved",
    "shutdown_rejected",
    "task_assignment",
    "team_permission_update",
    "mode_set_request",
    "sandbox_permission_request",
    "sandbox_permission_response",
    "teammate_terminated",
})


def is_structured_protocol_message(text: str) -> bool:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    return parsed.get("type") in STRUCTURED_TYPES


def parse_structured(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, dict) and parsed.get("type") in STRUCTURED_TYPES:
        return parsed
    return None


def format_teammate_messages(messages: list[dict[str, Any]]) -> str:
    """Wrap plain-text mailbox entries as <teammate-message> XML for the model."""
    from teammates.constants import TEAMMATE_MESSAGE_TAG

    parts: list[str] = []
    for m in messages:
        text = m.get("text", "")
        if is_structured_protocol_message(text):
            continue
        sender = m.get("from", "unknown")
        color = m.get("color")
        summary = m.get("summary")
        attrs = f'teammate_id="{sender}"'
        if color:
            attrs += f' color="{color}"'
        if summary:
            attrs += f' summary="{summary}"'
        parts.append(f"<{TEAMMATE_MESSAGE_TAG} {attrs}>\n{text}\n</{TEAMMATE_MESSAGE_TAG}>")
    return "\n\n".join(parts)


# ── Message factories ─────────────────────────────────────────────────────

def create_idle_notification(
    agent_id: str,
    *,
    idle_reason: str = "available",
    summary: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "idle_notification",
        "from": agent_id,
        "timestamp": _now_iso(),
        "idleReason": idle_reason,
        "summary": summary,
    }


def create_permission_request_message(
    *,
    request_id: str,
    agent_id: str,
    tool_name: str,
    tool_use_id: str,
    description: str,
    input_data: dict[str, Any],
    permission_suggestions: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "permission_request",
        "request_id": request_id,
        "agent_id": agent_id,
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "description": description,
        "input": input_data,
        "permission_suggestions": permission_suggestions or [],
    }


def create_permission_response_message(
    *,
    request_id: str,
    subtype: Literal["success", "error"],
    error: str | None = None,
    updated_input: dict[str, Any] | None = None,
    permission_updates: list[Any] | None = None,
) -> dict[str, Any]:
    if subtype == "error":
        return {
            "type": "permission_response",
            "request_id": request_id,
            "subtype": "error",
            "error": error or "Permission denied",
        }
    return {
        "type": "permission_response",
        "request_id": request_id,
        "subtype": "success",
        "response": {
            "updated_input": updated_input,
            "permission_updates": permission_updates,
        },
    }


def create_plan_approval_request(
    *,
    from_agent: str,
    plan_file_path: str,
    plan_content: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "plan_approval_request",
        "from": from_agent,
        "timestamp": _now_iso(),
        "planFilePath": plan_file_path,
        "planContent": plan_content,
        "requestId": request_id or _gen_id("plan"),
    }


def create_plan_approval_response(
    *,
    request_id: str,
    approved: bool,
    feedback: str | None = None,
    permission_mode: str | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": "plan_approval_response",
        "requestId": request_id,
        "approved": approved,
        "timestamp": _now_iso(),
    }
    if feedback:
        msg["feedback"] = feedback
    if permission_mode:
        msg["permissionMode"] = permission_mode
    return msg


def create_shutdown_request(
    *,
    request_id: str,
    from_agent: str,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "shutdown_request",
        "requestId": request_id,
        "from": from_agent,
        "reason": reason,
        "timestamp": _now_iso(),
    }


def create_shutdown_approved(*, request_id: str, from_agent: str) -> dict[str, Any]:
    return {
        "type": "shutdown_approved",
        "requestId": request_id,
        "from": from_agent,
        "timestamp": _now_iso(),
    }


def create_shutdown_rejected(
    *, request_id: str, from_agent: str, reason: str
) -> dict[str, Any]:
    return {
        "type": "shutdown_rejected",
        "requestId": request_id,
        "from": from_agent,
        "reason": reason,
        "timestamp": _now_iso(),
    }


def create_task_assignment(
    *,
    task_id: str,
    subject: str,
    description: str,
    assigned_by: str,
) -> dict[str, Any]:
    return {
        "type": "task_assignment",
        "taskId": task_id,
        "subject": subject,
        "description": description,
        "assignedBy": assigned_by,
        "timestamp": _now_iso(),
    }


def create_team_permission_update(
    *,
    tool_name: str,
    directory_path: str,
    rules: list[dict[str, Any]],
    behavior: str = "allow",
) -> dict[str, Any]:
    return {
        "type": "team_permission_update",
        "permissionUpdate": {
            "type": "addRules",
            "rules": rules,
            "behavior": behavior,
            "destination": "session",
        },
        "directoryPath": directory_path,
        "toolName": tool_name,
    }


def create_mode_set_request(*, mode: str, from_agent: str) -> dict[str, Any]:
    return {
        "type": "mode_set_request",
        "mode": mode,
        "from": from_agent,
    }


def create_sandbox_permission_request(
    *,
    request_id: str,
    worker_id: str,
    worker_name: str,
    host: str,
    worker_color: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "sandbox_permission_request",
        "requestId": request_id,
        "workerId": worker_id,
        "workerName": worker_name,
        "workerColor": worker_color,
        "hostPattern": {"host": host},
        "createdAt": int(datetime.now(timezone.utc).timestamp() * 1000),
    }


def create_sandbox_permission_response(
    *, request_id: str, host: str, allow: bool
) -> dict[str, Any]:
    return {
        "type": "sandbox_permission_response",
        "requestId": request_id,
        "host": host,
        "allow": allow,
        "timestamp": _now_iso(),
    }


def create_teammate_terminated(*, agent_name: str, reason: str | None = None) -> dict[str, Any]:
    return {
        "type": "teammate_terminated",
        "agentName": agent_name,
        "reason": reason,
        "timestamp": _now_iso(),
    }
