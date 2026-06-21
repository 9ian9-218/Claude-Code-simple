"""s17 autonomous teammate — idle poll, auto-claim, identity re-injection."""

from __future__ import annotations

import threading
import time
from typing import Any, Literal

from teammates.constants import (
    TEAMMATE_IDLE_POLL_INTERVAL,
    TEAMMATE_IDLE_TIMEOUT,
    TEAMMATE_IDENTITY_REINJECT_THRESHOLD,
)
from teammates.inbox_dispatch import dispatch_inbox_batch
from tasks import try_claim_next_task, load_task
from console_lock import locked_print

IdleResult = Literal["work", "shutdown", "timeout"]


def make_identity_block(name: str, role: str, team_name: str) -> str:
    return (
        f"<identity>You are teammate '{name}' on team '{team_name}', "
        f"role: {role}. Continue assigned work or claim tasks from the board.</identity>"
    )


def maybe_reinject_identity(
    messages: list[dict[str, Any]],
    *,
    name: str,
    role: str,
    team_name: str,
) -> None:
    """Re-inject identity after context compression thins history."""
    if len(messages) > TEAMMATE_IDENTITY_REINJECT_THRESHOLD:
        return
    block = make_identity_block(name, role, team_name)
    if messages and messages[0].get("role") == "system":
        if block in str(messages[1].get("content", "") if len(messages) > 1 else ""):
            return
        messages.insert(1, {"role": "user", "content": block})
    else:
        messages.insert(0, {"role": "user", "content": block})


def idle_poll(
    *,
    agent_name: str,
    team_name: str,
    messages: list[dict[str, Any]],
    shutdown_event: threading.Event,
    role: str = "",
) -> IdleResult:
    """
    Poll inbox + task board every TEAMMATE_IDLE_POLL_INTERVAL for up to TEAMMATE_IDLE_TIMEOUT.
    Returns 'work' to resume WORK phase, 'shutdown' on shutdown_request, 'timeout' when idle expires.
    """
    polls = max(1, int(TEAMMATE_IDLE_TIMEOUT / TEAMMATE_IDLE_POLL_INTERVAL))
    for _ in range(polls):
        if shutdown_event.is_set():
            return "shutdown"

        dispatch = dispatch_inbox_batch(
            agent_name=agent_name,
            team_name=team_name,
            messages=messages,
        )
        if dispatch.should_shutdown:
            shutdown_event.set()
            locked_print(f"  \033[35m[idle] {agent_name} approved shutdown\033[0m")
            return "shutdown"
        if dispatch.resume_work:
            locked_print(f"  \033[36m[idle] {agent_name} inbox → resume work\033[0m")
            return "work"

        result = try_claim_next_task(agent_name)
        if result.startswith("Claimed"):
            task_id = result.split()[1]
            task = load_task(task_id)
            messages.append({
                "role": "user",
                "content": (
                    f"<auto-claimed>Task {task.id}: {task.subject}\n"
                    f"{task.description}</auto-claimed>"
                ),
            })
            locked_print(f"  \033[32m[idle] {agent_name} auto-claimed: {task.subject}\033[0m")
            return "work"
        if result != "No unclaimed tasks available":
            locked_print(f"  \033[33m[idle] {agent_name} claim failed: {result}\033[0m")

        shutdown_event.wait(TEAMMATE_IDLE_POLL_INTERVAL)

    locked_print(f"  \033[31m[idle] {agent_name} timeout ({TEAMMATE_IDLE_TIMEOUT}s)\033[0m")
    return "timeout"
