"""Teammate swarm constants (aligned with CC swarm/constants.ts)."""

from config import TEAMS_DIR

TEAM_LEAD_NAME = "team-lead"
TEAMMATE_MESSAGE_TAG = "teammate-message"

# Agent colors for UI / mailbox metadata
AGENT_COLORS = (
    "blue",
    "green",
    "yellow",
    "purple",
    "orange",
    "pink",
    "cyan",
    "red",
)

# Poller intervals (seconds)
LEAD_INBOX_POLL_INTERVAL = 1.0
WORKER_PERMISSION_POLL_INTERVAL = 0.5

# File lock retries (proper-lockfile semantics)
LOCK_RETRIES = 10
LOCK_MIN_TIMEOUT_MS = 5
LOCK_MAX_TIMEOUT_MS = 100
