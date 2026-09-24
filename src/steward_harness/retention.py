"""Reclaim materialized checkouts only after their owners have accepted the work."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from steward_harness.git import IN_PROGRESS_GIT_MARKERS, agent_git
from steward_harness.kernel import repository_lease
from steward_harness.state import ConversationId, TaskId
from steward_harness.task_lock import task_lock
from steward_harness.lease import Busy

log = logging.getLogger(__name__)


def _head_if_clean(broker, repository: Path, path: Path) -> str:
    """Validate custody, including ignored files and interrupted Git operations."""
    def git(*args, cwd=path):
        return agent_git(broker, *args, cwd=cwd, timeout=60)

    if broker.is_symlink(path):
        raise ValueError("symlink checkout")
    if Path(git("rev-parse", "--show-toplevel")) != path:
        raise ValueError("unexpected checkout root")
    common_args = ("rev-parse", "--path-format=absolute", "--git-common-dir")
    if git(*common_args) != git(*common_args, cwd=repository):
        raise ValueError("checkout belongs to another repository")
    if git("status", "--porcelain", "--untracked-files=all", "--ignored"):
        raise ValueError("dirty, untracked or ignored files")
    for marker in IN_PROGRESS_GIT_MARKERS:
        if broker.path_exists(Path(git("rev-parse", "--path-format=absolute", "--git-path", marker))):
            raise ValueError(f"unfinished Git operation: {marker}")
    return git("rev-parse", "HEAD")


def _remove(broker, repository: Path, path: Path) -> None:
    # No force and no filesystem fallback: Git gets the final refusal.
    agent_git(broker, "worktree", "remove", str(path), cwd=repository, timeout=60)
    agent_git(broker, "worktree", "prune", cwd=repository, timeout=60)
    log.info("pruned accepted workspace %s", path)


def prune_tasks(runner) -> None:
    """Use accepted task status, then recheck under repository and task locks."""
    for task in runner.state.tasks.all():
        if task.status.value not in {"done", "cancelled"}:
            continue
        task_id = task.task_id
        repository = runner.repositories.get(task.repository)
        path = runner.worktrees_root / str(task_id)
        if repository is None or not runner.broker.path_exists(path):
            continue
        lock = task_lock(runner.state.tasks.locks_root, task_id)
        try:
            lock.acquire()
        except Busy:
            continue
        try:
            with repository_lease(runner.state, task.repository), runner.state.tasks.lease:
                current = runner.state.tasks.get(task_id)
                # This retention operation owns the lock; it is not cognition.
                if replace(current, running=False).status.value not in {"done", "cancelled"}:
                    continue
                head = _head_if_clean(runner.broker, Path(repository.path), path)
                if not runner.state.tasks.contains(head, current.revision):
                    raise ValueError("HEAD is not retained in accepted task Git")
                _remove(runner.broker, Path(repository.path), path)
        except Busy:
            continue
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
            log.warning("retained task workspace %s: %s", path, error)
        finally:
            lock.release()


def prune_world_sessions(checkpoint, idle_seconds: int) -> None:
    """Serialize eligibility with turn admission and world acceptance."""
    state = checkpoint.state
    cutoff = datetime.now(UTC) - timedelta(seconds=idle_seconds)
    with state.connect() as connection:
        owners = [row[0] for row in connection.execute("SELECT DISTINCT conversation_id FROM turns")]
    for owner in owners:
        name = hashlib.sha256(ConversationId(owner).workspace.encode()).hexdigest()
        path = checkpoint.worktrees_root / f"session-{name}"
        if not checkpoint.broker.path_exists(path):
            continue
        try:
            # World writers take this lease before writing SQL. BEGIN IMMEDIATE
            # also prevents a new turn being admitted between the check and removal.
            with checkpoint.lease, state.connect(write=True) as connection:
                latest = connection.execute(
                    "SELECT * FROM turns WHERE conversation_id=? AND execution_turn_id IS NULL "
                    "ORDER BY started_at DESC, turn_id DESC LIMIT 1", (owner,),
                ).fetchone()
                if latest is None or latest["state"] != "completed":
                    continue
                if datetime.fromisoformat(latest["completed_at"]) >= cutoff:
                    continue
                pending = connection.execute(
                    "SELECT 1 FROM turns WHERE conversation_id=? AND state != 'completed' "
                    "AND episode_input IS NOT NULL LIMIT 1", (owner,),
                ).fetchone()
                receipt = state.path.with_suffix(".world-completions") / (
                    hashlib.sha256(owner.encode()).hexdigest() + ".json"
                )
                if pending or receipt.exists():
                    log.warning("retained world workspace %s: pending world turn or completion", path)
                    continue
                head = _head_if_clean(checkpoint.broker, checkpoint.world.root, path)
                if checkpoint._git(checkpoint.world.root, "merge-base", "--is-ancestor", head,
                                   checkpoint.world.input_cursor(), check=False).returncode:
                    raise ValueError("HEAD is not in the accepted world")
                _remove(checkpoint.broker, checkpoint.world.root, path)
        except Busy:
            continue
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
            log.warning("retained world workspace %s: %s", path, error)
