"""
File-based teammate mailbox (~/.claude/teams/{team}/inboxes/{agent}.json).

Each inbox is a JSON array. Writes use file locking with up to 10 retries
(proper-lockfile semantics via fcntl on Linux).
"""

from __future__ import annotations

import json
import random
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from teammates.constants import (
    LOCK_MAX_TIMEOUT_MS,
    LOCK_MIN_TIMEOUT_MS,
    LOCK_RETRIES,
    TEAMS_DIR,
)


def sanitize_path_component(name: str) -> str:
    safe = re.sub(r"[^\w\-.@]+", "-", name.strip())
    return safe or "agent"


def get_inbox_path(agent_name: str, team_name: str) -> Path:
    safe_team = sanitize_path_component(team_name)
    safe_agent = sanitize_path_component(agent_name)
    return TEAMS_DIR / safe_team / "inboxes" / f"{safe_agent}.json"


def ensure_inbox_dir(team_name: str) -> Path:
    inbox_dir = TEAMS_DIR / sanitize_path_component(team_name) / "inboxes"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    return inbox_dir


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Advisory lock with exponential backoff (max LOCK_RETRIES attempts)."""
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    last_err: OSError | None = None
    for attempt in range(LOCK_RETRIES):
        fh = lock_path.open("a+")
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                last_err = e
                fh.close()
                delay_ms = min(
                    LOCK_MAX_TIMEOUT_MS,
                    LOCK_MIN_TIMEOUT_MS * (2 ** attempt) + random.randint(0, 10),
                )
                time.sleep(delay_ms / 1000.0)
                continue
            yield
            return
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()
    raise TimeoutError(f"Could not acquire lock {lock_path} after {LOCK_RETRIES} retries") from last_err


def read_mailbox(agent_name: str, team_name: str) -> list[dict[str, Any]]:
    inbox_path = get_inbox_path(agent_name, team_name)
    if not inbox_path.exists():
        return []
    try:
        content = inbox_path.read_text(encoding="utf-8")
        messages = json.loads(content)
        return messages if isinstance(messages, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def read_unread_messages(agent_name: str, team_name: str) -> list[dict[str, Any]]:
    return [m for m in read_mailbox(agent_name, team_name) if not m.get("read")]


def write_to_mailbox(
    recipient_name: str,
    message: dict[str, Any],
    team_name: str,
) -> None:
    """Append a message to recipient inbox (read→append→write under lock)."""
    ensure_inbox_dir(team_name)
    inbox_path = get_inbox_path(recipient_name, team_name)
    lock_path = inbox_path.with_suffix(inbox_path.suffix + ".lock")

    if not inbox_path.exists():
        try:
            with inbox_path.open("x", encoding="utf-8") as fh:
                fh.write("[]")
        except FileExistsError:
            pass

    with _file_lock(lock_path):
        messages = read_mailbox(recipient_name, team_name)
        new_message = {
            **message,
            "read": False,
            "timestamp": message.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        }
        messages.append(new_message)
        inbox_path.write_text(json.dumps(messages, indent=2, ensure_ascii=False), encoding="utf-8")


def mark_messages_as_read(agent_name: str, team_name: str) -> None:
    inbox_path = get_inbox_path(agent_name, team_name)
    lock_path = inbox_path.with_suffix(inbox_path.suffix + ".lock")
    if not inbox_path.exists():
        return
    with _file_lock(lock_path):
        messages = read_mailbox(agent_name, team_name)
        if not messages:
            return
        for m in messages:
            m["read"] = True
        inbox_path.write_text(json.dumps(messages, indent=2, ensure_ascii=False), encoding="utf-8")


def mark_message_as_read_by_index(
    agent_name: str, team_name: str, message_index: int
) -> None:
    inbox_path = get_inbox_path(agent_name, team_name)
    lock_path = inbox_path.with_suffix(inbox_path.suffix + ".lock")
    if not inbox_path.exists():
        return
    with _file_lock(lock_path):
        messages = read_mailbox(agent_name, team_name)
        if message_index < 0 or message_index >= len(messages):
            return
        messages[message_index]["read"] = True
        inbox_path.write_text(json.dumps(messages, indent=2, ensure_ascii=False), encoding="utf-8")


def clear_mailbox(agent_name: str, team_name: str) -> None:
    inbox_path = get_inbox_path(agent_name, team_name)
    if inbox_path.exists():
        inbox_path.write_text("[]", encoding="utf-8")


def send_plain_message(
    *,
    from_agent: str,
    to_agent: str,
    text: str,
    team_name: str,
    color: str | None = None,
    summary: str | None = None,
) -> None:
    write_to_mailbox(
        to_agent,
        {
            "from": from_agent,
            "text": text,
            "color": color,
            "summary": summary,
        },
        team_name,
    )


def send_structured_message(
    *,
    from_agent: str,
    to_agent: str,
    payload: dict[str, Any],
    team_name: str,
    color: str | None = None,
) -> None:
    write_to_mailbox(
        to_agent,
        {
            "from": from_agent,
            "text": json.dumps(payload, ensure_ascii=False),
            "color": color,
        },
        team_name,
    )
