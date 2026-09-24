"""Concurrency follows task isolation and repository publication ownership."""

import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from state_fixtures import admit_task, advance
from steward_harness.repository_reconciler import RepositoryReconciler
from steward_harness.cognition import Cognition
from steward_harness.config.schema import CommandSpec, RepositoryConfig, UntrustedExecutionConfig
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.kernel import Dispatch, StewardKernel, repository_lease
from steward_harness.landing import merger as merger_module
from steward_harness.lease import Busy
from steward_harness.landing.merger import PromotionEngine
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import StateDatabase, TaskSpec, TaskStatus
from steward_harness.task_runner import TaskRunner
from steward_harness.task_lock import locked_tasks
from test_task_runner_kernel import EditingAdapter, _git, _repository


def until(kernel, condition, timeout=12):
    deadline = time.monotonic() + timeout
    while not condition():
        advance(kernel)
        assert time.monotonic() < deadline, "independent work did not progress"
        threading.Event().wait(0.01)
    kernel.dispatch.reap()


def build(tmp_path, adapter, *, gates=(), workers=4, state=None):
    repositories, transports, remotes = {}, {}, {}
    for name in ("app", "other"):
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        if not (root / "repo").exists():
            remote, clone = _repository(root)
        else:
            remote, clone = root / "origin.git", root / "repo"
        repositories[name] = RepositoryConfig(
            path=str(clone), remote_url=str(remote), gates=gates if name == "app" else (),
        )
        remotes[name] = remote
        transports[name] = ControllerGitTransport(
            tmp_path / "state.db", name, str(remote), "main", allow_local=True,
        )
    state = state or StateDatabase(tmp_path / "state.db")
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    tasks = TaskRunner(
        state=state, repositories=repositories, transports=transports,
        worktrees_root=tmp_path / "worktrees", broker=broker,
        cognition=Cognition({"claude": adapter}),
        provider_fallbacks=(), timeout_seconds=30,
    )
    ambient = RepositoryReconciler(
        state=state, repositories=repositories, transports=transports,
        worktrees_root=tmp_path / "worktrees", broker=broker,
    )
    kernel = StewardKernel(
        state, ambient, tasks,
        dispatch=Dispatch(workers),
    )
    return kernel, remotes


def admit(state, repository="app", title="Work"):
    # Admission is the branch, so it is opened in the clone `build` made for
    # this repository — which sits beside the state database it was given.
    return admit_task(
        state,
        TaskSpec(repository, title, "Write the requested result."),
        provider="claude",
        profile="balanced",

    ).task_id


class DistinctFiles(EditingAdapter):
    def execute(self, request):
        result = super().execute(request)
        (request.cwd / f"{request.cwd.name}.txt").write_text("independent work\n")
        return result


@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_publication_outage_blocks_its_task_but_a_defect_reaches_the_pass(
    tmp_path, monkeypatch, error_type,
):
    kernel, remotes = build(tmp_path, DistinctFiles(), workers=1)
    state = kernel.state
    task_id = admit(state)
    original = _git("rev-parse", "main", cwd=remotes["app"])
    run_agent_git = merger_module.run_agent_git

    def fail_publication(broker, *args, cwd=None, **kwargs):
        if str(cwd) == str(tmp_path / "app" / "repo") and args[0] == "show-ref":
            raise error_type("publication setup failed")
        return run_agent_git(broker, *args, cwd=cwd, **kwargs)

    monkeypatch.setattr(merger_module, "run_agent_git", fail_publication)
    try:
        if error_type is OSError:
            until(kernel, lambda: kernel.state.tasks.get(task_id).status is TaskStatus.BLOCKED)
            peer = admit(state, "other", "peer after outage")
            until(kernel, lambda: kernel.state.tasks.get(peer).status is TaskStatus.DONE)
            assert _git("show", f"main:{peer}.txt", cwd=remotes["other"]) == "independent work"
        else:
            with pytest.raises(ValueError, match="publication setup failed"):
                until(kernel, lambda: False)
            # A defect leaves the task exactly where it was: still owing a
            # publication, with nothing written down about the attempt.
            assert kernel.state.tasks.get(task_id).status is TaskStatus.RUNNING
            # Still owing a publication, which is now read off the task's own
            # branch rather than from a repository-level filter.
            assert state.tasks.get(task_id).publishable
            # A second tick used to raise again and admit nothing, because a
            # fault set a latch on the pool. There is no latch: the defect is
            # raised once, where the pass runs, and the pass is what leaves.
            # `test_shutdown` asserts that it takes the daemon and the lease.
        assert _git("rev-parse", "main", cwd=remotes["app"]) == original
        assert (tmp_path / "worktrees" / str(task_id)).is_dir()
        assert _git(
            "show",
            f"{state.tasks.get(task_id).branch}:{task_id}.txt",
            cwd=tmp_path / "app" / "repo",
        ) == "independent work"
    finally:
        kernel.stop()




