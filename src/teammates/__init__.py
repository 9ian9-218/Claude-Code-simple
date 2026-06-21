"""Teammates package — CC-style file-based agent swarm."""

from teammates.context import AgentContext, agent_context, get_agent_context, reset_agent_context
from teammates.spawn import (
    spawn_teammate,
    list_active_teammate_names,
    request_teammate_shutdown,
    is_teammate_active,
)
from teammates.inbox_dispatch import dispatch_inbox_batch
from teammates.poller import (
    start_lead_inbox_poller,
    consume_pending_injections,
    consume_pending_idle_notifications,
)
from teammates.team_helpers import (
    create_team,
    read_team_config,
    list_active_teammates,
    ensure_teammate_for_spawn,
    reactivate_teammate,
)
from teammates.protocol import ProtocolState, match_response, register_request
from teammates.autonomous import idle_poll
from tasks import scan_unclaimed_tasks, try_claim_next_task

__all__ = [
    "AgentContext",
    "agent_context",
    "get_agent_context",
    "reset_agent_context",
    "spawn_teammate",
    "list_active_teammate_names",
    "request_teammate_shutdown",
    "is_teammate_active",
    "start_lead_inbox_poller",
    "consume_pending_injections",
    "consume_pending_idle_notifications",
    "create_team",
    "read_team_config",
    "list_active_teammates",
    "ensure_teammate_for_spawn",
    "reactivate_teammate",
    "ProtocolState",
    "match_response",
    "register_request",
    "scan_unclaimed_tasks",
    "try_claim_next_task",
    "idle_poll",
    "dispatch_inbox_batch",
]
