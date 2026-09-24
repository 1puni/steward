"""Real Git/SQLite acceptance and restart, including the edited files."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.conversations import ConversationService
from steward_harness.runtime.contracts import ResolvedModel, RuntimeExecutionError, RuntimeResult, RuntimeInputResult
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import ConversationBusy, StateDatabase
from steward_harness.world.git_world import GitWorld
from steward_harness.lease import Busy, Lease
from steward_harness.world.turn_checkpoint import (
    WorldContentConflict,
    WorldTurnCheckpoint,
    WorldUpdatePending,
)


class Crash(BaseException):
    pass


class EditingCognition:
    def __init__(self, before_return=lambda request: None, repository="app"):
        self.calls = 0
        self.before_return = before_return
        self.repository = repository

    def run(self, request, *, execution_id=None):
        if callable(request):
            request = request()
        self.calls += 1
        (request.cwd / "decision.md").write_text(
            "The private consumer handoff is still outstanding.\n"
        )
        self.before_return(request)
        return RuntimeResult(
            output='Investigation saved.\nTASK_PROPOSAL: {"repository":"%s","title":"Private handoff","brief":"Connect the private consumer."}' % self.repository,
            resolved=ResolvedModel("codex", "balanced", "model", "medium"),
            effective_model="model",
            provider_session_id="session",
        )


def runtime(root: Path, cognition=None, repositories=frozenset({"app"})):
    world_path = root / "world"
    if not world_path.exists():
        subprocess.run(["git", "init", "-q", str(world_path)], check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=t@x",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "initial",
            ],
            cwd=world_path,
            check=True,
        )
    state = StateDatabase(root / "state.db")
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    world = GitWorld(world_path, execution_broker=broker)
    checkpoint = WorldTurnCheckpoint(
        world,
        Lease(root / "locks"),
        root / "worktrees",
        execution_broker=broker,
        state=state,
        # The daemon always supplies a resolver. This one declines, so a real
        # conflict surfaces as WorldContentConflict instead of a TypeError.
        resolve_turn=lambda _turn: None,
    )
    cognition = cognition or EditingCognition()
    service = ConversationService(
        state,
        cognition,
        provider_order=("codex",),
        profile="balanced",
        workspace=checkpoint,
        timeout_seconds=30,
    )
    state.tasks.repositories = repositories
    return state, checkpoint, service, cognition


from test_world_turn_checkpoint import _episodes


def run(adapter, event="event"):
    return adapter.run_turn(
        transport="telegram",
        transport_key="topic",
        source_event_key=event,
        operator_id="operator",
        text="Establish the private handoff.",
    )


@pytest.mark.parametrize("boundary", ["add", "commit"])
def test_completed_output_recovers_before_a_new_source(tmp_path, monkeypatch, boundary):
    state, checkpoint, service, cognition = runtime(tmp_path)
    original_git = GitWorld._git

    def fail_git(world, *args, **kwargs):
        if boundary in args:
            raise subprocess.CalledProcessError(128, ["git", *args], stderr="capture unavailable")
        return original_git(world, *args, **kwargs)

    monkeypatch.setattr(GitWorld, "_git", fail_git)
    with pytest.raises((subprocess.CalledProcessError, OSError)):
        run(service, "first")
    with state.connect() as connection:
        first = connection.execute("SELECT turn_id FROM turns").fetchone()[0]
    assert state.prepared_turn(first) is None
    assert _episodes(checkpoint.world.root) == []
    assert cognition.calls == 1
    # Same-process retries cannot hand the dirty checkout to a fresh source.
    with pytest.raises(WorldUpdatePending, match="awaits capture"):
        run(service, "second")
    assert cognition.calls == 1
    assert state.tasks.all() == []

    monkeypatch.setattr(GitWorld, "_git", original_git)
    reopened, restored, replay, _ = runtime(tmp_path, cognition)
    # Exercise recovery even if an earlier startup already abandoned the row.
    reopened.interrupt_abandoned_turns()

    def before_second(request):
        assert reopened.prepared_turn(first)["state"] == "completed"
        assert len(reopened.tasks.all()) == 1
        assert request.provider_session_id == "session"

    cognition.before_return = before_second
    second = run(replay, "second")
    first_again = run(replay, "first")
    assert str(first_again.turn_id) == first
    assert first_again.task_admission.task_id != second.task_admission.task_id
    assert cognition.calls == 2
    assert len(reopened.tasks.all()) == 2
    episodes = _episodes(restored.world.root)
    assert [episode["event_id"] for episode in episodes] == [first, str(second.turn_id)]
    assert reopened.claimed_turns() == []


def test_startup_recovers_completed_output_without_provider(tmp_path, monkeypatch):
    from steward_harness.daemon import StewardDaemon

    state, checkpoint, service, cognition = runtime(tmp_path)
    def crash(*args):
        raise Crash("provider finished, before capture")
    monkeypatch.setattr(checkpoint, "retain", crash)
    with pytest.raises(Crash):
        run(service)
    reopened, restored, replay, _ = runtime(tmp_path, cognition)
    StewardDaemon._recover_turns(reopened, replay, restored)
    assert len(reopened.tasks.all()) == 1
    assert cognition.calls == 1
    assert run(replay).task_admission.task_id == reopened.tasks.all()[0].task_id


def test_captured_completion_does_not_restore_a_cleared_native_session(tmp_path, monkeypatch):
    state, checkpoint, service, cognition = runtime(tmp_path)
    def fail(*args):
        raise OSError("capture unavailable")
    monkeypatch.setattr(checkpoint, "retain", fail)
    with pytest.raises(OSError):
        run(service)
    owner = service.conversation_for("telegram", "topic").conversation_id
    cleared = state.clear_conversation(owner)
    reopened, _, replay, _ = runtime(tmp_path, cognition)
    replay.recover_completion(owner)
    assert reopened.get_conversation(owner).generation == cleared.generation
    assert reopened.get_conversation(owner).provider_session_id is None
    assert len(reopened.tasks.all()) == 1
    assert cognition.calls == 1


def test_unretained_output_keeps_owner_fenced_across_restart(tmp_path, monkeypatch):
    from steward_harness.daemon import StewardDaemon

    state, checkpoint, service, cognition = runtime(tmp_path)
    retain = StateDatabase.retain_output
    def fail_output(*args, **kwargs):
        raise OSError("disk full retaining completed output")
    monkeypatch.setattr(StateDatabase, "retain_output", fail_output)
    with pytest.raises(OSError, match="disk full"):
        run(service, "first")
    with pytest.raises(ConversationBusy, match="inspect its native records"):
        run(service, "second")
    reopened, restored, replay, _ = runtime(tmp_path, cognition)
    # One uncertain owner must not stop the controller serving others.
    StewardDaemon._recover_turns(reopened, replay, restored)
    reopened.interrupt_abandoned_turns()
    with pytest.raises(ConversationBusy, match="inspect its native records"):
        run(replay, "second")
    assert cognition.calls == 1
    assert reopened.tasks.all() == []
    monkeypatch.setattr(StateDatabase, "retain_output", retain)
    other = replay.run_turn(transport="telegram", transport_key="other",
        source_event_key="other", operator_id="operator", text="Independent request.")
    assert other.task_admission is not None
    assert cognition.calls == 2


def test_empty_completion_still_accepts_world_and_replays(tmp_path, monkeypatch):
    class QuietCognition(EditingCognition):
        def run(self, request, **kwargs):
            return replace(super().run(request, **kwargs), output="")

    state, checkpoint, service, cognition = runtime(tmp_path, QuietCognition())
    retain = checkpoint.retain
    def fail(*args):
        raise OSError("capture unavailable")
    monkeypatch.setattr(checkpoint, "retain", fail)
    arguments = dict(transport="telegram", transport_key="topic", source_event_key="result",
                     operator_id="harness:task-result", text="Assess retained findings.",
                     allow_empty_output=True)
    with pytest.raises(OSError):
        service.run_turn(**arguments)
    monkeypatch.setattr(checkpoint, "retain", retain)
    reopened, restored, replay, _ = runtime(tmp_path, cognition)
    result = replay.run_turn(**arguments)
    assert result.reply_text == ""
    assert reopened.prepared_turn(str(result.turn_id))["state"] == "completed"
    assert (restored.world.root / "decision.md").exists()
    assert not reopened.tasks.all()
    assert cognition.calls == 1


def test_completion_custody_keeps_live_controller_input_attributed(tmp_path):
    delivered = []
    def during_native(request):
        request.on_session_started("codex", "session")
        def receive(source):
            delivered.append(source)
            request.on_input_result(RuntimeInputResult(source.source_id, "accepted"))
        request.on_input_ready(receive)
        with pytest.raises(ConversationBusy):
            service.run_turn(transport="telegram", transport_key="topic",
                source_event_key="observation", operator_id="harness:task-result",
                text="Controller evidence.")

    state, _, service, cognition = runtime(tmp_path, EditingCognition(during_native))
    accepted = run(service)
    assert len(delivered) == 1
    assert delivered[0].origin == "controller"
    assert delivered[0].author == "harness:task-result"
    attached = state.get_turn(delivered[0].source_id)
    assert str(attached.execution_turn_id) == str(accepted.turn_id)
    assert attached.input_disposition == "accepted"
    assert cognition.calls == 1


def test_accepted_replay_ignores_a_later_uncertain_checkout(tmp_path):
    state, checkpoint, service, cognition = runtime(tmp_path)
    first = run(service, "first")
    def crash(request):
        raise Crash("controller stopped during native work")
    cognition.before_return = crash
    with pytest.raises(Crash):
        run(service, "second")
    head = checkpoint.world.input_cursor()
    replay = run(service, "first")
    assert replay.task_admission.task_id == first.task_admission.task_id
    assert checkpoint.world.input_cursor() == head
    assert len(state.tasks.all()) == 1
    with pytest.raises(ConversationBusy):
        run(service, "third")
    assert cognition.calls == 2


def test_slow_completion_capture_does_not_lock_other_conversations(tmp_path, monkeypatch):
    state, checkpoint, service, _ = runtime(tmp_path)
    capturing, release, other_done = threading.Event(), threading.Event(), threading.Event()
    retain = checkpoint.retain
    errors = []
    def slow(worktree, event, text, reply, source):
        if text == "Establish the private handoff.":
            capturing.set()
            assert release.wait(10)
        return retain(worktree, event, text, reply, source)
    monkeypatch.setattr(checkpoint, "retain", slow)
    def first():
        try:
            run(service)
        except BaseException as error:
            errors.append(error)
    def second():
        try:
            service.run_turn(transport="telegram", transport_key="other", source_event_key="other",
                operator_id="operator", text="Independent conversation.")
            other_done.set()
        except BaseException as error:
            errors.append(error)
    slow_thread, other_thread = threading.Thread(target=first), threading.Thread(target=second)
    slow_thread.start()
    try:
        assert capturing.wait(5)
        other_thread.start()
        assert other_done.wait(5), "capture blocked an independent conversation"
    finally:
        release.set()
        slow_thread.join(10)
        if other_thread.ident is not None:
            other_thread.join(10)
    assert not errors
    assert len(state.tasks.all()) == 2


def test_native_checkpoint_marker_cannot_skip_world_capture(tmp_path):
    native_commits = []

    def native_commit(request):
        subprocess.run(
            [
                "git", "-c", "user.name=Native", "-c", "user.email=native@example.invalid",
                "commit", "--allow-empty", "--quiet",
                "-m", f"steward: checkpoint {request.execution_id}",
                "-m", f"Steward-Reply-SHA256: {hashlib.sha256(b'Investigation saved.').hexdigest()}",
            ],
            cwd=request.cwd, check=True,
        )
        native_commits.append(subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=request.cwd,
            check=True, capture_output=True, text=True,
        ).stdout.strip())

    state, checkpoint, service, cognition = runtime(tmp_path, EditingCognition(native_commit))
    accepted = run(service)
    assert (checkpoint.world.root / "decision.md").read_text().startswith("The private consumer")
    episodes = _episodes(checkpoint.world.root)
    assert len(episodes) == 1 and episodes[0]["event_id"] == str(accepted.turn_id)
    assert state.prepared_turn(str(accepted.turn_id))["state"] == "completed"
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", native_commits[0], "HEAD"],
        cwd=checkpoint.world.root, check=True,
    )
    head = checkpoint.world.input_cursor()
    replay = run(service)
    assert replay.task_admission.task_id == accepted.task_admission.task_id
    assert len(state.tasks.all()) == 1 and cognition.calls == 1
    assert checkpoint.world.input_cursor() == head


@pytest.mark.parametrize(
    "boundary", ["prepared", "applied", "accepted"]
)
def test_restart_retains_edits_and_admits_once(tmp_path, monkeypatch, boundary):
    state, checkpoint, adapter, cognition = runtime(tmp_path)
    target, method = {
        "prepared": (state, "record_candidate"),
        "applied": (checkpoint, "_apply_revision"),
        "accepted": (state, "accept_turn"),
    }[boundary]
    original = getattr(target, method)

    def fail_after(*args, **kwargs):
        original(*args, **kwargs)
        if boundary != "accepted":
            assert state.tasks.all() == []
        raise Crash(boundary)

    monkeypatch.setattr(target, method, fail_after)
    with pytest.raises(Crash):
        run(adapter)
    prepared = state.prepared_turn(next(iter(_events(state))))
    assert prepared is not None
    # Strip transient copies and run GC; the owner's retained workspace and the
    # world branch are what recovery needs.
    checkpoint.startup_cleanup()
    subprocess.run(
        ["git", "reflog", "expire", "--expire=now", "--all"],
        cwd=checkpoint.world.root,
        check=True,
    )
    subprocess.run(
        ["git", "gc", "--prune=now"],
        cwd=checkpoint.world.root,
        check=True,
        capture_output=True,
    )
    reopened, restored, replay, _ = runtime(tmp_path, cognition)
    assert reopened.interrupt_abandoned_turns() == 0
    result = run(replay)
    again = run(replay)
    assert result.reply_text == again.reply_text
    assert result.task_admission.task_id == again.task_admission.task_id
    assert len(reopened.tasks.all()) == 1
    assert cognition.calls == 1
    assert (
        (restored.world.root / "decision.md")
        .read_text()
        .startswith("The private consumer")
    )
    episodes = _episodes(restored.world.root)
    assert len(episodes) == 1
    assert "Task admitted" not in episodes[0]["steward"]
    assert sum(e["event_id"] == str(result.turn_id) for e in episodes) == 1


def _events(state):
    with state.connect() as connection:
        return [
            row[0] for row in connection.execute("SELECT turn_id FROM turns WHERE output IS NOT NULL")
        ]


def test_interrupted_native_records_survive_turn_cleanup_and_restart(tmp_path):
    original = '{"type":"native-original","session_id":"session"}\n'

    def interrupt(request):
        records = request.cwd / "artefacts/codex/sessions"
        records.mkdir(parents=True)
        (records / "rollout-session.jsonl").write_text(original)
        request.on_session_started("codex", "session")
        raise RuntimeExecutionError("native connection interrupted", session_id="session")

    cognition = EditingCognition(interrupt)
    state, checkpoint, inbound, _ = runtime(tmp_path, cognition)
    with pytest.raises(RuntimeExecutionError, match="connection interrupted"):
        run(inbound)
    retained = list(checkpoint.worktrees_root.glob("session-*"))
    assert len(retained) == 1
    assert _events(state) == []
    assert state.tasks.all() == []
    assert not (checkpoint.world.root / "decision.md").exists()

    reopened, recovered, replay, _ = runtime(tmp_path, cognition)
    recovered.startup_cleanup()
    assert (retained[0] / "artefacts/codex/sessions/rollout-session.jsonl").read_text() == original
    assert (retained[0] / "decision.md").is_file()
    with pytest.raises(RuntimeError, match="inspect its recorded history"):
        run(replay)
    assert cognition.calls == 1
    assert reopened.tasks.all() == []

    # A new message can recover in the retained conversation workspace even
    # though the interrupted source itself has no acceptance receipt to replay.
    def inspect_retained(request):
        assert request.cwd == retained[0]
        assert (request.cwd / "artefacts/codex/sessions/rollout-session.jsonl").read_text() == original

    cognition.before_return = inspect_retained
    completed = run(replay, "next-message")
    assert completed.task_admission is not None
    assert cognition.calls == 2
    assert len(reopened.tasks.all()) == 1


def test_startup_reclaims_only_integration_scratch(tmp_path):
    _, checkpoint, inbound, _ = runtime(tmp_path)
    accepted = run(inbound)
    # Historical copies may have local evidence beyond their accepted candidate.
    # Create them with native Git; current cognition always has a retained owner.
    names = (
        "event-crashed-event",
        f"event-{accepted.turn_id}",
        "a" * 32,
        "work-in-progress",
        "integration-not-scratch",
    )
    for name in names:
        path = checkpoint.worktrees_root / name
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(path), "HEAD"],
            cwd=checkpoint.world.root, check=True, capture_output=True,
        )
        (path / "partial.md").write_text("only copy of partial work")
    checkpoint.startup_cleanup()
    assert not (checkpoint.worktrees_root / names[-1]).exists()
    for name in names[:-1]:
        assert (checkpoint.worktrees_root / name / "partial.md").read_text() == "only copy of partial work"


def test_one_conversation_owns_multiple_tasks(tmp_path, monkeypatch):
    state, _, inbound, cognition = runtime(tmp_path)
    first = run(inbound, "first-task")
    execute = cognition.run
    monkeypatch.setattr(
        cognition,
        "run",
        lambda request, **kwargs: replace(
            execute(request, **kwargs), output="Let's discuss the layout too."
        ),
    )
    between = run(inbound, "other-subject")
    assert between.conversation_id == first.conversation_id
    assert between.task_admission is None
    monkeypatch.setattr(cognition, "run", execute)
    second = run(inbound, "second-task")
    assert first.conversation_id == second.conversation_id
    assert first.task_admission.task_id != second.task_admission.task_id
    assert cognition.calls == 3
    assert len(state.tasks.all()) == 2
    for event, original in [("first-task", first), ("second-task", second)]:
        replay = run(inbound, event)
        assert replay.reply_text == original.reply_text
        assert replay.task_admission.task_id == original.task_admission.task_id
    assert cognition.calls == 3
    assert state.pending_turns() == []

    # Each task still delivers to the same originating thread independently.
    for result in (first, second):
        state.tasks.cancel(
            result.task_admission.task_id,
            "operator changed direction",
        )
    # Both tasks report back to the one conversation that admitted them.
    assert state.pending_task_result_conversations() == (first.conversation_id,)
    delivery = state.pending_task_result_for(
        first.conversation_id
    )
    assert delivery is not None
    assert str(delivery[0]) in delivery[1]

    execute = cognition.run
    monkeypatch.setattr(
        cognition,
        "run",
        lambda request, **kwargs: replace(
            execute(request, **kwargs), output="Let's discuss another subject."
        ),
    )
    discussion = run(inbound, "later-discussion")
    assert discussion.conversation_id == first.conversation_id
    assert discussion.task_admission is None
    assert discussion.reply_text == "Let's discuss another subject."
    assert len(state.tasks.all()) == 2
    assert state.active_turn(first.conversation_id) is None


def test_revocation_before_acceptance_and_replay_are_distinct(tmp_path, monkeypatch):
    state, checkpoint, adapter, cognition = runtime(tmp_path)
    original = state.record_candidate

    def stop(*args, **kwargs):
        original(*args, **kwargs)
        raise Crash()

    monkeypatch.setattr(state, "record_candidate", stop)
    with pytest.raises(Crash):
        run(adapter)
    state, checkpoint, adapter, _ = runtime(
        tmp_path, cognition, repositories=set()
    )
    rejected = run(adapter)
    assert rejected.task_admission is None and rejected.task_rejection
    assert state.tasks.all() == []
    _, _, replay, _ = runtime(tmp_path, cognition, repositories=frozenset({"app"}))
    assert run(replay).reply_text == rejected.reply_text
    assert state.tasks.all() == []
    assert cognition.calls == 1


def test_invalid_workspace_does_not_leave_a_running_turn(tmp_path):
    state, checkpoint, adapter, cognition = runtime(tmp_path)
    conversation = adapter.conversation_for("telegram", "topic")
    worktree = checkpoint.checkout(
        workspace_id=f"conversation-{conversation.conversation_id}",
    )
    git_file = worktree.path / ".git"
    original = git_file.read_text()
    git_file.write_text(f"gitdir: {tmp_path / 'missing-git'}\n")

    with pytest.raises(RuntimeError, match="not a git repository"):
        run(adapter)
    assert cognition.calls == 0
    with state.connect() as connection:
        assert (
            connection.execute("SELECT state FROM turns").fetchone()[0] == "interrupted"
        )
    git_file.write_text(original)
    accepted = run(adapter, "after-workspace-repair")
    assert cognition.calls == 1
    assert state.prepared_turn(str(accepted.turn_id))["state"] == "completed"


def test_unknown_epoch_is_untouched(tmp_path):
    state = StateDatabase(tmp_path / "state.db")
    with state.connect(write=True) as connection:
        connection.execute("UPDATE steward_schema SET epoch=999")
    with sqlite3.connect(state.path) as connection:
        before = list(connection.iterdump())
    with pytest.raises(ValueError, match="preserved"):
        StateDatabase(state.path)
    with sqlite3.connect(state.path) as connection:
        assert list(connection.iterdump()) == before


@pytest.mark.parametrize("epoch", [11, 12])
def test_previous_epoch_preserves_real_tasks_for_explicit_upgrade(tmp_path, epoch):
    state, _, adapter, _ = runtime(tmp_path)
    result = run(adapter)
    state.tasks.note(result.task_admission.task_id, "Keep this operator note.")
    with state.connect(write=True) as connection:
        connection.execute("UPDATE steward_schema SET epoch=?", (epoch,))
    with sqlite3.connect(state.path) as connection:
        before = list(connection.iterdump())
    with pytest.raises(ValueError, match="requires explicit upgrade"):
        StateDatabase(state.path)
    with sqlite3.connect(state.path) as connection:
        assert list(connection.iterdump()) == before
    assert len(state.tasks.all()) == 1
    assert tuple((k, t) for _, k, t, _ in state.tasks.get(result.task_admission.task_id).pending) == (("note", "Keep this operator note."),)



@pytest.mark.parametrize("same_file", [False, True])
def test_concurrent_world_turns_keep_both_candidates(tmp_path, same_file):
    state, checkpoint, adapter, cognition = runtime(tmp_path)
    barrier = threading.Barrier(2, timeout=10)

    def write(request):
        name = threading.current_thread().name
        (request.cwd / "decision.md").unlink()
        (request.cwd / ("shared.md" if same_file else f"{name}.md")).write_text(
            name + "\n"
        )
        barrier.wait()

    cognition.before_return = write
    results, errors = [], []

    def work(topic):
        try:
            results.append(
                adapter.run_turn(
                    transport="telegram",
                    transport_key=topic,
                    source_event_key=topic,
                    operator_id="operator",
                    text="Investigate.",
                )
            )
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=work, args=(name,), name=name)
        for name in ["one", "two"]
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    assert cognition.calls == 2, [repr(error) for error in errors]
    if same_file:
        assert len(results) == 1
        assert len(errors) == 1 and isinstance(errors[0], WorldContentConflict)
        pending = state.pending_turns()
        assert len(pending) == 1
        assert len(state.tasks.all()) == 1
        retained = subprocess.run(
            ["git", "show", f"{pending[0]['candidate_sha']}:shared.md"],
            cwd=checkpoint.world.root, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert retained in {"one", "two"}
        assert retained != (checkpoint.world.root / "shared.md").read_text().strip()
    else:
        assert errors == []
        assert len(results) == 2 and len(state.tasks.all()) == 2
        for name in ["one", "two"]:
            assert (checkpoint.world.root / f"{name}.md").read_text() == name + "\n"


def test_sql_failure_preserves_git_task_and_replay_admits_once(tmp_path, monkeypatch):
    """The bookkeeping half of admission is still all-or-nothing.

    The branch is the task and is created first, so it outlives a failure here;
    that half is asserted in `test_kernel_state`. What this pins is the rest of
    the transaction — the turn, the world turn and the row move together, and a
    replay admits once without re-running cognition.
    """
    state, checkpoint, adapter, cognition = runtime(tmp_path)
    original = state._insert_task

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise Crash("after insertion, before SQL commit")

    monkeypatch.setattr(state, "_insert_task", fail)
    with pytest.raises(Crash):
        run(adapter)
    assert len(state.tasks.all()) == 1
    with state.connect() as connection:
        assert connection.execute("SELECT state FROM turns").fetchone()[0] == "running"
    assert (checkpoint.world.root / "decision.md").exists()
    state, _, replay, _ = runtime(tmp_path, cognition)
    run(replay)
    assert len(state.tasks.all()) == 1 and cognition.calls == 1


def test_an_applied_turn_awaiting_acceptance_does_not_fence_other_writers(tmp_path, monkeypatch):
    """Applied is a fact of the world's history, so no receipt must order writers."""
    state, checkpoint, adapter, cognition = runtime(tmp_path)
    original = state.accept_turn

    def fail(*args, **kwargs):
        raise Crash("before finalization")

    monkeypatch.setattr(state, "accept_turn", fail)
    with pytest.raises(Crash):
        run(adapter)
    monkeypatch.setattr(state, "accept_turn", original)
    adapter.run_turn(
        transport="telegram",
        transport_key="other",
        source_event_key="other",
        operator_id="operator",
        text="Another request.",
    )
    head = checkpoint.world.input_cursor()
    for pending in state.pending_turns():
        adapter.accept_prepared(pending["turn_id"])
    assert checkpoint.world.input_cursor() == head
    assert len(state.tasks.all()) == 2
    assert len(_episodes(checkpoint.world.root)) == 2


