"""s16 protocol state machine — request_id correlation (aligned with CC teammateMailbox.ts)."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

ProtocolKind = Literal["shutdown", "plan_approval"]
ProtocolStatus = Literal["pending", "approved", "rejected"]

_lock = threading.Lock()
_pending_requests: dict[str, "ProtocolState"] = {}


@dataclass
class ProtocolState:
    request_id: str
    type: ProtocolKind
    sender: str
    target: str
    payload: str = ""
    status: ProtocolStatus = "pending"
    created_at: float = field(default_factory=time.time)


def new_request_id(prefix: str = "req") -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def register_request(state: ProtocolState) -> None:
    with _lock:
        _pending_requests[state.request_id] = state


def get_request(request_id: str) -> ProtocolState | None:
    with _lock:
        return _pending_requests.get(request_id)


def match_response(
    *,
    response_type: str,
    request_id: str,
    approved: bool,
) -> ProtocolState | None:
    """Correlate a response to the original request via request_id."""
    if not request_id:
        return None
    with _lock:
        state = _pending_requests.get(request_id)
        if state is None:
            return None
        expected = {
            "shutdown": {"shutdown_approved", "shutdown_rejected"},
            "plan_approval": {"plan_approval_response"},
        }.get(state.type, set())
        if expected and response_type not in expected:
            return None
        state.status = "approved" if approved else "rejected"
        return state


def format_plan_approval_injection(parsed: dict[str, Any]) -> str:
    req_id = parsed.get("requestId") or parsed.get("request_id", "")
    from_agent = parsed.get("from", "unknown")
    plan = parsed.get("planContent") or parsed.get("plan", "")
    path = parsed.get("planFilePath") or ""
    lines = [
        "[Plan approval request]",
        f"From: {from_agent}",
        f"request_id: {req_id}",
    ]
    if path:
        lines.append(f"Plan file: {path}")
    lines.append(f"Plan:\n{plan}")
    lines.append("Use review_plan(request_id, approve, feedback) to respond.")
    return "\n".join(lines)


def format_idle_notification_injection(parsed: dict[str, Any]) -> str:
    from_agent = parsed.get("from", "unknown")
    reason = parsed.get("idleReason") or parsed.get("idle_reason", "available")
    summary = parsed.get("summary") or ""
    text = f"[Teammate idle] {from_agent} is {reason}."
    if summary:
        text += f" Summary: {summary}"
    return text
