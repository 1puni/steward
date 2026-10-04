"""Reclaim materialized checkouts only after their owners have accepted the work."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from steward_harness.git import git_operation_paths, agent_git
from steward_harness.kernel import repository_lease
from steward_harness.state import ConversationId, TaskId
from steward_harness.task_lock import task_lock
from steward_harness.lease import Busy
from steward_harness.runtime.native_evidence import EVIDENCE_PENDING

log = logging.getLogger(__name__)


def _head_if_clean(broker, repository: Path, path: Path) -> str:
    """Validate custody: no local changes, untracked files or interrupted Git operations.

    Ignored files do not block. The repository declares them regenerable, and
    without this rule every venv or dependency cache retains its checkout forever.
    """
    def git(*args, cwd=path):
        return agent_git(broker, *args, cwd=cwd, timeout=60)

    if broker.path_exists(path / EVIDENCE_PENDING):
        raise ValueError("native evidence preservation is pending")
    if broker.is_symlink(path):
        raise ValueError("symlink checkout")
    if Path(git("rev-parse", "--show-toplevel")) != path:
        raise ValueError("unexpected checkout root")
    common_args = ("rev-parse", "--path-format=absolute", "--git-common-dir")
    if git(*common_args) != git(*common_args, cwd=repository):
        raise ValueError("checkout belongs to another repository")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("dirty or untracked files")
    for marker, marker_path in git_operation_paths(git).items():
        if broker.path_exists(marker_path):
            raise ValueError(f"unfinished Git operation: {marker}")
    return git("rev-parse", "HEAD")


def _remove(broker, repository: Path, path: Path) -> None:
    # No force and no filesystem fallback: Git gets the final refusal. It
    # deletes ignored files and refuses modified or untracked ones.
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
                _head_if_clean(checkpoint.broker, checkpoint.world.root, path)
                # An accepted turn rebased past a concurrent writer is not an
                # ancestor of the world; its completed receipt accepts it.
                if checkpoint.unaccepted(path, checkpoint.world.input_cursor(),
                                         connection=connection):
                    raise ValueError("HEAD is not in the accepted world")
                _remove(checkpoint.broker, checkpoint.world.root, path)
        except Busy:
            continue
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
            log.warning("retained world workspace %s: %s", path, error)


# Runs as the execution identity, which owns the homes and the repositories.
_VERIFY_HOME = r'''
import json, os, pathlib, subprocess, sys, time
home, repository, commit, mappings, quiet_seconds = json.load(sys.stdin)
home = pathlib.Path(home)
env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
def git(*args, text=None):
    return subprocess.run(['git', *args], cwd=repository, input=text, capture_output=True,
                          text=True, env=env, timeout=240, check=True).stdout
newest = 0
for directory, _, names in os.walk(home):
    for name in names:
        newest = max(newest, os.lstat(os.path.join(directory, name)).st_mtime)
if newest > time.time() - quiet_seconds:
    print(json.dumps({'retire': False, 'reason': 'changed within the quiet period'}))
    sys.exit()
records = []
for name, relative in mappings.items():
    root = home / name
    if root.is_symlink():
        # A home from before capture wrote into a checkout. A missing target
        # was a clean checkout removed after commit; an existing one is checked.
        root = pathlib.Path(os.readlink(root))
    if not root.is_dir():
        continue
    for directory, _, names in os.walk(root):
        for file in names:
            path = pathlib.Path(directory) / file
            if path.is_file() and not path.is_symlink():
                records.append((relative + '/' + path.relative_to(root).as_posix(), str(path)))
if records:
    blobs = git('hash-object', '--no-filters', '--stdin-paths',
                text=''.join(path + '\n' for _, path in records)).split()
    tree = {}
    for line in git('ls-tree', '-r', '-z', commit, '--', *mappings.values()).split('\0'):
        if line:
            meta, path = line.split('\t', 1)
            tree[path] = meta.split()[2]
    missing = [path for (path, _), blob in zip(records, blobs) if tree.get(path) != blob]
    if missing:
        print(json.dumps({'retire': False, 'reason': f'{len(missing)} records not in {commit[:12]}, e.g. {missing[0]}'}))
        sys.exit()
print(json.dumps({'retire': True, 'records': len(records)}))
'''


def _home_key(provider: str, generation: int) -> str:
    return hashlib.sha256(json.dumps([provider, generation]).encode()).hexdigest()[:16]


def retire_native_homes(state, broker, native_homes, world_root, repositories,
                        *, quiet_seconds: int = 3600) -> None:
    """Remove provider homes whose records Git already holds and no run needs.

    A home is superseded when its owner's lineage moved to another generation
    or provider, or its task finished. Its records must match the accepted
    world head (or the task's tip) byte for byte, nothing in it may have
    changed for an hour, and its owner may have no running turn. Its private
    provider state (databases, caches) goes with it. Unknown owners stay.
    """
    homes = {family: Path(path) for family, path in native_homes.items()}
    with state.connect() as connection:
        lineages = {row[0]: (row[1], row[2]) for row in connection.execute(
            "SELECT conversation_id, provider, generation FROM conversations")}
        running = {row[0] for row in connection.execute(
            "SELECT DISTINCT conversation_id FROM turns WHERE state='running'")}
    owners = {hashlib.sha256(owner.encode()).hexdigest()[:32]: owner for owner in lineages}
    tasks = {str(task.session_id): task for task in state.tasks.all()}
    world_head = agent_git(broker, "rev-parse", "HEAD", cwd=world_root, timeout=60) if world_root else None
    for family, root in homes.items():
        if not broker.path_exists(root):
            continue
        for home in broker.child_directories(root):
            name = home.name
            if name.startswith(".steward-owner-"):
                prefix, _, key = name[len(".steward-owner-"):].partition("-")
                owner = owners.get(prefix)
                if owner is None or owner in running:
                    continue
                provider, generation = lineages[owner]
                current = {_home_key(p, generation + (p != provider)) for p in homes}
                task = tasks.get(owner)
                if task is not None:
                    finished = task.status.value in {"done", "cancelled"}
                    if task.status.value in {"running", "queued"} or task.tip is None:
                        continue
                    repository = repositories.get(task.repository)
                    target = (Path(repository), task.tip) if repository else None
                else:
                    finished = False
                    target = (Path(world_root), world_head) if world_head else None
                if (key in current and not finished) or target is None:
                    continue
            elif name.startswith(".steward-launch-"):
                if not world_head:
                    continue
                target = (Path(world_root), world_head)
            else:
                continue
            _retire(broker, family, home, target, quiet_seconds)


def _retire(broker, family, home: Path, target, quiet_seconds: int) -> None:
    from steward_harness.runtime.native_evidence import record_mappings

    repository, commit = target
    checked = broker.run([broker.python_executable, "-I", "-c", _VERIFY_HOME], cwd="/", timeout=600,
                         input_text=json.dumps([str(home), str(repository), commit,
                                                record_mappings(family), quiet_seconds]))
    if checked.returncode:
        log.warning("native home %s kept: verification failed: %s", home, checked.stderr.strip()[-300:])
        return
    verdict = json.loads(checked.stdout)
    if not verdict["retire"]:
        log.debug("native home %s kept: %s", home, verdict["reason"])
        return
    removed = broker.run([broker.python_executable, "-I", "-c",
                          "import shutil,sys; shutil.rmtree(sys.argv[1])", str(home)],
                         cwd="/", timeout=600)
    if removed.returncode:
        log.warning("native home %s could not be removed: %s", home, removed.stderr.strip()[-300:])
    else:
        log.info("retired native home %s: %d records held at %s", home, verdict["records"], commit[:12])
