"""Spawn and run teammate agents in background threads."""

from __future__ import annotations

import threading
import time
from typing import Any

from teammates.constants import TEAM_LEAD_NAME
from teammates.context import AgentContext, agent_context
from teammates.lifecycle import handle_shutdown_request, send_idle_notification
from teammates.mailbox import (
    mark_messages_as_read,
    read_unread_messages,
    send_plain_message,
)
from teammates.message_types import format_teammate_messages, is_structured_protocol_message, parse_structured
from teammates.team_helpers import add_teammate, read_team_config

# Active teammate threads: name → {"thread": Thread, "shutdown": Event}
_active_teammates: dict[str, dict[str, Any]] = {}
_teammate_lock = threading.Lock()

# After initial work, poll inbox briefly for Lead follow-up, then stop.
TEAMMATE_IDLE_ROUNDS_AFTER_WORK = 2
TEAMMATE_IDLE_SLEEP_SEC = 0.5
TEAMMATE_IDLE_POLLS = 10


def _teammate_identity(name: str, role: str, team_name: str) -> str:
    return (
        f"You are teammate '{name}' on team '{team_name}', role: {role}. "
        f"Complete ONLY the assigned task in the initial prompt — do not expand scope, "
        f"self-review, or iterate unless the lead sends new inbox instructions. "
        f"When done: send_message a concise summary to '{TEAM_LEAD_NAME}', then stop. "
        f"You cannot spawn other teammates."
    )


def _notify_lead_teammate_update(name: str, team_name: str, summary: str) -> None:
    """Queue a high-priority notification for the Lead's next agent_loop turn."""
    from messageQueueManager import enqueue_pending_notification

    preview = summary.strip()
    if len(preview) > 800:
        preview = preview[:800] + "\n...(truncated)"
    enqueue_pending_notification(
        f"[Teammate update] {name}@{team_name}:\n{preview}",
        priority="next",
    )


def _process_teammate_inbox(
    *,
    name: str,
    team_name: str,
    inbox: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    shutdown_event: threading.Event,
) -> None:
    for entry in inbox:
        text = entry.get("text", "")
        if handle_shutdown_request(text, name, team_name):
            shutdown_event.set()
            break
        if is_structured_protocol_message(text):
            parsed = parse_structured(text)
            if parsed and parsed.get("type") in (
                "permission_response",
                "task_assignment",
                "plan_approval_response",
                "team_permission_update",
                "mode_set_request",
                "sandbox_permission_response",
            ):
                messages.append({
                    "role": "user",
                    "content": f"[System message]\n{text}",
                })
    plain = format_teammate_messages(inbox)
    if plain:
        messages.append({"role": "user", "content": plain})
    mark_messages_as_read(name, team_name)


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

    with agent_context(ctx):
        system = _teammate_identity(name, role, team_name) + "\n\n" + build_subagent_system(SKILL_CATALOG)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": initial_prompt},
        ]

        initial_work_done = False
        idle_after_work = 0

        while not shutdown_event.is_set():
            inbox = read_unread_messages(name, team_name)
            if inbox:
                _process_teammate_inbox(
                    name=name,
                    team_name=team_name,
                    inbox=inbox,
                    messages=messages,
                    shutdown_event=shutdown_event,
                )
                initial_work_done = False
                idle_after_work = 0

            if shutdown_event.is_set():
                break

            if initial_work_done and not inbox:
                idle_after_work += 1
                if idle_after_work > TEAMMATE_IDLE_ROUNDS_AFTER_WORK:
                    break
                for _ in range(TEAMMATE_IDLE_POLLS):
                    if shutdown_event.is_set():
                        break
                    time.sleep(TEAMMATE_IDLE_SLEEP_SEC)
                continue

            result = agent_loop(
                messages,
                max_turn=15,
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
                _notify_lead_teammate_update(name, team_name, result)
                print(
                    f"  \033[36m[{name}]\033[0m task done — "
                    f"report queued for lead"
                )

            send_idle_notification(agent_name=name, team_name=team_name)
            initial_work_done = True
            idle_after_work = 0

    with _teammate_lock:
        _active_teammates.pop(name, None)
    print(f"  \033[32m[teammate] {name} stopped\033[0m")


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

    with _teammate_lock:
        if name in _active_teammates:
            return f"Teammate '{name}' already active"

    try:
        member = add_teammate(team_name, name, agent_type=agent_type)
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
        name=f"teammate-{name}",
    )

    with _teammate_lock:
        _active_teammates[name] = {"thread": thread, "shutdown": shutdown_event}

    thread.start()
    print(f"  \033[36m[teammate] {name} spawned ({member.color}) on team '{team_name}'\033[0m")
    return (
        f"Teammate '{name}' spawned as {role} (color: {member.color}). "
        f"They will report via inbox when done."
    )


def request_teammate_shutdown(name: str, team_name: str, reason: str | None = None) -> str:
    from teammates.lifecycle import send_shutdown_request

    with _teammate_lock:
        if name not in _active_teammates:
            return f"Teammate '{name}' is not active"
    request_id = send_shutdown_request(target_name=name, team_name=team_name, reason=reason)
    return f"Shutdown request {request_id} sent to '{name}'"


def list_active_teammate_names() -> list[str]:
    with _teammate_lock:
        return list(_active_teammates.keys())
