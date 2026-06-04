import os
import re
import subprocess
import threading
import time
from types import SimpleNamespace

from hook import trigger_hooks
from messageQueueManager import enqueue_pending_notification
from tool import execute_tool_call

_bg_counter = 0
_bg_lock = threading.Lock()

# Stall watchdog (aligned with LocalShellTask.tsx L24-26)
STALL_CHECK_INTERVAL_S = 5
STALL_THRESHOLD_S = 45
STALL_TAIL_BYTES = 1024

# Last-line patterns suggesting a command is blocked on keyboard input (L28-38)
_PROMPT_PATTERNS = [
    re.compile(r"\(y/n\)", re.I),
    re.compile(r"\[y/n\]", re.I),
    re.compile(r"\(yes/no\)", re.I),
    re.compile(r"\b(?:Do you|Would you|Shall I|Are you sure|Ready to)\b.*\? *$", re.I),
    re.compile(r"Press (any key|Enter)", re.I),
    re.compile(r"Continue\?", re.I),
    re.compile(r"Overwrite\?", re.I),
]


def _looks_like_prompt(tail: str) -> bool:
    last_line = tail.rstrip().split("\n")[-1] if tail else ""
    return any(p.search(last_line) for p in _PROMPT_PATTERNS)


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """
    判断某个工具调用（主要是 shell 命令）是否属于“慢操作”，即可能需要 30 秒以上才能完成。
    这个函数主要用于自动判断哪些命令适合放到后台执行，避免前端阻塞等待。
    参数:
        tool_name (str): 工具名，只有 "run_bash" 时才会进一步判断。
        tool_input (dict): 工具的输入参数，主要用于提取 shell 命令内容。
    返回:
        bool: 如果识别为慢操作，则返回 True，否则为 False。
    逻辑说明:
    1. 只针对 "run_bash" 工具（shell 命令），其他工具直接返回 False。
    2. 获取命令文本，全部转为小写，便于关键词匹配。
    3. 定义了一组典型慢操作的关键词（如 install、build、test、deploy、compile 等构建/测试/安装相关）；
       以及常见的“慢命令”组合（docker build、pip install、npm install 等）。
    4. 只要命令字符串包含任何一个慢关键词，就认定为慢操作，返回 True。
    """
    if tool_name != "run_bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = [
        "install",       # 软件安装常常较慢
        "build",         # 构建/编译通常较慢
        "test",          # 大型测试耗时长
        "deploy",        # 部署脚本依赖/环境复杂
        "compile",       # 源码编译
        "docker build",  # 容器镜像构建
        "pip install",   # Python 依赖安装
        "npm install",   # Node 依赖安装
        "cargo build",   # Rust 构建
        "pytest",        # 单元/集成测试
        "make"           # makefile 通常是复杂构建
    ]
    return any(kw in cmd for kw in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """Explicit run_in_background wins; heuristic only when the flag is absent."""
    if "run_in_background" in tool_input:
        return bool(tool_input["run_in_background"])
    return is_slow_operation(tool_name, tool_input)


def _enqueue_stall_notification(
    bg_id: str, command: str, tool_use_id: str | None, tail: str,
    *, recipient: str | None = None,
) -> None:
    """One-shot stall notification (no <status> — CC treats unknown status as terminal)."""
    summary = f'Background command "{command}" appears to be waiting for interactive input'
    tool_use_line = f"  <tool_use_id>{tool_use_id}</tool_use_id>\n" if tool_use_id else ""
    message = (
        f"<task_notification>\n"
        f"  <task_id>{bg_id}</task_id>\n"
        f"{tool_use_line}"
        f"  <summary>{summary}</summary>\n"
        f"\nLast output:\n{tail.rstrip()}\n\n"
        f"The command is likely blocked on an interactive prompt. Kill this task and re-run "
        f"with piped input (e.g., `echo y | command`) or a non-interactive flag if one exists."
    )
    enqueue_pending_notification(message, priority="next", recipient=recipient)


def _run_bash_with_exit_code(
    command: str, *, bg_id: str = "", tool_use_id: str | None = None,
    recipient: str | None = None,
) -> tuple[str, int]:
    """Execute bash with streaming output and a stall watchdog."""
    try:
        proc = subprocess.Popen(
            command, shell=True, cwd=os.getcwd(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}", 1

    output_chunks: list[str] = []
    lock = threading.Lock()
    last_growth = [time.time()]
    last_size = [0]
    watchdog_stopped = threading.Event()
    stall_notified = [False]

    def reader():
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                with lock:
                    output_chunks.append(chunk)
                    last_growth[0] = time.time()
        finally:
            if proc.stdout:
                proc.stdout.close()

    def watchdog():
        while not watchdog_stopped.wait(STALL_CHECK_INTERVAL_S):
            if proc.poll() is not None:
                break
            with lock:
                current_size = sum(len(c) for c in output_chunks)
                if current_size > last_size[0]:
                    last_size[0] = current_size
                    last_growth[0] = time.time()
                    continue
                if time.time() - last_growth[0] < STALL_THRESHOLD_S:
                    continue
                tail = "".join(output_chunks)[-STALL_TAIL_BYTES:]
                if not _looks_like_prompt(tail):
                    # Not a prompt — reset so the next check is 45s out.
                    last_growth[0] = time.time()
                    continue
                if stall_notified[0]:
                    break
                stall_notified[0] = True
                _enqueue_stall_notification(bg_id, command, tool_use_id, tail, recipient=recipient)
                print(f"  \033[33m[stall watchdog] {bg_id}: "
                      f"interactive prompt detected\033[0m")
            break

    reader_t = threading.Thread(target=reader, daemon=True)
    watchdog_t = threading.Thread(target=watchdog, daemon=True)
    reader_t.start()
    watchdog_t.start()
    reader_t.join()
    exit_code = proc.wait()
    watchdog_stopped.set()
    watchdog_t.join(timeout=1)

    out = "".join(output_chunks).strip()
    output = out[:50000] if out else "(no output)"
    return output, exit_code


_COMPLETION_OUTPUT_PREVIEW = 2000


def _build_completion_summary(tool_name: str, command: str, exit_code: int) -> str:
    if tool_name == "run_bash" and command:
        return f'Background command "{command}" completed (exit code {exit_code})'
    return f"Background {tool_name} completed (exit code {exit_code})"


def _enqueue_completion_notification(
    bg_id: str,
    summary: str,
    output: str,
    tool_use_id: str | None = None,
    *,
    recipient: str | None = None,
) -> None:
    """Completion payload injected into messages — include command output, not just summary."""
    if len(output) <= _COMPLETION_OUTPUT_PREVIEW:
        preview = output
    else:
        omitted = len(output) - _COMPLETION_OUTPUT_PREVIEW
        preview = f"{output[:_COMPLETION_OUTPUT_PREVIEW]}\n... ({omitted} more chars)"
    tool_use_line = f"  <tool_use_id>{tool_use_id}</tool_use_id>\n" if tool_use_id else ""
    message = (
        f"<task_notification>\n"
        f"  <task_id>{bg_id}</task_id>\n"
        f"  <status>completed</status>\n"
        f"  <summary>{summary}</summary>\n"
        f"{tool_use_line}"
        f"\nOutput:\n{preview}\n"
        f"</task_notification>"
    )
    enqueue_pending_notification(message, priority="later", recipient=recipient)


def start_background_task(tool_call, args: dict) -> str:
    """Run tool in a daemon thread. Returns background task ID."""
    global _bg_counter
    with _bg_lock:
        _bg_counter += 1
        bg_id = f"bg_{_bg_counter:04d}"

    tool_name = tool_call.function.name
    command = args.get("command", "")
    block = SimpleNamespace(name=tool_name, input=args)

    from teammates.context import get_agent_context
    ctx = get_agent_context()
    recipient = ctx.agent_name if ctx.is_teammate else None

    def worker():
        if tool_name == "run_bash":
            output, exit_code = _run_bash_with_exit_code(
                command, bg_id=bg_id, tool_use_id=tool_call.id, recipient=recipient,
            )
        else:
            output = execute_tool_call(tool_call, args=args)
            exit_code = 0 if not str(output).startswith("Error") else 1

        trigger_hooks("PostToolUse", block, output)
        summary = _build_completion_summary(tool_name, command, exit_code)
        _enqueue_completion_notification(
            bg_id, summary, str(output), tool_use_id=tool_call.id, recipient=recipient,
        )
        print(f"  \033[32m[background done] {bg_id}: "
              f"{command[:40] or tool_name} (exit code {exit_code})\033[0m")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    print(f"  \033[33m[background] dispatched {bg_id}: "
          f"{command[:40] or tool_name}\033[0m")
    return bg_id