def test_two_tasks_think_together_while_same_repository_publication_progresses(tmp_path):
    """Two tasks of one repository think at once, and its lane keeps moving.

    The repository lane used to be a deployment; publication is what is left of
    it. The property is unchanged: task locks are per task, so holding two of
    them in one repository does not stop that repository owning itself and
    landing a third task's work.
    """
    release = threading.Event()
    entered = set()
    lock = threading.Lock()

    class BlockTwo(DistinctFiles):
        def execute(self, request):
            with lock:
                blocking = len(entered) < 2
                if blocking:
                    entered.add(request.cwd.name)
            if blocking:
                assert release.wait(15)
            return super().execute(request)

    adapter = BlockTwo()
    kernel, remotes = build(tmp_path, adapter)
    state = kernel.state
    tasks = (admit(state, title="first"), admit(state, title="second"))
    try:
        until(kernel, lambda: len(entered) == 2)
        # Both hold their locks; `running` is those locks and nothing else.
        assert locked_tasks(kernel.state.tasks.locks_root) == {str(t) for t in tasks}
        base = _git("rev-parse", "main", cwd=remotes["app"])
        third = admit(state, title="third")
        until(kernel, lambda: kernel.state.tasks.get(third).status is TaskStatus.DONE)
        # The repository lane took its lease, gated and pushed, all while the
        # two blocked tasks were still inside cognition holding their own.
        assert _git("rev-parse", "main", cwd=remotes["app"]) != base
        assert _git("show", f"main:{third}.txt", cwd=remotes["app"]) == "independent work"
        assert not release.is_set()
        assert len(entered) == 2
        until(kernel, lambda: locked_tasks(kernel.state.tasks.locks_root) == {str(t) for t in tasks})
        release.set()
        until(kernel, lambda: all(kernel.state.tasks.get(task).status is TaskStatus.DONE for task in tasks))
        for task in tasks:
            assert _git("show", f"main:{task}.txt", cwd=remotes["app"]) == "independent work"
        assert len(adapter.requests) == 3
    finally:
        release.set()
        kernel.stop()


def test_slow_gate_allows_peer_cognition_and_other_repository_landing(tmp_path):
    marker, release = tmp_path / "gate-started", tmp_path / "release-gate"
    script = tmp_path / "gate.py"
    script.write_text(
        "from pathlib import Path\nimport time\n"
        f"Path({str(marker)!r}).touch()\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
    )
    import sys
    adapter = DistinctFiles()
    kernel, remotes = build(tmp_path, adapter, gates=(CommandSpec(argv=[sys.executable, str(script)]),))
    state = kernel.state
    first = admit(state, title="first")
    try:
        until(kernel, marker.exists)
        peer = admit(state, title="peer")
        other = admit(state, "other", "other")
        until(kernel, lambda: kernel.state.tasks.get(other).status is TaskStatus.DONE)
        until(kernel, lambda: kernel.state.tasks.get(peer).status not in (TaskStatus.QUEUED,))
        # The first task holds its repository: its gate is still running, so
        # it owes a publication and reads as in flight.
        assert kernel.state.tasks.get(first).status is TaskStatus.RUNNING
        assert not release.exists()
        release.touch()
        until(kernel, lambda: kernel.state.tasks.get(first).status is TaskStatus.DONE)
        until(kernel, lambda: kernel.state.tasks.get(peer).status is TaskStatus.DONE)
        files = _git("ls-tree", "--name-only", "main", cwd=remotes["app"]).splitlines()
        assert f"{peer}.txt" in files
        assert f"{first}.txt" in files
        assert len(adapter.requests) == 3
    finally:
        release.touch()
        kernel.stop()


def test_restart_recovers_all_checkpoints_without_repeating_cognition(tmp_path):
    adapter = DistinctFiles()
    original, remotes = build(tmp_path, adapter)
    tasks = (admit(original.state), admit(original.state, "other", "other"))
    for task in tasks:
        original.tasks.prepare(task)
    assert all(original.state.tasks.get(t).publishable for t in tasks)
    assert original.state.tasks.queued() == ()
    original.stop()

    class NoCognition(EditingAdapter):
        def execute(self, request):
            pytest.fail("recovery repeated accepted cognition")

    kernel, _ = build(tmp_path, NoCognition(), state=StateDatabase(tmp_path / "state.db"))
    kernel.state.set_paused(True)
    try:
        until(kernel, lambda: all(kernel.state.tasks.get(task).status is TaskStatus.DONE for task in tasks))
        for task, repository in zip(tasks, ("app", "other")):
            assert _git("show", f"main:{task}.txt", cwd=remotes[repository]) == "independent work"
    finally:
        kernel.stop()