@pytest.mark.parametrize("control", ["clear", "switch"])
def test_prepared_recovery_preserves_new_conversation_lineage(
    tmp_path, monkeypatch, control
):
    state, checkpoint, adapter, cognition = runtime(tmp_path)
    original = state.record_candidate

    def stop(*args, **kwargs):
        original(*args, **kwargs)
        raise Crash()

    monkeypatch.setattr(state, "record_candidate", stop)
    with pytest.raises(Crash):
        run(adapter)
    conversation = state.find_conversation("telegram", "topic")
    if control == "clear":
        changed = state.clear_conversation(conversation.conversation_id)
    else:
        changed = state.bind_conversation_provider(
            conversation.conversation_id, "other", None
        )
    _, _, replay, _ = runtime(tmp_path, cognition)
    result = run(replay)
    recovered = state.find_conversation(conversation.transport, conversation.transport_key)
    assert recovered.provider == changed.provider
    assert recovered.generation == changed.generation
    assert recovered.provider_session_id is None
    # Both controls rotate the session and neither touches the prepared
    # revision: the turn's work lands either way.
    assert result.task_admission is not None
    assert (checkpoint.world.root / "decision.md").exists()


def test_historical_completion_is_not_replayed_as_new_acceptance(tmp_path):
    state, _, adapter, cognition = runtime(tmp_path)
    accepted = run(adapter)
    with state.connect(write=True) as connection:
        connection.execute(
            "UPDATE turns SET output=NULL WHERE turn_id=?", (str(accepted.turn_id),)
        )
    with pytest.raises(RuntimeError, match="durable acceptance receipt"):
        run(adapter)
    assert cognition.calls == 1
    assert len(state.tasks.all()) == 1
    assert state.prepared_turn(str(accepted.turn_id)) is None


