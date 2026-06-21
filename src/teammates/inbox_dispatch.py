"""Unified teammate inbox dispatch — structured protocol + plain XML."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from teammates.lifecycle import handle_shutdown_request
from teammates.mailbox import read_mailbox
from teammates.message_types import (
    format_teammate_messages,
    is_structured_protocol_message,
    parse_structured,
)


@dataclass
class DispatchResult:
    should_shutdown: bool = False
    resume_work: bool = False
    read_indices: list[int] | None = None


def _format_task_assignment(parsed: dict[str, Any]) -> str:
    task_id = parsed.get("taskId") or parsed.get("task_id", "")
    subject = parsed.get("subject", "")
    description = parsed.get("description", "")
    assigned_by = parsed.get("assignedBy") or parsed.get("assigned_by", "lead")
    lines = [f"[Task assignment from {assigned_by}]"]
    if task_id:
        lines.append(f"Task ID: {task_id}")
    if subject:
        lines.append(f"Subject: {subject}")
    if description:
        lines.append(f"Description:\n{description}")
    return "\n".join(lines)


def _format_plan_approval_response(parsed: dict[str, Any]) -> str:
    approved = parsed.get("approved", False)
    feedback = parsed.get("feedback") or ""
    if approved:
        text = "[Plan approved] Proceed with the assigned work."
        if feedback:
            text += f"\nFeedback: {feedback}"
        return text
    text = "[Plan rejected]"
    if feedback:
        text += f" Feedback: {feedback}"
    else:
        text += " Revise your plan and resubmit via send_message(message_type=plan_approval)."
    return text


def _inject_structured(parsed: dict[str, Any], messages: list[dict[str, Any]]) -> bool:
    """Inject structured message into teammate context. Returns True if handled."""
    msg_type = parsed.get("type")
    if msg_type == "task_assignment":
        messages.append({"role": "user", "content": _format_task_assignment(parsed)})
        return True
    if msg_type == "plan_approval_response":
        messages.append({"role": "user", "content": _format_plan_approval_response(parsed)})
        return True
    if msg_type in (
        "permission_response",
        "team_permission_update",
        "mode_set_request",
        "sandbox_permission_response",
    ):
        messages.append({"role": "user", "content": f"[System message]\n{parsed}"})
        return True
    return False


def dispatch_inbox_batch(
    *,
    agent_name: str,
    team_name: str,
    messages: list[dict[str, Any]],
) -> DispatchResult:
    """
    Process unread mailbox entries for a teammate.
    Marks processed entries as read by index; returns shutdown / resume-work signals.
    """
    from teammates.mailbox import mark_message_as_read_by_index

    mailbox = read_mailbox(agent_name, team_name)
    plain_batch: list[dict[str, Any]] = []
    read_indices: list[int] = []
    result = DispatchResult()

    for i, entry in enumerate(mailbox):
        if entry.get("read"):
            continue
        text = entry.get("text", "")

        if handle_shutdown_request(text, agent_name, team_name):
            read_indices.append(i)
            result.should_shutdown = True
            break

        if is_structured_protocol_message(text):
            parsed = parse_structured(text)
            if parsed:
                if _inject_structured(parsed, messages):
                    read_indices.append(i)
                    result.resume_work = True
                    continue
                # Unknown structured — mark read to avoid stuck loop
                read_indices.append(i)
                continue

        plain_batch.append(entry)
        read_indices.append(i)

    if plain_batch:
        formatted = format_teammate_messages(plain_batch)
        if formatted:
            messages.append({"role": "user", "content": formatted})
            result.resume_work = True

    for idx in read_indices:
        mark_message_as_read_by_index(agent_name, team_name, idx)

    result.read_indices = read_indices
    return result
