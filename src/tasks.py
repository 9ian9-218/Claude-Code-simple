from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import TASKS_DIR, set_worktree_override
from console_lock import locked_print
from file_lock import file_lock
from worktree import create_task_worktree, remove_task_worktree
"""
CC 任务系统字段：(本项目包括了其中 7 个字段)
字段	类型	用途
id	string	递增整数 ID
subject	string	简短标题
description	string	自由格式描述
activeForm	string?	进行时态，in_progress 时在 spinner 显示，不包括
owner	string?	分配的 agent ID
status	pending/in_progress/completed	生命周期
blocks	string[]	此任务阻塞的任务 ID（下游）
blockedBy	string[]	阻塞此任务的任务 ID（上游）
metadata	Record?	任意扩展键值对，不包括
"""
# ── Task System ──

HIGHWATERMARK_FILE = TASKS_DIR / ".highwatermark"


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None    # Agent name (multi-agent scenarios)
    blockedBy: list[str] = field(default_factory=list)  # 上游：须等这些 task 完成
    blocks: list[str] = field(default_factory=list)     # 下游：被本 task 阻塞的 task id


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def _task_lock_path(task_id: str) -> Path:
    path = _task_path(task_id)
    return path.with_suffix(path.suffix + ".lock")


def _read_task_file(path: Path) -> Task:
    return _task_from_dict(json.loads(path.read_text(encoding="utf-8")))


def _write_task_file(path: Path, task: Task) -> None:
    path.write_text(json.dumps(asdict(task), indent=2), encoding="utf-8")


def parse_task_num(task_id: str) -> int | None:
    """从 task_42 解析数字序号；无法解析时返回 None。"""
    if not task_id.startswith("task_"):
        return None
    try:
        return int(task_id.split("_", 1)[1])
    except ValueError:
        return None


def _read_highwatermark() -> int:
    """读取曾分配过的最高任务序号（文件不存在则为 0）。"""
    if not HIGHWATERMARK_FILE.exists():
        return 0
    text = HIGHWATERMARK_FILE.read_text().strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0


def _write_highwatermark(value: int) -> None:
    HIGHWATERMARK_FILE.write_text(f"{value}\n")


def _max_id_from_task_files() -> int:
    """扫描 task_*.json 的最大序号（highwatermark 丢失时用于恢复）。"""
    max_id = 0
    for path in TASKS_DIR.glob("task_*.json"):
        num = parse_task_num(path.stem)
        if num is not None:
            max_id = max(max_id, num)
    return max_id


def _next_task_num() -> int:
    """下一个 task 序号（不写盘）。"""
    return max(_read_highwatermark(), _max_id_from_task_files()) + 1


def allocate_task_id() -> str:
    """
    顺序 ID + highwatermark（对齐 CC tasks.ts）。
    新 ID = max(highwatermark, 现存文件最大序号) + 1；写入后即使删除 JSON 也不重用。
    """
    next_id = _next_task_num()
    _write_highwatermark(next_id)
    return f"task_{next_id}"


# ── 依赖图（blockedBy：task → 依赖，边 u→v 表示 u 须等 v 完成）──

