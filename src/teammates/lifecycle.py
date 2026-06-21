"""Teammate lifecycle: idle notifications, shutdown protocol."""

from __future__ import annotations

import json
import uuid
from typing import Any

from teammates.constants import TEAM_LEAD_NAME
from teammates.mailbox import send_structured_message
from teammates.message_types import (
    create_shutdown_approved,
    create_shutdown_rejected,
    create_shutdown_request,
    create_teammate_terminated,
    parse_structured,
)
from teammates.team_helpers import deactivate_teammate, get_leader_name


def send_idle_notification(
    *,
    agent_name: str,
    team_name: str,
    summary: str | None = None,
    idle_reason: str = "available",
) -> None:
    from teammates.message_types import create_idle_notification

    payload = create_idle_notification(agent_name, idle_reason=idle_reason, summary=summary)
    leader = get_leader_name(team_name)
    send_structured_message(
        from_agent=agent_name,
        to_agent=leader,
        payload=payload,
        team_name=team_name,
    )


def send_shutdown_request(
    *,
    target_name: str,
    team_name: str,
    from_agent: str = TEAM_LEAD_NAME,
    reason: str | None = None,
) -> str:
    request_id = f"shutdown-{uuid.uuid4().hex[:12]}"
    payload = create_shutdown_request(
        request_id=request_id, from_agent=from_agent, reason=reason
    )
    send_structured_message(
        from_agent=from_agent,
        to_agent=target_name,
        payload=payload,
        team_name=team_name,
    )
    return request_id


def handle_shutdown_request(text: str, agent_name: str, team_name: str) -> bool:
    """Teammate received shutdown_request → reply shutdown_approved and return True."""
    parsed = parse_structured(text)
    if not parsed or parsed.get("type") != "shutdown_request":
        return False

    request_id = parsed.get("requestId") or parsed.get("request_id", "")
    leader = get_leader_name(team_name)
    payload = create_shutdown_approved(request_id=request_id, from_agent=agent_name)
    send_structured_message(
        from_agent=agent_name,
        to_agent=leader,
        payload=payload,
        team_name=team_name,
    )
    print(f"  \033[35m[protocol] {agent_name} approved shutdown ({request_id})\033[0m")
    return True


def reject_shutdown(
    *,
    request_id: str,
    agent_name: str,
    team_name: str,
    reason: str,
) -> None:
    leader = get_leader_name(team_name)
    payload = create_shutdown_rejected(
        request_id=request_id, from_agent=agent_name, reason=reason
    )
    send_structured_message(
        from_agent=agent_name,
        to_agent=leader,
        payload=payload,
        team_name=team_name,
    )


def notify_teammate_terminated(
    *,
    agent_name: str,
    team_name: str,
    reason: str | None = None,
) -> None:
    """Broadcast teammate_terminated to lead and remaining teammates."""
    from teammates.team_helpers import list_active_teammates

    payload = create_teammate_terminated(agent_name=agent_name, reason=reason)
    leader = get_leader_name(team_name)
    recipients = {leader} | {m.name for m in list_active_teammates(team_name)}
    for recipient in recipients:
        send_structured_message(
            from_agent="system",
            to_agent=recipient,
            payload=payload,
            team_name=team_name,
        )
    deactivate_teammate(team_name, agent_name)


def is_shutdown_message(text: str) -> dict[str, Any] | None:
    parsed = parse_structured(text)
    if parsed and parsed.get("type") == "shutdown_request":
        return parsed
    return None
