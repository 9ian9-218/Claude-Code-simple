"""
worktree.py — Git worktree isolation per task (s18)

On task claim  → create git worktree at .claude/worktrees/<task_id>/ on branch claude/task-<task_id>.
On task complete → remove worktree and branch.
"""

import subprocess
from pathlib import Path

from config import PROJECT_ROOT, WORKTREES_DIR


def ensure_worktrees_dir() -> None:
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)


def _git(*args: str, check: bool = True) -> str:
    """Run a git command in PROJECT_ROOT. Raises RuntimeError on failure."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=PROJECT_ROOT,
        capture_output=True, text=True, timeout=30,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def is_git_available() -> bool:
    """Check whether git is available and we are in a git repo."""
    try:
        _git("rev-parse", "--git-dir")
        return True
    except (RuntimeError, FileNotFoundError):
        return False


def is_git_clean() -> bool:
    """Check if the working tree has uncommitted changes."""
    status = _git("status", "--porcelain")
    return len(status) == 0


def task_branch_name(task_id: str) -> str:
    return f"claude/task-{task_id}"


def task_worktree_path(task_id: str) -> Path:
    return WORKTREES_DIR / task_id


def create_task_worktree(task_id: str) -> Path | None:
    """
    Create a git worktree for the given task.

    Returns the worktree Path, or None if creation fails (non-fatal — the
    task can still be worked on without isolation).
    """
    if not is_git_available():
        return None

    ensure_worktrees_dir()
    wt_path = task_worktree_path(task_id)
    if wt_path.exists():
        return wt_path

    branch = task_branch_name(task_id)

    # 1. Create tracking branch from HEAD (best-effort)
    try:
        _git("branch", "--track", branch, "HEAD")
    except RuntimeError:
        pass  # branch may already exist

    # 2. Create worktree
    try:
        _git("worktree", "add", str(wt_path), branch)
    except RuntimeError as e:
        # Clean up branch so we don't leave a dangling ref
        try:
            _git("branch", "-D", branch)
        except RuntimeError:
            pass
        print(f"  \033[33m[worktree] warning: could not create worktree for {task_id}: {e}\033[0m")
        return None

    print(f"  \033[36m[worktree] created at {wt_path} (branch: {branch})\033[0m")
    return wt_path


def remove_task_worktree(task_id: str) -> None:
    """
    Remove the git worktree and branch for a completed/failed task.
    Best-effort; never raises.
    """
    if not is_git_available():
        return

    wt_path = task_worktree_path(task_id)
    branch = task_branch_name(task_id)

    if wt_path.exists():
        try:
            _git("worktree", "remove", str(wt_path))
        except RuntimeError:
            try:
                _git("worktree", "remove", "--force", str(wt_path))
            except RuntimeError:
                pass

    # Prune stale worktree references
    try:
        _git("worktree", "prune")
    except RuntimeError:
        pass

    # Remove the branch
    try:
        _git("branch", "-D", branch)
    except RuntimeError:
        pass


def list_task_worktrees() -> list[str]:
    """Return sorted task_ids that have worktrees on disk."""
    ensure_worktrees_dir()
    return sorted(
        d.name for d in WORKTREES_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


def get_current_worktree_task_id() -> str | None:
    """Return the task_id if cwd is inside a task worktree."""
    try:
        toplevel = Path(_git("rev-parse", "--show-toplevel")).resolve()
        wt_resolved = WORKTREES_DIR.resolve()
        if wt_resolved in toplevel.parents:
            return toplevel.name
    except (RuntimeError, FileNotFoundError):
        pass
    return None
