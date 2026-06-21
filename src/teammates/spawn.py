"""Spawn and run teammate agents — WORK → IDLE → SHUTDOWN (s16/s17 + CC inProcessRunner)."""

from __future__ import annotations

import threading
from typing import Any

from teammates.autonomous import idle_poll, maybe_reinject_identity
from teammates.constants import TEAM_LEAD_NAME, TEAMMATE_WORK_MAX_TURNS
from teammates.context import AgentContext, agent_context
from teammates.inbox_dispatch import dispatch_inbox_batch
from teammates.lifecycle import notify_teammate_terminated, send_idle_notification
from teammates.mailbox import send_plain_message
from teammates.team_helpers import ensure_teammate_for_spawn, read_team_config
from console_lock import locked_print

# Active threads keyed by (team_name, agent_name)
_active_teammates: dict[tuple[str, str], dict[str, Any]] = {}
_teammate_lock = threading.Lock()


def _teammate_key(team_name: str, name: str) -> tuple[str, str]:
    return (team_name, name)


def is_teammate_active(team_name: str, name: str) -> bool:
    with _teammate_lock:
        return _teammate_key(team_name, name) in _active_teammates


def _teammate_identity(name: str, role: str, team_name: str) -> str:
    return (
        f"You are teammate '{name}' on team '{team_name}', role: {role}. "
        f"Complete assigned work; use list_tasks / claim_task / complete_task on the shared board. "
        f"When idle, unclaimed tasks may be auto-assigned to you. "
        f"Submit plans via send_message(message_type=plan_approval) before major changes. "
        f"When done with a unit of work: send_message a concise summary to '{TEAM_LEAD_NAME}'. "
        f"You cannot spawn other teammates."
    )


def _run_teammate_loop(
    *,
    name: str,
    role: str,
    team_name: str,
    color: str,
    initial_prompt: str,
    shutdown_event: threading.Event,
) -> None:
    from agent_loop import agent_loop
    from loop_options import LoopOptions
    from prompt import build_subagent_system
    from skill_load import SKILL_CATALOG

    ctx = AgentContext(
        team_name=team_name,
        agent_name=name,
        agent_id=f"{name}@{team_name}",
        color=color,
        role="teammate",
        agent_type=role,
    )

    try:
        with agent_context(ctx):
            system = _teammate_identity(name, role, team_name) + "\n\n" + build_subagent_system(SKILL_CATALOG)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system},
                {"role": "user", "content": initial_prompt},
            ]

            while not shutdown_event.is_set():
                dispatch = dispatch_inbox_batch(
                    agent_name=name,
                    team_name=team_name,
                    messages=messages,
                )
                if dispatch.should_shutdown:
                    break

                maybe_reinject_identity(
                    messages, name=name, role=role, team_name=team_name,
                )

                result = agent_loop(
                    messages,
                    max_turn=TEAMMATE_WORK_MAX_TURNS,
                    max_tokens=6000,
                    loop_options=LoopOptions.teammate(),
                )
                if result:
                    send_plain_message(
                        from_agent=name,
                        to_agent=TEAM_LEAD_NAME,
                        text=result,
                        team_name=team_name,
                        color=color,
                    )
                    locked_print(
                        f"  \033[36m[{name}]\033[0m work round done — "
                        f"report sent to lead inbox"
                    )

                send_idle_notification(agent_name=name, team_name=team_name)

                idle_result = idle_poll(
                    agent_name=name,
                    team_name=team_name,
                    messages=messages,
                    shutdown_event=shutdown_event,
                    role=role,
                )
                if idle_result in ("shutdown", "timeout"):
                    break
                # idle_result == "work" → next outer loop iteration
    finally:
        notify_teammate_terminated(agent_name=name, team_name=team_name)
        with _teammate_lock:
            _active_teammates.pop(_teammate_key(team_name, name), None)
        locked_print(f"  \033[32m[teammate] {name} stopped\033[0m")


def spawn_teammate(
    *,
    name: str,
    role: str,
    prompt: str,
    team_name: str,
    agent_type: str = "general-purpose",
) -> str:
    """Spawn a teammate in a background thread (CC spawnMultiAgent in-process backend)."""
    if read_team_config(team_name) is None:
        return f"Error: team '{team_name}' not found. Use create_team first."

    key = _teammate_key(team_name, name)
    with _teammate_lock:
        if key in _active_teammates:
            return f"Teammate '{name}' already active on team '{team_name}'"

    try:
        member = ensure_teammate_for_spawn(team_name, name, agent_type=agent_type)
    except ValueError as e:
        return f"Error: {e}"

    shutdown_event = threading.Event()
    thread = threading.Thread(
        target=_run_teammate_loop,
        kwargs={
            "name": name,
            "role": role,
            "team_name": team_name,
            "color": member.color,
            "initial_prompt": prompt,
            "shutdown_event": shutdown_event,
        },
        daemon=True,
        name=f"teammate-{team_name}-{name}",
    )

    with _teammate_lock:
        if key in _active_teammates:
            return f"Teammate '{name}' already active on team '{team_name}'"
        _active_teammates[key] = {"thread": thread, "shutdown": shutdown_event}

    thread.start()
    locked_print(f"  \033[36m[teammate] {name} spawned ({member.color}) on team '{team_name}'\033[0m")
    return (
        f"Teammate '{name}' spawned as {role} (color: {member.color}). "
        f"Autonomous idle polling enabled — results arrive via lead inbox."
    )


def request_teammate_shutdown(name: str, team_name: str, reason: str | None = None) -> str:
    from teammates.context import get_agent_context
    from teammates.lifecycle import send_shutdown_request
    from teammates.protocol import ProtocolState, register_request
    from teammates.team_helpers import get_leader_name

    key = _teammate_key(team_name, name)
    with _teammate_lock:
        if key not in _active_teammates:
            return f"Teammate '{name}' is not active on team '{team_name}'"

    ctx = get_agent_context()
    leader = get_leader_name(team_name)
    request_id = send_shutdown_request(
        target_name=name,
        team_name=team_name,
        from_agent=ctx.agent_name or leader,
        reason=reason,
    )
    register_request(ProtocolState(
        request_id=request_id,
        type="shutdown",
        sender=leader,
        target=name,
        payload=reason or "",
    ))
    return f"Shutdown request {request_id} sent to '{name}'"


def list_active_teammate_names(team_name: str | None = None) -> list[str]:
    with _teammate_lock:
        if team_name is None:
            return [name for (_, name) in _active_teammates]
        return [name for (t, name) in _active_teammates if t == team_name]