def test_busy_checkout_replays_same_source_after_restart(tmp_path, monkeypatch):
    state, checkpoint, service, cognition = runtime(tmp_path)
    def busy(**_kwargs):
        raise Busy("busy (thread mutex contention)")
    monkeypatch.setattr(checkpoint, "checkout", busy)
    with pytest.raises(Busy):
        run(service)
    with state.connect() as connection:
        assert connection.execute("SELECT count(*) FROM turns").fetchone()[0] == 0
    assert cognition.calls == 0
    state, _, replay, _ = runtime(tmp_path, cognition)
    accepted = run(replay)
    assert state.prepared_turn(str(accepted.turn_id))["state"] == "completed"
    assert cognition.calls == 1 and len(state.tasks.all()) == 1
    assert run(replay).turn_id == accepted.turn_id
    assert cognition.calls == 1


def test_completed_turn_does_not_acquire_world_for_obsolete_cleanup(tmp_path, monkeypatch):
    state, checkpoint, service, cognition = runtime(tmp_path)
    def busy(**_kwargs):
        raise Busy("peer now owns the world")
    monkeypatch.setattr(checkpoint, "startup_cleanup", busy)
    accepted = run(service)
    assert state.prepared_turn(str(accepted.turn_id))["state"] == "completed"
    assert cognition.calls == 1


