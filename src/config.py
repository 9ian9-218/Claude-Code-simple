"""Shared runtime config"""

from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（main.py 所在目录，与 src 同级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKDIR = Path.cwd()

load_dotenv(PROJECT_ROOT / ".env")
