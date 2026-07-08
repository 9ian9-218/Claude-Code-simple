"""
Comprehensive module tests for Claude-Code-simple.

Tests each module independently and the full integration flow.
Reports any issues found.
"""
import sys
import os
import json
import tempfile
import time
import threading
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

issues = []
passed = 0
failed = 0


def test(name):
    _tests.append((name, None))  # placeholder

    def decorator(fn):
        for i, (n, _) in enumerate(_tests):
            if n == name:
                _tests[i] = (name, fn)
                break
        return fn
    return decorator

_tests = []


def run_all():
    global passed, failed
    for name, fn in _tests:
        if fn is None:
            continue
        try:
            fn()
            print(f"  \u2713 {name}")
            passed += 1
        except Exception as e:
            print(f"  \u2717 {name}: {e}")
            issues.append((name, str(e)))
            failed += 1
    print(f"\n{'='*50}")
    print(f"Total: {passed + failed}, Passed: {passed}, Failed: {failed}")
    if issues:
        print(f"\nIssues found:")
        for name, msg in issues:
            print(f"  [{name}] {msg}")


# ── Module 1: config ─────────────────────────────────────────────

@test("config: imports cleanly")
def test_config_import():
    import config as c
    assert hasattr(c, 'PROJECT_ROOT')
    assert hasattr(c, 'WORKDIR')
    assert hasattr(c, 'WORKTREES_DIR')
    assert hasattr(c, 'get_workdir')
    assert hasattr(c, 'set_worktree_override')

@test("config: get_workdir returns WORKDIR by default")
def test_config_get_workdir_default():
    import config as c
    assert c.get_workdir() == c.WORKDIR

@test("config: set_worktree_override changes get_workdir")
def test_config_worktree_override():
    import config as c
    tmp = Path(tempfile.mkdtemp())
    c.set_worktree_override(tmp)
    assert c.get_workdir() == tmp
    c.set_worktree_override(None)
    assert c.get_workdir() == c.WORKDIR

@test("config: worktree override is thread-local")
def test_config_thread_local():
    import config as c
    original = c.get_workdir()
    results = {}
    tmp = Path(tempfile.mkdtemp())

    def worker():
        c.set_worktree_override(tmp)
        results['worker'] = c.get_workdir()

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert results['worker'] == tmp, "Worker should see its override"
    assert c.get_workdir() == original, "Main thread should be unaffected"

@test("config: ensure_claude_dirs creates all dirs")
def test_config_dirs():
    import config as c
    for d in [c.CLAUDE_DIR, c.TEAMS_DIR, c.MEMORY_DIR, c.TASKS_DIR,
              c.SKILLS_DIR, c.WORKTREES_DIR]:
        assert d.exists(), f"Directory does not exist: {d}"


# ── Module 2: worktree ───────────────────────────────────────────

@test("worktree: imports cleanly")
def test_worktree_import():
    import worktree as wt
    assert hasattr(wt, 'create_task_worktree')
    assert hasattr(wt, 'remove_task_worktree')
    assert hasattr(wt, 'is_git_available')
    assert hasattr(wt, 'task_branch_name')
    assert hasattr(wt, 'task_worktree_path')

@test("worktree: naming conventions")
def test_worktree_naming():
    import worktree as wt
    assert wt.task_branch_name("task_5") == "claude/task-task_5"
    p = wt.task_worktree_path("task_5")
    assert "worktrees" in str(p)
    assert p.name == "task_5"

@test("worktree: is_git_available on this repo")
def test_worktree_git_avail():
    import worktree as wt
    assert wt.is_git_available(), "Should be in a git repo"

@test("worktree: create and remove worktree")
def test_worktree_create_remove():
    import worktree as wt
    tid = "test_mod_verify"
    wt_path = wt.task_worktree_path(tid)
    if wt_path.exists():
        wt.remove_task_worktree(tid)

    p = wt.create_task_worktree(tid)
    assert p is not None, "Worktree creation returned None"
    assert p.exists(), f"Worktree path does not exist: {p}"
    assert (p / "src").exists(), "Worktree should contain src/"

    wt.remove_task_worktree(tid)
    assert not p.exists(), "Worktree should be removed"

    import subprocess
    subprocess.run(["git", "branch", "-D", f"claude/task-{tid}"],
                   cwd=Path(__file__).resolve().parent.parent, capture_output=True)

@test("worktree: idempotent removal")
def test_worktree_idempotent_remove():
    import worktree as wt
    wt.remove_task_worktree("nonexistent_task")

@test("worktree: list and current")
def test_worktree_list():
    import worktree as wt
    result = wt.list_task_worktrees()
    assert isinstance(result, list)
    current = wt.get_current_worktree_task_id()
    assert current is None or isinstance(current, str)


# ── Module 3: tasks ──────────────────────────────────────────────