def test_conversation_retains_accepted_content_and_ignored_environment(tmp_path):
    paths = []

    def edit(request):
        paths.append(request.cwd)
        svg = request.cwd / "allmanningen-qr-50x50mm.svg"
        if len(paths) == 1:
            (request.cwd / ".gitignore").write_text(".venv/\n")
            (request.cwd / ".venv").mkdir()
            (request.cwd / ".venv" / "installed").write_text("keep environment")
            svg.write_text("<svg>first</svg>")
        else:
            assert request.cwd == paths[0]
            assert svg.read_text() == "<svg>first</svg>"
            assert (request.cwd / ".venv" / "installed").read_text() == "keep environment"
            assert request.provider_session_id == "session"
            svg.write_text("<svg>revised</svg>")

    cognition = EditingCognition(edit)
    _, checkpoint, inbound, _ = runtime(tmp_path, cognition)
    run(inbound, "create-svg")
    assert paths[0].is_dir()
    assert (checkpoint.world.root / "allmanningen-qr-50x50mm.svg").is_file()
    _, restarted, inbound, _ = runtime(tmp_path, cognition)
    restarted.startup_cleanup()
    run(inbound, "revise-svg")
    assert (checkpoint.world.root / "allmanningen-qr-50x50mm.svg").read_text() == "<svg>revised</svg>"
    assert not (checkpoint.world.root / ".venv").exists()


