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

# Autonomous teammate idle phase (aligned with s17 / CC inProcessRunner)
TEAMMATE_IDLE_POLL_INTERVAL = 5.0
TEAMMATE_IDLE_TIMEOUT = 60.0
TEAMMATE_WORK_MAX_TURNS = 15
TEAMMATE_IDENTITY_REINJECT_THRESHOLD = 3

# File lock retries (proper-lockfile semantics)
LOCK_RETRIES = 10
LOCK_MIN_TIMEOUT_MS = 5
LOCK_MAX_TIMEOUT_MS = 100