@test("tasks: imports cleanly")
def test_tasks_import():
    import tasks as t
    assert hasattr(t, 'Task')
    assert hasattr(t, 'create_task')
    assert hasattr(t, 'claim_task')
    assert hasattr(t, 'complete_task')

@test("tasks: create/list/claim/complete lifecycle")
def test_task_lifecycle():
    import tasks as t
    from config import TASKS_DIR, set_worktree_override
    set_worktree_override(None)
    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()

    task = t.create_task("test subject", "test desc", blockedBy=[])
    tid = task.id
    assert task.status == "pending"

    all_tasks = t.list_tasks()
    assert any(x.id == tid for x in all_tasks), "Task not found in list"

    result = t.claim_task(tid, owner="test_agent", enforce_busy=False)
    assert "Claimed" in result, f"Claim failed: {result}"

    claimed = t.load_task(tid)
    assert claimed.status == "in_progress"
    assert claimed.owner == "test_agent"

    result = t.complete_task(tid, owner="test_agent")
    assert "Completed" in result, f"Complete failed: {result}"

    done = t.load_task(tid)
    assert done.status == "completed"

    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()

@test("tasks: dependency graph")
def test_task_deps():
    import tasks as t
    from config import TASKS_DIR, set_worktree_override
    set_worktree_override(None)
    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()

    t1 = t.create_task("dep 1", "", blockedBy=[])
    t2 = t.create_task("dep 2", "", blockedBy=[t1.id])
    t3 = t.create_task("dep 3", "", blockedBy=[t2.id])

    claim_t3 = t.claim_task(t3.id, owner="agent", enforce_busy=False)
    assert "Blocked" in claim_t3, f"t3 should be blocked: {claim_t3}"

    t.claim_task(t1.id, owner="agent", enforce_busy=False)
    t.complete_task(t1.id, owner="agent")
    t.claim_task(t2.id, owner="agent", enforce_busy=False)
    t.complete_task(t2.id, owner="agent")

    result = t.claim_task(t3.id, owner="agent", enforce_busy=False)
    assert "Claimed" in result, f"t3 should now be claimable: {result}"
    t.complete_task(t3.id, owner="agent")

    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()

@test("tasks: cannot complete someone else's task")
def test_task_owner_enforce():
    import tasks as t
    from config import TASKS_DIR, set_worktree_override
    set_worktree_override(None)
    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()

    task = t.create_task("owned test", "", blockedBy=[])
    t.claim_task(task.id, owner="alice", enforce_busy=False)

    result = t.complete_task(task.id, owner="bob")
    assert "only the owner" in result, f"Should reject non-owner: {result}"

    result = t.complete_task(task.id, owner="alice")
    assert "Completed" in result

    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()

@test("tasks: worktree integration on claim/complete")
def test_task_worktree_integration():
    import tasks as t
    from config import TASKS_DIR, get_workdir, set_worktree_override
    from worktree import task_worktree_path
    set_worktree_override(None)

    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()

    original_workdir = get_workdir()
    task = t.create_task("wt test", "test worktree integration", blockedBy=[])
    tid = task.id

    result = t.claim_task(tid, owner="agent", enforce_busy=False)
    assert "Claimed" in result, f"Claim failed: {result}"

    wt_path = task_worktree_path(tid)
    assert wt_path.exists(), f"Worktree should exist: {wt_path}"
    assert get_workdir() == wt_path, "Workdir should be switched"

    result = t.complete_task(tid, owner="agent")
    assert "Completed" in result, f"Complete failed: {result}"
    assert not wt_path.exists(), "Worktree should be removed after complete"
    assert get_workdir() == original_workdir, "Workdir should be restored"

    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()

@test("tasks: run_list_tasks formatting")
def test_task_list_format():
    import tasks as t
    from config import TASKS_DIR, set_worktree_override
    set_worktree_override(None)
    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()

    t.create_task("list test", "", blockedBy=[])
    output = t.run_list_tasks("all")
    assert "list test" in output
    assert "pending" in output

    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()


# ── Module 4: messageQueue ───────────────────────────────────────

@test("messageQueue: enqueue and consume")
def test_mq_basic():
    from messageQueueManager import (
        enqueue_pending_notification, consume_pending_notifications
    )
    consume_pending_notifications()
    enqueue_pending_notification("test message")
    results = consume_pending_notifications()
    assert len(results) == 1
    assert "test message" in results[0]

@test("messageQueue: priority ordering")
def test_mq_priority():
    from messageQueueManager import (
        enqueue_pending_notification, consume_pending_notifications, Priority
    )
    consume_pending_notifications()
    enqueue_pending_notification("later msg", priority="later")
    enqueue_pending_notification("next msg", priority="next")

    results = consume_pending_notifications()
    assert len(results) == 2
    assert "next" in results[0], f"NEXT priority should come first: {results}"
    assert "later" in results[1]