def test_new_message_resumes_interrupted_workspace_without_losing_partial_work(tmp_path):
    paths = []

    def interrupted(request):
        paths.append(request.cwd)
        (request.cwd / "partial.svg").write_text("unfinished")
        raise RuntimeExecutionError("interrupted")

    cognition = EditingCognition(interrupted)
    _, checkpoint, inbound, _ = runtime(tmp_path, cognition)
    with pytest.raises(RuntimeExecutionError):
        run(inbound, "interrupted")

    def resume(request):
        assert request.cwd == paths[0]
        assert (request.cwd / "partial.svg").read_text() == "unfinished"
        (request.cwd / "partial.svg").write_text("finished")

    cognition.before_return = resume
    _, restarted, inbound, _ = runtime(tmp_path, cognition)
    restarted.startup_cleanup()
    run(inbound, "continue")
    assert (checkpoint.world.root / "partial.svg").read_text() == "finished"


def test_pending_checkpoint_blocks_only_its_owner_until_recovery(tmp_path, monkeypatch):
    state, checkpoint, inbound, cognition = runtime(tmp_path)

    def crash(*args, **kwargs):
        raise Crash("before application")

    with monkeypatch.context() as patch:
        patch.setattr(checkpoint, "apply", crash)
        with pytest.raises(Crash):
            run(inbound, "prepared")
    assert len(state.pending_turns()) == 1
    with pytest.raises(ConversationBusy, match="owning conversation is busy"):
        run(inbound, "too-early")
    assert cognition.calls == 1
    other = checkpoint.checkout(workspace_id="conversation-independent")
    assert other.path.is_dir()
    run(inbound, "prepared")
    run(inbound, "after-recovery")
    assert cognition.calls == 2
    assert not state.pending_turns()


