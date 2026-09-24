"""Real Git/SQLite checks for the retain-versus-prune boundary."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from state_fixtures import prepare_turn, admit_task
from test_task_runner_kernel import (
    EditingAdapter, _repository, _git, publish_task,
)
from test_world_turn_checkpoint import _checkpoint, _git_world, _finish, _commit_all, _git_path
from steward_harness.cognition import Cognition
from steward_harness.config.schema import RepositoryConfig, UntrustedExecutionConfig
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.kernel import repository_lease
from steward_harness.retention import prune_tasks, prune_world_sessions
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import StateDatabase, TaskSpec
from steward_harness.task_runner import TaskRunner
from steward_harness.task_lock import task_lock


@pytest.mark.parametrize("boundary", [
    "published", "cancelled", "unpublished", "locked", "repository_busy",
    "dirty", "ignored", "unaccepted", "merge",
])
def test_task_retention(tmp_path, boundary, caplog):
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    task = admit_task(state, TaskSpec("app", "Result", "Create result.txt"), provider="claude")
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    runner = TaskRunner(
        state=state,
        repositories={"app": RepositoryConfig(path=str(clone), remote_url=str(bare))},
        transports={"app": ControllerGitTransport(state.path, "app", str(bare), "main", allow_local=True)},
        worktrees_root=tmp_path / "worktrees", broker=broker,
        cognition=Cognition({"claude": EditingAdapter()}), provider_fallbacks=(), timeout_seconds=30,
    )
    runner.prepare(task.task_id)
    path = runner.worktrees_root / str(task.task_id)
    if boundary == "cancelled":
        state.tasks.cancel(task.task_id)
    elif boundary != "unpublished":
        publish_task(runner)
    lock = None
    if boundary == "locked":
        lock = task_lock(state.tasks.locks_root, task.task_id)
        lock.acquire()
    elif boundary == "repository_busy":
        # A separate lease instance in the same thread still contends.
        lock = repository_lease(state, "app")
        lock.acquire()
    elif boundary == "dirty":
        (path / "result.txt").write_text("unfinished")
    elif boundary == "ignored":
        common = _git_path(path, "info/exclude")
        common.write_text("cache\n")
        (path / "cache").write_text("unaccepted")
    elif boundary == "unaccepted":
        (path / "extra").write_text("local")
        _commit_all(path, "local commit outside acceptance")
    elif boundary == "merge":
        _git_path(path, "MERGE_HEAD").write_text(_git("rev-parse", "HEAD", cwd=path))
    try:
        prune_tasks(runner)
    finally:
        if lock:
            lock.release()
    assert path.exists() == (boundary not in {"published", "cancelled"})
    if not path.exists():
        assert str(path) not in _git("worktree", "list", "--porcelain", cwd=clone)
        assert state.tasks.read(task.task_id)[1].work
        prune_tasks(runner)  # Restart/idempotence does not delete task history.
    if boundary in {"dirty", "ignored", "unaccepted", "merge"}:
        assert "retained task workspace" in caplog.text


@pytest.mark.parametrize("boundary", [
    "old", "recent", "running", "interrupted", "pending", "completion",
    "dirty", "ignored", "unaccepted", "merge",
])
def test_world_retention(tmp_path, boundary, caplog):
    checkpoint = _checkpoint(_git_world(tmp_path / "world"), tmp_path)
    state = checkpoint.state
    owner = state.get_or_create_conversation("telegram", "owner", provider="codex", profile="balanced")
    turn = checkpoint.checkout(workspace_id=owner.conversation_id.workspace)
    _finish(checkpoint, turn, "owner", "input", "reply")
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    if boundary != "recent":
        with state.connect(write=True) as connection:
            connection.execute("UPDATE turns SET completed_at=?", (old,))
    if boundary in {"running", "interrupted", "pending"}:
        active, _ = state.start_turn(owner.conversation_id, "next", "operator", "next")
        if boundary == "interrupted":
            state.interrupt_turn(active.turn_id, "interrupted")
        if boundary == "pending":
            prepare_turn(state, str(active.turn_id), world_root=str(checkpoint.world.root),
                base_sha=turn.base_sha, candidate_sha=_git("rev-parse", "HEAD", cwd=turn.path),
                output="reply", provider="codex", model="model", provider_session_id=None, profile="balanced")
    elif boundary == "completion":
        receipt = state.path.with_suffix(".world-completions") / (hashlib.sha256(str(owner.conversation_id).encode()).hexdigest() + ".json")
        receipt.parent.mkdir()
        receipt.write_text("{}")
    elif boundary == "dirty":
        (turn.path / "unfinished").write_text("local")
    elif boundary == "ignored":
        _git_path(turn.path, "info/exclude").write_text("cache\n")
        (turn.path / "cache").write_text("local")
    elif boundary == "unaccepted":
        (turn.path / "extra").write_text("local")
        _commit_all(turn.path, "unaccepted world work")
    elif boundary == "merge":
        _git_path(turn.path, "MERGE_HEAD").write_text(turn.base_sha)
    prune_world_sessions(checkpoint, 7 * 86400)
    assert turn.path.exists() == (boundary != "old")
    if boundary == "old":
        assert str(turn.path) not in _git("worktree", "list", "--porcelain", cwd=checkpoint.world.root)
        prune_world_sessions(checkpoint, 7 * 86400)
        assert checkpoint.checkout(workspace_id=owner.conversation_id.workspace).path.exists()
    if boundary in {"dirty", "ignored", "unaccepted", "merge", "completion"}:
        assert "retained world workspace" in caplog.text


def test_world_removal_serializes_new_turn_admission(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import steward_harness.retention as retention

    checkpoint = _checkpoint(_git_world(tmp_path / "world"), tmp_path)
    state = checkpoint.state
    owner = state.get_or_create_conversation("telegram", "owner", provider="codex", profile="balanced")
    turn = checkpoint.checkout(workspace_id=owner.conversation_id.workspace)
    _finish(checkpoint, turn, "owner", "input", "reply")
    with state.connect(write=True) as connection:
        connection.execute("UPDATE turns SET completed_at=?", ((datetime.now(UTC) - timedelta(days=10)).isoformat(),))
    removing, release, admitting = threading.Event(), threading.Event(), threading.Event()
    original = retention._remove

    def remove(*args):
        removing.set()
        assert release.wait(5)
        original(*args)

    def admit():
        admitting.set()
        state.start_turn(owner.conversation_id, "new", "operator", "new")
        return checkpoint.checkout(workspace_id=owner.conversation_id.workspace)

    monkeypatch.setattr(retention, "_remove", remove)
    with ThreadPoolExecutor(2) as pool:
        cleanup = pool.submit(prune_world_sessions, checkpoint, 7 * 86400)
        assert removing.wait(5)
        new_turn = pool.submit(admit)
        assert admitting.wait(5)
        try:
            assert not new_turn.done()
        finally:
            release.set()
        cleanup.result(timeout=10)
        assert new_turn.result(timeout=10).path.is_dir()