def build_dependency_graph(
    extra_nodes: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """
    构建 blockedBy 有向图 adj[u] = u 依赖的 task id 列表。
    extra_nodes: 尚未落盘的节点，例如 {"task_5": ["task_1", "task_2"]}。
    """
    graph: dict[str, list[str]] = {t.id: list(t.blockedBy) for t in list_tasks()}
    if extra_nodes:
        graph.update({k: list(v) for k, v in extra_nodes.items()})
    return graph


def task_graph_has_cycle(graph: dict[str, list[str]] | None = None) -> bool:
    """检测依赖图是否存在环（有环则可能出现永远无法 claim 的死锁）。"""
    graph = graph if graph is not None else build_dependency_graph()

    visited: set[str] = set()
    stack: set[str] = set()

    def dfs(node: str) -> bool:
        if node in stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        stack.add(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            if dfs(dep):
                return True
        stack.remove(node)
        return False

    for node in graph:
        if node not in visited and dfs(node):
            return True
    return False


def _task_from_dict(data: dict) -> Task:
    """从 JSON 反序列化 。"""
    return Task(
        id=data["id"],
        subject=data["subject"],
        description=data.get("description", ""),
        status=data["status"],
        owner=data.get("owner"),
        blockedBy=list(data.get("blockedBy", [])),
        blocks=list(data.get("blocks", [])),
    )


_blocks_index_synced = False


def _ensure_blocks_index() -> None:
    """
    根据全图 blockedBy 重建 blocks 。
    每个进程首次访问任务列表时执行一次。
    """
    global _blocks_index_synced
    if _blocks_index_synced:
        return

    paths = list(TASKS_DIR.glob("task_*.json"))
    if not paths:
        _blocks_index_synced = True
        return

    tasks = [_task_from_dict(json.loads(p.read_text())) for p in paths]
    blocks_map: dict[str, list[str]] = {t.id: [] for t in tasks}

    for t in tasks:
        for dep_id in t.blockedBy:
            if dep_id in blocks_map and t.id not in blocks_map[dep_id]:
                blocks_map[dep_id].append(t.id)

    for t in tasks:
        new_blocks = sorted(blocks_map[t.id])
        if t.blocks != new_blocks:
            t.blocks = new_blocks
            save_task(t)

    _blocks_index_synced = True


def add_block(downstream_id: str, blocked_by: list[str]) -> None:
    """下游 blockedBy 指向的上游，在上游 blocks 中登记 downstream_id（O(1) 查表）。"""
    for upstream_id in blocked_by:
        upstream = load_task(upstream_id)
        if downstream_id not in upstream.blocks:
            upstream.blocks.append(downstream_id)
            save_task(upstream)


def validate_create_task_dependencies(task_id: str, blocked_by: list[str]) -> str | None:
    """
    校验新建 task 的 blockedBy 是否合法且无环。
    返回 None 表示通过；否则返回可读错误信息。
    """
    if task_id in blocked_by:
        return f"Task {task_id} cannot depend on itself"

    for dep in blocked_by:
        if not _task_path(dep).exists():
            return f"Unknown dependency: {dep}"

    graph = build_dependency_graph({task_id: blocked_by})
    if task_graph_has_cycle(graph):
        return (
            "blockedBy would create a cyclic dependency; "
            "fix the dependency chain so tasks can complete in order"
        )
    return None


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    blocked_by = blockedBy or []
    next_num = _next_task_num()
    task_id = f"task_{next_num}"
    err = validate_create_task_dependencies(task_id, blocked_by)
    if err:
        raise ValueError(err)

    _write_highwatermark(next_num)
    task = Task(
        id=task_id,
        subject=subject,
        description=description,
        status="pending",
        owner=None,
        blockedBy=blocked_by,
        blocks=[],
    )
    save_task(task)
    add_block(task_id, blocked_by)
    return task


def save_task(task: Task):
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))


def load_task(task_id: str) -> Task:
    """加载单条任务；首次加载前会按需从 blockedBy 同步 blocks 索引。"""
    _ensure_blocks_index()
    return _task_from_dict(json.loads(_task_path(task_id).read_text()))


def list_tasks() -> list[Task]:
    _ensure_blocks_index()
    tasks = [_task_from_dict(json.loads(p.read_text())) for p in TASKS_DIR.glob("task_*.json")]
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
    tasks = _load_all_tasks_raw()
    return _deps_satisfied(task, _unresolved_task_ids(tasks))


def _task_list_lock_path() -> Path:
    return TASKS_DIR / ".lock"


def _load_all_tasks_raw() -> list[Task]:
    """Load all tasks from disk (caller holds task-list lock for consistent snapshots)."""
    tasks = [
        _task_from_dict(json.loads(p.read_text(encoding="utf-8")))
        for p in TASKS_DIR.glob("task_*.json")
    ]
    tasks.sort(key=lambda t: parse_task_num(t.id) or 0)
    return tasks


def _unresolved_task_ids(tasks: list[Task]) -> set[str]:
    """Tasks that are not yet completed (aligned with CC unresolvedTaskIds)."""
    return {t.id for t in tasks if t.status != "completed"}


def _deps_satisfied(task: Task, unresolved: set[str]) -> bool:
    """All blockedBy deps exist and are completed (not in unresolved set)."""
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if dep_id in unresolved:
            return False
    return True


def _find_available_task(tasks: list[Task]) -> Task | None:
    """First pending, unowned task whose blockedBy deps are all completed (CC findAvailableTask)."""
    unresolved = _unresolved_task_ids(tasks)
    for task in tasks:
        if task.status != "pending" or task.owner:
            continue
        if _deps_satisfied(task, unresolved):
            return task
    return None


def _agent_busy_task(tasks: list[Task], owner: str) -> Task | None:
    """Return in_progress task already owned by this agent (CC busy check)."""
    for task in tasks:
        if task.owner == owner and task.status == "in_progress":
            return task
    return None


