"""Teammates package — CC-style file-based agent swarm."""

from teammates.context import AgentContext, agent_context, get_agent_context, reset_agent_context
from teammates.spawn import spawn_teammate, list_active_teammate_names, request_teammate_shutdown
from teammates.poller import start_lead_inbox_poller, consume_pending_injections
from teammates.team_helpers import create_team, read_team_config, list_active_teammates

__all__ = [
    "AgentContext",
    "agent_context",
    "get_agent_context",
    "reset_agent_context",
    "spawn_teammate",
    "list_active_teammate_names",
    "request_teammate_shutdown",
    "start_lead_inbox_poller",
    "consume_pending_injections",
    "create_team",
    "read_team_config",
    "list_active_teammates",
]
