from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

from config import WORKDIR

# ── Task System ──

TASKS_DIR = WORKDIR / ".tasks"
HIGHWATERMARK_FILE = TASKS_DIR / ".highwatermark"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None    # Agent name (multi-agent scenarios)
    blockedBy: list[str] # Dependency task IDs


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def parse_task_num(task_id: str) -> int | None:
    """从 task_42 解析数字序号；无法解析时返回 None。"""
    if not task_id.startswith("task_"):
        return None
    try:
        return int(task_id.split("_", 1)[1])
    except ValueError:
        return None


def allocate_task_id() -> str:
    """
    顺序 ID + highwatermark（对齐 CC tasks.ts）。
    - 新 ID = max(文件中的 highwatermark, 现存 task 文件最大序号) + 1
    - 写入 highwatermark 后再创建任务，即使任务 JSON 被删也不会重用旧 ID
    """
    # 1. 读取 highwatermark
    if not HIGHWATERMARK_FILE.exists():
        highwatermark = 0
    else:
        text = HIGHWATERMARK_FILE.read_text().strip()
        try:
            highwatermark = int(text) if text else 0
        except ValueError:
            highwatermark = 0
    # 2. 遍历所有 task_*.json，找最大 task id（即 task_数字），复用 parse_task_num
    max_id = 0
    for path in TASKS_DIR.glob("task_*.json"):
        num = parse_task_num(path.stem)
        if num is not None:
            max_id = max(max_id, num)
    # 3. 新 id = max(highwatermark, max_id) + 1
    current = max(highwatermark, max_id)
    next_id = current + 1
    # 4. 写入 highwatermark
    HIGHWATERMARK_FILE.write_text(f"{next_id}\n")
    # 5. 返回新 task id
    return f"task_{next_id}"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    task = Task(
        id=allocate_task_id(),
        subject=subject,
        description=description,
        status="pending",
        owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task


def save_task(task: Task):
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))


def load_task(task_id: str) -> Task:
    return Task(**json.loads(_task_path(task_id).read_text()))


def list_tasks() -> list[Task]:
    tasks = [Task(**json.loads(p.read_text())) for p in TASKS_DIR.glob("task_*.json")]
    tasks.sort(key=lambda t: parse_task_num(t.id) or 0)
    return tasks


def get_task(task_id: str) -> str:
    """Return full task details as JSON."""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


def can_start(task_id: str) -> bool:
    """Check if all blockedBy dependencies are completed.
    Missing dependencies are treated as blocked."""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"]
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ── Task tools（供 LLM 通过 tool 调用）──

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks(status_filter: str = "all") -> str:
    tasks = list_tasks()
    if status_filter != "all":
        tasks = [t for t in tasks if t.status == status_filter]
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "●",
                "completed": "✓"}.get(t.status, "?")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} "
                     f"[{t.status}]{owner}{deps}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    try:
        return claim_task(task_id, owner="agent")
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_complete_task(task_id: str) -> str:
    try:
        return complete_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"