@test("messageQueue: recipient filtering")
def test_mq_recipient():
    from messageQueueManager import (
        enqueue_pending_notification, consume_pending_notifications
    )
    consume_pending_notifications()

    # recipient=None = global (lead) notification
    enqueue_pending_notification("global msg")

    # recipient=bob = bob's notification
    enqueue_pending_notification("bob msg", recipient="bob")

    # Lead drains global notifications
    lead_msgs = consume_pending_notifications()
    assert len(lead_msgs) == 1
    assert "global" in lead_msgs[0]

    # Bob drains his own
    bob_msgs = consume_pending_notifications(recipient="bob")
    assert len(bob_msgs) == 1
    assert "bob" in bob_msgs[0]

    # No leftovers
    assert len(consume_pending_notifications()) == 0
    assert len(consume_pending_notifications(recipient="bob")) == 0

@test("messageQueue: task notification XML format")
def test_mq_task_notification():
    from messageQueueManager import enqueue_task_notification, consume_pending_notifications
    consume_pending_notifications()

    enqueue_task_notification("completed", "echo done")
    results = consume_pending_notifications()
    assert len(results) == 1
    assert "<task_notification>" in results[0]
    assert "completed" in results[0]
    assert "echo done" in results[0]


# ── Module 5: teammate context ───────────────────────────────────

@test("context: thread-local isolation")
def test_context_thread_local():
    from teammates.context import get_agent_context, AgentContext, agent_context

    ctx = get_agent_context()
    assert ctx.agent_name == "team-lead"
    assert ctx.role == "lead"

    new_ctx = AgentContext(agent_name="test-agent", role="teammate")
    with agent_context(new_ctx):
        assert get_agent_context().agent_name == "test-agent"
        assert get_agent_context().role == "teammate"

    assert get_agent_context().agent_name == "team-lead"