def _execute_task_claim(task_id: str, owner: str) -> str:
    """Per-task file lock: read-check-write → in_progress → create worktree."""
    path = _task_path(task_id)
    if not path.exists():
        raise FileNotFoundError(task_id)

    with file_lock(_task_lock_path(task_id)):
        task = _read_task_file(path)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        if task.owner:
            return f"Task {task_id} already owned by {task.owner}"
        tasks = _load_all_tasks_raw()
        if not _deps_satisfied(task, _unresolved_task_ids(tasks)):
            deps = [
                d for d in task.blockedBy
                if d in _unresolved_task_ids(tasks) or not _task_path(d).exists()
            ]
            return f"Blocked by: {deps}"
        task.owner = owner
        task.status = "in_progress"
        _write_task_file(path, task)
        subject = task.subject
        task_ref = task.id

    # Create git worktree for isolation (non-fatal on failure)
    wt = create_task_worktree(task_id)
    if wt is not None:
        set_worktree_override(wt)
        locked_print(f"  \033[36m[worktree] switched to {wt}\033[0m")

    locked_print(f"  \033[36m[claim] {subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task_ref} ({subject})"


def claim_task_with_busy_check(
    owner: str,
    task_id: str | None = None,
    *,
    enforce_busy: bool = True,
) -> str:
    """
    CC claimTaskWithBusyCheck: task-list lock → busy check → find/select → claim.
    Lock order: list lock, then per-task file lock (no deadlock with claim-only paths).
    """
    _ensure_blocks_index()
    with file_lock(_task_list_lock_path()):
        tasks = _load_all_tasks_raw()

        if enforce_busy:
            busy = _agent_busy_task(tasks, owner)
            if busy is not None:
                return (
                    f"Agent '{owner}' is busy with {busy.id} ({busy.subject}); "
                    f"complete it before claiming another task"
                )

        if task_id is not None:
            target = next((t for t in tasks if t.id == task_id), None)
            if target is None:
                return f"Error: Task {task_id} not found"
            unresolved = _unresolved_task_ids(tasks)
            if target.status != "pending":
                return f"Task {task_id} is {target.status}, cannot claim"
            if target.owner:
                return f"Task {task_id} already owned by {target.owner}"
            if not _deps_satisfied(target, unresolved):
                deps = [
                    d for d in target.blockedBy
                    if d in unresolved or not _task_path(d).exists()
                ]
                return f"Blocked by: {deps}"
            chosen_id = task_id
        else:
            available = _find_available_task(tasks)
            if available is None:
                return "No unclaimed tasks available"
            chosen_id = available.id

        return _execute_task_claim(chosen_id, owner)


def try_claim_next_task(owner: str) -> str:
    """Autonomous claim: busy check + findAvailable + claim atomically (CC tryClaimNextTask)."""
    return claim_task_with_busy_check(owner, task_id=None, enforce_busy=True)


def scan_unclaimed_tasks() -> list[Task]:
    """Pending, unowned tasks with all blockedBy dependencies completed (s17)."""
    _ensure_blocks_index()
    with file_lock(_task_list_lock_path()):
        tasks = _load_all_tasks_raw()
        unresolved = _unresolved_task_ids(tasks)
        return [
            t for t in tasks
            if t.status == "pending" and not t.owner and _deps_satisfied(t, unresolved)
        ]


def claim_task(
    task_id: str,
    owner: str = "agent",
    *,
    enforce_busy: bool = False,
) -> str:
    """Claim a specific task. Workers should pass enforce_busy=True (via run_claim_task)."""
    return claim_task_with_busy_check(
        owner, task_id=task_id, enforce_busy=enforce_busy
    )


def complete_task(task_id: str, *, owner: str | None = None) -> str:
    path = _task_path(task_id)
    if not path.exists():
        raise FileNotFoundError(task_id)

    with file_lock(_task_lock_path(task_id)):
        task = _read_task_file(path)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"
        if owner is not None and task.owner is not None and task.owner != owner:
            return (
                f"Task {task_id} is owned by {task.owner}; "
                f"only the owner can complete it"
            )
        task.status = "completed"
        _write_task_file(path, task)
        subject = task.subject
        task_ref = task.id
        block_ids = list(task.blocks)

    # Remove worktree (best-effort) and restore working directory
    remove_task_worktree(task_id)
    set_worktree_override(None)

    unblocked: list[str] = []
    for down_id in block_ids:
        if not _task_path(down_id).exists():
            continue
        downstream = load_task(down_id)
        if downstream.status == "pending" and can_start(down_id):
            unblocked.append(downstream.subject)
    locked_print(f"  \033[32m[complete] {subject} ✓\033[0m")
    msg = f"Completed {task_ref} ({subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        locked_print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ── Task tools（供 LLM 通过 tool 调用）──

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    try:
        task = create_task(subject, description, blockedBy)
    except ValueError as e:
        return f"Error: {e}"
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    locked_print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
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
        blocks = f" (blocks: {', '.join(t.blocks)})" if t.blocks else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} "
                     f"[{t.status}]{owner}{deps}{blocks}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str, owner: str | None = None) -> str:
    enforce_busy = False
    if owner is None:
        try:
            from teammates.context import get_agent_context
            ctx = get_agent_context()
            owner = ctx.agent_name if ctx.is_worker else "agent"
            enforce_busy = ctx.is_worker
        except ImportError:
            owner = "agent"
    return claim_task(task_id, owner=owner, enforce_busy=enforce_busy)


def run_complete_task(task_id: str) -> str:
    owner: str | None = None
    try:
        from teammates.context import get_agent_context
        ctx = get_agent_context()
        if ctx.is_worker:
            owner = ctx.agent_name
    except ImportError:
        pass
    try:
        return complete_task(task_id, owner=owner)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"