def test_an_episode_records_what_prompted_the_turn_not_the_prompt(tmp_path):
    """The world remembers the observation, not the instruction that carried it.

    A task-result turn's prompt is a page of controller preamble wrapped around
    a brief that is already committed on the task's own branch. Recording it as
    the episode's input stores it a second time and teaches the next pass
    nothing — and it is the same constant every time, so 333 of gg's 558
    episodes were 82% harness boilerplate by the time this was measured.

    The provider must still be asked the whole thing. This asserts both halves:
    the prompt keeps the preamble, the episode does not.
    """
    prompts: list[str] = []
    state, checkpoint, service, cognition = runtime(
        tmp_path, EditingCognition(lambda request: prompts.append(request.prompt))
    )
    preamble = "## Harness task result\nThis is a controller observation, not a new operator request."

    accepted = service.run_turn(
        transport="telegram",
        transport_key="topic",
        source_event_key="task_result:task_x",
        operator_id="harness:task-result",
        text=f"{preamble}\n\nTask brief:\nSomething long and already committed elsewhere.",
        episode_input="Harness task result for task_x in steward-harness.",
    )

    episodes = _episodes(checkpoint.world.root)
    recorded = next(e for e in episodes if e["event_id"] == str(accepted.turn_id))

    assert recorded["user"] == "Harness task result for task_x in steward-harness."
    assert "controller observation" not in recorded["user"]
    assert "Task brief:" not in recorded["user"]
    # The turn itself is unchanged: the provider was asked the whole prompt.
    assert prompts and "controller observation" in prompts[-1]


def test_an_operator_message_is_remembered_as_the_operator_wrote_it(tmp_path):
    """Only the harness's own speech is summarised; the operator's is not.

    `episode_input` defaults to the turn's text, so the change above cannot
    quietly start abbreviating what a person actually said.
    """
    state, checkpoint, service, _cognition = runtime(tmp_path)
    accepted = run(service)

    episodes = _episodes(checkpoint.world.root)
    recorded = next(e for e in episodes if e["event_id"] == str(accepted.turn_id))
    assert recorded["user"] == "Establish the private handoff."


def test_an_empty_reply_releases_its_owner_like_any_provider_failure(tmp_path):
    class Quiet(EditingCognition):
        quiet = True

        def run(self, request, **kwargs):
            result = super().run(request, **kwargs)
            return replace(result, output="") if self.quiet else result

    state, checkpoint, service, cognition = runtime(tmp_path, Quiet())
    with pytest.raises(RuntimeError, match="empty world-session reply"):
        run(service, "quiet")
    assert state.claimed_turns() == []
    cognition.quiet = False
    accepted = run(service, "next")
    assert accepted.task_admission is not None
    # The empty turn's edit was kept in the checkout for the next source.
    assert (checkpoint.world.root / "decision.md").is_file()
