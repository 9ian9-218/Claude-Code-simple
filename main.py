from agent_loop import agent_loop
from hook import trigger_hooks

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
        trigger_hooks("Stop", messages)


if __name__ == "__main__":
    main()