@test("context: thread-safe isolation")
def test_context_thread_safety():
    from teammates.context import get_agent_context, AgentContext, agent_context
    import threading

    results = {}
    def worker(name):
        with agent_context(AgentContext(agent_name=name, role="teammate")):
            results[name] = get_agent_context().agent_name

    threads = [threading.Thread(target=worker, args=(f"agent-{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(5):
        assert results[f"agent-{i}"] == f"agent-{i}"
    assert get_agent_context().agent_name == "team-lead", "Main thread unaffected"


# ── Module 6: hook system ────────────────────────────────────────

@test("hook: registration and triggering")
def test_hook_system():
    from hook import register_hook, trigger_hooks

    results = []
    def my_hook(arg):
        results.append(arg)
        return None

    register_hook("TestEvent", my_hook)
    trigger_hooks("TestEvent", "hello")
    assert "hello" in results

@test("hook: short-circuit on non-None")
def test_hook_short_circuit():
    from hook import register_hook, trigger_hooks

    results = []
    def blocker(arg):
        return "blocked"

    register_hook("BlockEvent", blocker)
    register_hook("BlockEvent", lambda x: results.append(x))

    result = trigger_hooks("BlockEvent", "test")
    assert result == "blocked"
    assert len(results) == 0, "Second callback should NOT be called"

@test("hook: context_inject_hook uses dynamic workdir")
def test_hook_dynamic_workdir():
    from hook import context_inject_hook
    import io
    from contextlib import redirect_stdout

    f = io.StringIO()
    with redirect_stdout(f):
        context_inject_hook("test query")
    output = f.getvalue()
    assert "working in" in output


# ── Module 7: permission check ───────────────────────────────────

@test("permissions: deny list")
def test_perm_deny():
    from check_permissions import check_deny_list
    assert check_deny_list("rm -rf /") is not None
    assert check_deny_list("ls -la") is None

@test("permissions: path check rules")
def test_perm_rules():
    from check_permissions import check_rules
    result = check_rules("write_file", {"path": "/etc/passwd"})
    assert result is not None, "Should block write outside workspace"

    result = check_rules("write_file", {"path": "src/tool.py"})
    assert result is None, "Should allow write inside workspace"


# ── Module 8: tool execution ─────────────────────────────────────

@test("tool: execute_tool_call handles unknown tool")
def test_tool_unknown():
    import tool
    from types import SimpleNamespace
    tc = SimpleNamespace(
        function=SimpleNamespace(name="nonexistent_tool", arguments="{}")
    )
    result = tool.execute_tool_call(tc)
    assert "Unknown" in result or "error" in result

@test("tool: execute_tool_call handles bad JSON")
def test_tool_bad_json():
    import tool
    from types import SimpleNamespace
    tc = SimpleNamespace(
        function=SimpleNamespace(name="glob", arguments="{bad json")
    )
    result = tool.execute_tool_call(tc)
    assert result is not None

@test("tool: safe_path respects worktree override")
def test_tool_safe_path_worktree():
    from tool import safe_path
    from config import set_worktree_override

    tmpdir = Path(tempfile.mkdtemp())
    (tmpdir / "wt-file.txt").write_text("in worktree")

    set_worktree_override(tmpdir)
    p = safe_path("wt-file.txt")
    assert p.exists(), f"File should exist at {p}"
    assert str(p).startswith(str(tmpdir)), f"Path should be in worktree: {p}"
    set_worktree_override(None)

@test("tool: safe_path escapes outside worktree")
def test_tool_safe_path_escape():
    from tool import _check_path
    err = _check_path("/etc/passwd")
    assert err is not None, "Should reject absolute path outside workspace"
    assert "escapes" in err

@test("tool: run_bash uses worktree cwd")
def test_tool_bash_worktree():
    from config import set_worktree_override
    import tempfile
    tmpdir = Path(tempfile.mkdtemp())
    (tmpdir / "marker.txt").write_text("marker")
    set_worktree_override(tmpdir)

    from tool import _exec_run_bash
    result = _exec_run_bash({"command": "ls marker.txt", "run_in_background": False})
    assert "marker.txt" in result, f"Bash should see worktree files: {result}"

    set_worktree_override(None)


# ── Module 9: console_lock ───────────────────────────────────────

@test("console_lock: locked_print works")
def test_console_lock():
    from console_lock import locked_print
    locked_print("test output")


# ── Module 10: file_lock ─────────────────────────────────────────

@test("file_lock: basic locking")
def test_file_lock():
    from file_lock import file_lock
    tmp = Path(tempfile.mkdtemp()) / "test.lock"
    with file_lock(tmp):
        pass


# ── Module 11: prompt ────────────────────────────────────────────

@test("prompt: assemble_system_prompt uses dynamic workspace")
def test_prompt_workspace():
    from prompt import assemble_system_prompt
    result = assemble_system_prompt({"workspace": "/custom/ws"})
    assert "/custom/ws" in result

    result_sub = assemble_system_prompt({"workspace": "/sub/ws"}, isSubagent=True)
    assert "/sub/ws" in result_sub

@test("prompt: update_context returns workspace")
def test_prompt_update_context():
    from prompt import update_context
    ctx = update_context({}, [])
    assert "workspace" in ctx


# ── Integration: worktree + tasks + tools ───────────────────────

@test("integration: worktree file ops isolated")
def test_integration_worktree_isolation():
    from tasks import create_task, claim_task, complete_task
    from config import TASKS_DIR, get_workdir, set_worktree_override
    from worktree import task_worktree_path
    from tool import _exec_write_file, _exec_read_file, _exec_glob
    set_worktree_override(None)

    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()

    original_wd = get_workdir()

    task = create_task("isolation test", "verify worktree isolation", blockedBy=[])
    tid = task.id
    claim_task(tid, owner="agent", enforce_busy=False)

    _exec_write_file({"path": "wt-test.txt", "content": "worktree content"})

    wt_path = task_worktree_path(tid)
    assert (wt_path / "wt-test.txt").exists(), "File should be in worktree"
    assert not (original_wd / "wt-test.txt").exists(), "File should NOT be in original workdir"

    content = _exec_read_file({"path": "wt-test.txt"})
    assert "worktree content" in content

    glob_result = _exec_glob({"pattern": "wt-test.txt"})
    assert "wt-test.txt" in glob_result

    complete_task(tid, owner="agent")

    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()

@test("integration: multiple tasks create separate worktrees")
def test_integration_multi_worktree():
    from tasks import create_task, claim_task, complete_task
    from config import TASKS_DIR, get_workdir, set_worktree_override
    from worktree import task_worktree_path
    from tool import _exec_write_file
    set_worktree_override(None)

    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()

    t1 = create_task("task A", "", blockedBy=[])
    t2 = create_task("task B", "", blockedBy=[])

    claim_task(t1.id, owner="agent", enforce_busy=False)
    _exec_write_file({"path": "t1-file.txt", "content": "from task 1"})
    complete_task(t1.id, owner="agent")

    claim_task(t2.id, owner="agent", enforce_busy=False)
    wt2 = task_worktree_path(t2.id)

    assert not (wt2 / "t1-file.txt").exists(), "t2 worktree should be clean"
    complete_task(t2.id, owner="agent")

    for f in TASKS_DIR.glob("task_*.json"):
        f.unlink()

@test("integration: background task notification")
def test_integration_bg_notification():
    from messageQueueManager import enqueue_task_notification, consume_pending_notifications
    consume_pending_notifications()

    enqueue_task_notification("completed", "echo hello")
    results = consume_pending_notifications()
    assert len(results) >= 1
    assert "echo hello" in results[0]


if __name__ == "__main__":
    run_all()
