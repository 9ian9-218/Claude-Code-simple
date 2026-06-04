"""入口：将 src 加入模块搜索路径后导入应用代码。"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_loop import agent_loop
from hook import trigger_hooks
from teammates.context import get_agent_context, reset_agent_context
from teammates.team_helpers import create_team, read_team_config
from teammates.poller import start_lead_inbox_poller
from teammates.constants import TEAM_LEAD_NAME
from permission_sync import process_pending_lead_permissions

DEFAULT_TEAM = "default"


def _lead_team_name() -> str:
    ctx = get_agent_context()
    return ctx.team_name or DEFAULT_TEAM


def _init_lead_team() -> None:
    """Ensure default team exists and start lead inbox poller."""
    reset_agent_context()
    ctx = get_agent_context()
    ctx.agent_name = TEAM_LEAD_NAME
    ctx.role = "lead"
    if read_team_config(DEFAULT_TEAM) is None:
        create_team(DEFAULT_TEAM, lead_name=TEAM_LEAD_NAME)
    ctx.team_name = DEFAULT_TEAM
    start_lead_inbox_poller(DEFAULT_TEAM)


def main():
    _init_lead_team()
    messages = []
    while True:
        process_pending_lead_permissions(_lead_team_name())
        try:
            query = input("\033[36mUser >\t \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("/new", "/n"):
            messages = []
            print("=" * 50)
            process_pending_lead_permissions(_lead_team_name())
            try:
                query = input("\033[36mUser >\t \033[0m")
            except (EOFError, KeyboardInterrupt):
                break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)
        messages.append({"role": "user", "content": query})
        agent_loop(messages)


if __name__ == "__main__":
    main()