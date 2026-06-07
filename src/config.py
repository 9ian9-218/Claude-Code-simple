"""Shared runtime config"""

from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（main.py 所在目录，与 src 同级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKDIR = Path.cwd()

# 项目内 Claude 运行时数据（teams / memory / tasks / skills）
CLAUDE_DIR = PROJECT_ROOT / ".claude"
TEAMS_DIR = CLAUDE_DIR / "teams"
MEMORY_DIR = CLAUDE_DIR / "memory"
TASKS_DIR = CLAUDE_DIR / "tasks"
SKILLS_DIR = CLAUDE_DIR / "skills"


def ensure_claude_dirs() -> None:
    for path in (CLAUDE_DIR, TEAMS_DIR, MEMORY_DIR, TASKS_DIR, SKILLS_DIR):
        path.mkdir(parents=True, exist_ok=True)


ensure_claude_dirs()

load_dotenv(PROJECT_ROOT / ".env")
