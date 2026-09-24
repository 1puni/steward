"""Concrete operator control over the replacement state kernel."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from state_fixtures import FakeRemote, admit_task, close_task_slice
from steward_harness.task_lock import task_lock
from steward_harness.state import (
    CheckpointDisposition,
    StateDatabase,
    TaskSpec,
    TaskStatus,
    TurnId,
)


SHA_A = "a" * 40
SHA_B = "b" * 40


def _admit(
    state: StateDatabase,
    name: str,
    *,
    priority: int = 0,
):
    return admit_task(
        state,
        TaskSpec("repo", name, f"brief for {name}", priority),
    )


def test_pause_is_one_durable_boolean_singleton(tmp_path: Path) -> None:
    """A single bit is a file that is there or is not; nothing else can be said.

    The singleton `CHECK` that used to be asserted here was defending against a
    second row, which was only ever possible because the bit had been given a
    table to live in. A marker file has no second row to reject, and pausing
    twice or resuming an already-running scheduler are both no-ops rather than
    states to handle.
    """
    path = tmp_path / "state.db"
    state = StateDatabase(path)

    assert not state.paused()
    state.set_paused(True)
    assert state.paused()
    assert StateDatabase(path).paused()
    state.set_paused(True)
    assert state.paused()
    state.set_paused(False)
    assert not state.paused()
    state.set_paused(False)
    assert not state.paused()
    assert not StateDatabase(path).paused()
    assert (path.parent / "paused").exists() is False


def test_conversation_queries_profile_clear_and_concrete_turn_cancel(
    tmp_path: Path,
) -> None:
    state = StateDatabase(tmp_path / "state.db")
    first = state.get_or_create_conversation(
        "telegram",
        "10:4",
        provider="codex",
        profile="balanced",
    )
    second = state.get_or_create_conversation(
        "desk",
        "inbox/item",
        provider="claude",
        profile="fast",
    )
    turn, _started = state.start_turn(
        first.conversation_id, "update:1", "operator", "work"
    )

    assert state.find_conversation("telegram", "10:4") == first
    assert state.find_conversation("desk", "inbox/item") == second
    assert state.active_turn(first.conversation_id) == turn

    profiled = state.set_conversation_profile(first.conversation_id, "deep")
    assert profiled.profile == "deep"
    cleared = state.clear_conversation(first.conversation_id)
    assert cleared.generation == first.generation + 1
    assert cleared.provider_session_id is None
    assert state.active_turn(first.conversation_id) is None


def test_answer_and_retry_retain_task_identity_and_provider_lineage(
    tmp_path: Path,
) -> None:
    state = StateDatabase(tmp_path / "state.db")
    admitted = _admit(state, "question")
    task = state.tasks.get(admitted.task_id)
    state.bind_conversation_provider(
        state.tasks.get(task.task_id).session_id, "codex", "session-1"
    )
    close_task_slice(state, task.task_id, CheckpointDisposition.ASK,
        detail="Which option?",
    )

    answered = state.tasks.answer(task.task_id, "Use option B")

    assert answered.status is TaskStatus.QUEUED
    assert answered.task_id == task.task_id
    assert state.get_conversation(state.tasks.get(task.task_id).session_id).provider_session_id == "session-1"
    state.tasks.hold(task.task_id, "blocked", "gate failed")

    retried = state.tasks.retry(task.task_id, "gate policy corrected")

    assert retried.status is TaskStatus.QUEUED
    assert retried.task_id == task.task_id
    assert tuple((k, t) for _, k, t, _ in state.tasks.get(task.task_id).pending) == (
        ("answer", "Use option B"),
        ("retry", "gate policy corrected"),
    )

    state.tasks.hold(task.task_id, "blocked", "blocked again")
    state.tasks.retry(task.task_id)
    assert [
        text for kind, text in tuple((k, t) for _, k, t, _ in state.tasks.get(task.task_id).pending)
        if kind == "retry"
    ] == ["gate policy corrected"]


def test_task_list_priority_and_cancellation_are_transition_checked(
    tmp_path: Path,
) -> None:
    state = StateDatabase(tmp_path / "state.db")
    low = _admit(state, "low", priority=1)
    high = _admit(state, "high", priority=5)

    prioritized = state.tasks.set_priority(low.task_id, 10)
    assert prioritized.priority == 10
    assert [task.task_id for task in state.tasks.all()] == [
        low.task_id,
        high.task_id,
    ]
    assert all(task.status is TaskStatus.QUEUED and task.repository == "repo"
               for task in state.tasks.all())

    cancelled = state.tasks.cancel(high.task_id)
    assert cancelled.status is TaskStatus.CANCELLED
    assert state.tasks.cancelled(high.task_id)
    with pytest.raises(RuntimeError, match="waiting task"):
        state.tasks.answer(
            high.task_id, "not an answer to a waiting task"
        )

    lock = task_lock(state.tasks.locks_root, low.task_id)
    assert lock.acquire()
    state.tasks.cancel(low.task_id, "superseded by newer work")
    assert state.tasks.get(low.task_id).reason == "superseded by newer work"
    running = state.tasks.cancel(low.task_id)
    assert running.status is TaskStatus.RUNNING
    assert state.tasks.cancelled(low.task_id)
    # The withdrawal outlives this handle on the state directory.
    assert StateDatabase(state.path).tasks.cancelled(low.task_id)
    with pytest.raises(RuntimeError, match="inactive unfinished"):
        state.tasks.set_priority(low.task_id, 20)
    lock.release()

    close_task_slice(state, low.task_id, CheckpointDisposition.BLOCKED,
        detail="operator cancelled task",
    )
    terminal = state.tasks.get(low.task_id)
    assert terminal.status is TaskStatus.CANCELLED
    # Acting on the withdrawal retires it: `cancelled` is now written down,
    # where the marker only ever meant "stop what you are doing".
    assert state.tasks.cancelled(low.task_id)


def test_a_note_arriving_mid_slice_survives_the_slice_it_missed(
    tmp_path: Path,
) -> None:
    """Consumption is what the slice was *given*, not what existed when it ended.

    An operator input is deleted as the slice closes, because the commit it
    just made is the record that it ran with them. One that arrived after the
    prompt was built was never given to anything, so the boundary is when the
    slice opened: the accepted revision it was dispatched at.
    """
    state = StateDatabase(tmp_path / "state.db")
    task = _admit(state, "long-running")
    state.tasks.note(task.task_id, "Seen by this slice")

    opened_at = state.tasks.get(task.task_id).revision
    assert [text for _kind, text in tuple((k, t) for _, k, t, _ in state.tasks.get(task.task_id).pending)] == [
        "Seen by this slice"
    ]
    state.tasks.note(task.task_id, "Arrived while it was running")

    state.tasks.finish_slice(
        task.task_id,
        disposition=CheckpointDisposition.CONTINUE,
        opened_at=opened_at,
    )

    assert [text for _kind, text in tuple((k, t) for _, k, t, _ in state.tasks.get(task.task_id).pending)] == [
        "Arrived while it was running"
    ]


def test_a_blocked_task_takes_a_note_and_a_finished_one_does_not(
    tmp_path: Path,
) -> None:
    """An assessment is worth most about work that has stopped.

    Nothing consumes a task input until a slice runs, so a note on a blocked
    task simply waits for the retry that reads it. A task the store has nothing
    more to say about has no next slice, so a note there would be a dead write.
    """
    state = StateDatabase(tmp_path / "state.db")
    blocked = _admit(state, "parked-on-a-red-gate")
    state.tasks.hold(blocked.task_id, "blocked", "gate failed")

    state.tasks.note(blocked.task_id, "The gate fails for an unrelated reason.")

    assert [text for _kind, text in tuple((k, t) for _, k, t, _ in state.tasks.get(blocked.task_id).pending)] == [
        "The gate fails for an unrelated reason."
    ]

    finished = _admit(state, "already-landed")
    close_task_slice(state, finished.task_id, CheckpointDisposition.IDLE)
    FakeRemote().land(state, finished.task_id, "repo")
    assert state.tasks.get(finished.task_id).status is TaskStatus.DONE
    with pytest.raises(RuntimeError, match="completed task"):
        state.tasks.note(finished.task_id, "Nothing will ever read this.")


def test_retrying_a_withdrawn_task_retires_the_withdrawal(tmp_path: Path) -> None:
    """The operator's retry is their answer to their own stop.

    A promotion parked by a red gate blocks the task without ever reaching the
    marker, so a withdrawal recorded while it was publishing can outlive the
    work it aimed at. Re-queueing is the only moment anything knows it is no
    longer meant.
    """
    state = StateDatabase(tmp_path / "state.db")
    task = _admit(state, "withdrawn-then-retried")
    state.tasks.cancel(task.task_id)
    assert state.tasks.cancelled(task.task_id)
    state.tasks.hold(task.task_id, "blocked", "gate red")
    assert state.tasks.get(task.task_id).status is TaskStatus.CANCELLED

    state.tasks.retry(task.task_id)

    assert not state.tasks.cancelled(task.task_id)
    assert state.tasks.queued() == (task.task_id,)
