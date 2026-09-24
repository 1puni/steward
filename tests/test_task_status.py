"""A task's execution is its accepted document, its retained slices and a lock."""

from __future__ import annotations

import threading
from pathlib import Path

from state_fixtures import admit_task, close_task_slice
import pytest

from steward_harness.state import StateDatabase, TaskId, TaskSpec, TaskStatus
from steward_harness.lease import Busy
from steward_harness.task_lock import locked_tasks, task_lock


def test_running_is_a_held_lock_and_a_dead_holder_releases_it(tmp_path: Path) -> None:
    state = StateDatabase(tmp_path / "state.db")
    locks = state.tasks.locks_root
    task = admit_task(state, TaskSpec("app", "t", "b"))

    lock = task_lock(locks, task.task_id)
    assert lock.acquire()
    try:
        assert state.tasks.get(task.task_id).status is TaskStatus.RUNNING
        assert locked_tasks(locks) == {str(task.task_id)}
        # A second holder is refused even inside this process: `flock` binds to
        # the open file description, so the check and the claim are one act.
        with pytest.raises(Busy):
            task_lock(locks, task.task_id).acquire()
    finally:
        lock.release()

    # Releasing is all it takes to stop being running. Nothing had to notice,
    # which is why a crashed worker needs no recovery pass.
    assert state.tasks.get(task.task_id).status is TaskStatus.QUEUED
    assert locked_tasks(locks) == frozenset()


def test_concurrent_acquirers_admit_exactly_one(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    taken, attempted, barrier = [], threading.Barrier(4), threading.Barrier(4)

    def claim() -> None:
        lock = task_lock(locks, TaskId("contended"))
        barrier.wait(5)
        try:
            lock.acquire()
        except Busy:
            attempted.wait(5)
            return
        taken.append(lock)
        attempted.wait(5)
        lock.release()

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(taken) == 1


def test_the_slice_carries_its_question_its_findings_and_its_history(tmp_path: Path) -> None:
    """The disposition is what the slice decided, the reason is what it stopped
    on, and the body is what it concluded — for an investigation that changed
    no file, that body is the entire product of the task."""
    state = StateDatabase(tmp_path / "state.db")
    task_id = admit_task(state, TaskSpec("app", "t", "b")).task_id
    unrun = state.tasks.get(task_id)
    assert unrun.work_sha is None
    assert state.tasks.checkpoints(unrun, 5) == ()
    assert unrun.findings is None

    close_task_slice(state, task_id, "continue", findings="Slice 1: found the consumer.")
    close_task_slice(state, task_id, "ask", detail="Which consumer is authoritative?",
                     findings="Slice 2: two consumers read the old feed.")
    task = state.tasks.get(task_id)
    assert task.status is TaskStatus.WAITING
    assert task.reason == "Which consumer is authoritative?"
    assert task.findings == "Slice 2: two consumers read the old feed."
    assert [(item.disposition.value, item.question) for item in state.tasks.checkpoints(task, 5)] == [
        ("ask", "Which consumer is authoritative?"), ("continue", None),
    ]

    # A written decision carries its own reason, whatever the slice said.
    state.tasks.hold(task_id, "blocked", "gate red")
    assert state.tasks.get(task_id).reason == "gate red"

    # A slice that did not stop to ask is owed no explanation.
    kept_going = admit_task(state, TaskSpec("app", "u", "b")).task_id
    close_task_slice(state, kept_going, "continue")
    assert state.tasks.get(kept_going).reason is None
    assert state.tasks.get(kept_going).findings is None


def test_a_lock_nobody_can_hold_is_not_running(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    (locks / "odd.lock").mkdir(parents=True)
    (locks / "loop.lock").symlink_to(locks / "loop.lock")
    assert locked_tasks(locks) == frozenset()