def test_pause_prevents_new_task_execution(tmp_path):
    adapter = DistinctFiles()
    kernel, _ = build(tmp_path, adapter)
    task = admit(kernel.state)
    kernel.state.set_paused(True)
    advance(kernel)
    assert not adapter.requests
    kernel.stop()
    assert kernel.state.tasks.get(task).status is TaskStatus.QUEUED
    assert not adapter.requests


def test_saved_checkpoint_waits_for_its_writer_without_stalling_another_repository(tmp_path):
    """Its old name said "without holding repository capacity", and that was
    the two-pool reservation: a task slice used to occupy the task pool while
    the repository pool stood idle beside it. There is one budget now, so a
    slice does hold a slot — `workers=2` buys the peer repository its own — and
    what remains true is the property the test is actually about.

    The peer work used to be a deployment of `other`; it is `other` landing a
    task, which is the same lane keyed on the same owner."""
    saved, release = threading.Event(), threading.Event()
    adapter = DistinctFiles()
    kernel, remotes = build(tmp_path, adapter, workers=2)
    task = admit(kernel.state)

    work = kernel.tasks._work

    def held_work(task, *args):
        result = work(task, *args)
        # Pause after the real checkpoint, while its native writer owns the lock.
        if task.repository == "app":
            saved.set()
            assert release.wait(15)
        return result

    kernel.tasks._work = held_work
    try:
        until(kernel, saved.is_set)
        # The task holds its lock: that, and nothing in SQL, is `running`.
        assert locked_tasks(kernel.state.tasks.locks_root) == {str(task)}
        peer = admit(kernel.state, "other", "peer")
        until(kernel, lambda: kernel.state.tasks.get(peer).status is TaskStatus.DONE)
        assert _git("show", f"main:{peer}.txt", cwd=remotes["other"]) == "independent work"
        assert not release.is_set()
        release.set()
        until(kernel, lambda: kernel.state.tasks.get(task).status is TaskStatus.DONE)
        assert len(adapter.requests) == 2
    finally:
        release.set()
        kernel.stop()


_CONCURRENT_ADVANCE = """
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from steward_harness.kernel import Dispatch, StewardKernel
from steward_harness.state import StateDatabase

path, tag, writers = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
state = StateDatabase(path)
directory = path.parent


def publish_one(name):
    # Hold the repository for long enough that every peer has certainly tried.
    (directory / f"advanced-{tag}").write_text(name)
    time.sleep(2.0)


kernel = StewardKernel(
    state,
    SimpleNamespace(repositories={"app": None}, publish_one=publish_one),
    SimpleNamespace(),
    dispatch=Dispatch(1),
)
(directory / f"ready-{tag}").touch()
deadline = time.monotonic() + 60
while len(list(directory.glob("ready-*"))) < writers:
    assert time.monotonic() < deadline, "peers never started"
    time.sleep(0.01)
kernel._advance_repository("app")
kernel.stop()
"""


def test_only_one_process_advances_a_repository_at_once(tmp_path: Path) -> None:
    """The exclusion `scheduler_claim` was believed to give, actually given.

    `UNIQUE(repository)` on the claim row stopped a *second owner* being
    recorded. It never stopped a second driver of the *same* owner, because
    recovery is defined to hand the existing owner back to whoever asks — so
    two daemons over one state directory each received the same work and each
    ran it against the same repository checkout, concurrently. Nothing in
    SQLite was ever going to prevent that; the transaction had already
    committed by the time the gates started.

    So the property is stated where it can be enforced: the repository is
    locked before anything is read and stays locked until its owner is
    finished with, and a peer that cannot take the lock does not advance.
    Publication has no row at all now, which leaves the lock as the only
    exclusion there is — this is the test that it is enough.
    """
    state = StateDatabase(tmp_path / "state.db")
    writers = 3
    running = [
        subprocess.Popen(
            [sys.executable, "-c", _CONCURRENT_ADVANCE,
             str(state.path), f"p{index}", str(writers)],
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(writers)
    ]
    for writer in running:
        _, errors = writer.communicate(timeout=180)
        assert writer.returncode == 0, errors

    advanced = sorted(tmp_path.glob("advanced-*"))
    assert len(advanced) == 1, [item.name for item in advanced]
    assert advanced[0].read_text() == "app"
