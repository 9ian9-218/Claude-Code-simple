"""Team config CRUD (~/.claude/teams/{teamName}/config.json)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from teammates.constants import AGENT_COLORS, TEAMS_DIR, TEAM_LEAD_NAME
from teammates.mailbox import sanitize_path_component


@dataclass
class TeamMember:
    agentId: str
    name: str
    agentType: str = "general-purpose"
    color: str = "blue"
    isActive: bool = True
    joinedAt: str = ""
    model: str | None = None

    def __post_init__(self) -> None:
        if not self.joinedAt:
            self.joinedAt = datetime.now(timezone.utc).isoformat()


@dataclass
class TeamConfig:
    name: str
    leadAgentId: str
    members: list[TeamMember] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "leadAgentId": self.leadAgentId,
            "members": [asdict(m) for m in self.members],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TeamConfig:
        members = [TeamMember(**m) for m in data.get("members", [])]
        return cls(
            name=data["name"],
            leadAgentId=data["leadAgentId"],
            members=members,
        )


def get_team_dir(team_name: str) -> Path:
    return TEAMS_DIR / sanitize_path_component(team_name)


def get_team_config_path(team_name: str) -> Path:
    return get_team_dir(team_name) / "config.json"


def read_team_config(team_name: str) -> TeamConfig | None:
    path = get_team_config_path(team_name)
    if not path.exists():
        return None
    try:
        return TeamConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def write_team_config(config: TeamConfig) -> None:
    team_dir = get_team_dir(config.name)
    team_dir.mkdir(parents=True, exist_ok=True)
    (team_dir / "inboxes").mkdir(exist_ok=True)
    get_team_config_path(config.name).write_text(
        json.dumps(config.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def create_team(team_name: str, lead_name: str = TEAM_LEAD_NAME) -> TeamConfig:
    lead_id = f"{lead_name}@{team_name}"
    config = TeamConfig(
        name=team_name,
        leadAgentId=lead_id,
        members=[
            TeamMember(
                agentId=lead_id,
                name=lead_name,
                agentType="lead",
                color="white",
                isActive=True,
            )
        ],
    )
    write_team_config(config)
    return config


def get_leader_name(team_name: str) -> str:
    config = read_team_config(team_name)
    if not config:
        return TEAM_LEAD_NAME
    for member in config.members:
        if member.agentId == config.leadAgentId:
            return member.name
    return TEAM_LEAD_NAME


def get_next_color(team_name: str) -> str:
    config = read_team_config(team_name)
    used = {m.color for m in (config.members if config else [])}
    for color in AGENT_COLORS:
        if color not in used:
            return color
    return AGENT_COLORS[len(used) % len(AGENT_COLORS)]


def add_teammate(
    team_name: str,
    name: str,
    agent_type: str = "general-purpose",
    color: str | None = None,
) -> TeamMember:
    config = read_team_config(team_name)
    if config is None:
        raise ValueError(f"Team '{team_name}' not found")

    if any(m.name == name for m in config.members):
        raise ValueError(f"Teammate '{name}' already exists in team '{team_name}'")

    member = TeamMember(
        agentId=f"{name}@{team_name}",
        name=name,
        agentType=agent_type,
        color=color or get_next_color(team_name),
        isActive=True,
    )
    config.members.append(member)
    write_team_config(config)
    return member


def deactivate_teammate(team_name: str, name: str) -> None:
    config = read_team_config(team_name)
    if config is None:
        return
    for member in config.members:
        if member.name == name:
            member.isActive = False
    write_team_config(config)


def list_active_teammates(team_name: str) -> list[TeamMember]:
    config = read_team_config(team_name)
    if config is None:
        return []
    return [
        m for m in config.members
        if m.isActive and m.agentId != config.leadAgentId
    ]
