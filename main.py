"""入口：将 src 加入模块搜索路径后导入应用代码。"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from src.agent_loop import agent_loop
from src.hook import trigger_hooks

def main():
    messages = []
    while True:
        try:
            query = input("\033[36mUser >\t \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("/new", "/n"):
            messages = []
            print("=" * 50)
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