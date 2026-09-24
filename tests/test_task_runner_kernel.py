"""Task preparation and exact publication through real Git and SQLite."""

from __future__ import annotations

import subprocess
import sys
from steward_harness.deploy.config import SystemdReleaseConfig

from dataclasses import replace
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import pytest

from state_fixtures import admit_task
from steward_harness.cognition import Cognition
from steward_harness.config.schema import (
    CommandSpec,
    RepositoryConfig,
    UntrustedExecutionConfig,
)
from steward_harness.deploy.engine import DeployEngine
from steward_harness.deploy.release import ReleaseManager
from steward_harness.git_transport import ControllerGitTransport, GitTransportError
from steward_harness.runtime.contracts import (
    SESSION_WORKSPACE_CAPABILITIES,
    Availability,
    MissingProviderSession,
    RuntimeRequest,
    RuntimeResult,
    RuntimeInputResult,
)
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import (
    CheckpointDisposition,
    StateDatabase,
    TaskId,
    TaskSpec,
    TaskStatus,
)
from steward_harness.task_runner import TaskRunner
def task_status(runner: TaskRunner, task_id: TaskId) -> TaskStatus:
    """Status as the harness reports it."""
    return runner.state.tasks.get(task_id).status


def publish_task(runner: TaskRunner) -> TaskId | None:
    """Publish whatever the repositories now owe, which is a branch question."""
    from steward_harness.repository_reconciler import RepositoryReconciler

    reconciler = RepositoryReconciler(
        state=runner.state, repositories=runner.repositories, transports=runner.transports,
        worktrees_root=runner.worktrees_root, broker=runner.broker,
    )
    for repository in runner.repositories:
        landed = reconciler.publish_repository(repository)
        if landed is not None:
            return landed
    return None


def run_task(runner: TaskRunner, **publication) -> TaskId | None:
    """Exercise one queued cognition turn and its submitted promotion."""
    queued = runner.state.tasks.queued()
    if not queued:
        return publish_task(runner, **publication)
    task_id = runner.prepare(queued[0])
    publish_task(runner, **publication)
    return task_id


def latest_slice(runner: TaskRunner, task_id: TaskId):
    """The newest slice this task closed, read off the commit that closed it."""
    history = runner.state.tasks.checkpoints(runner.state.tasks.get(task_id), 1)
    return history[0] if history else None


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "origin.git"
    _git("init", "-q", "-b", "main", "--bare", str(bare), cwd=tmp_path)
    clone = tmp_path / "repo"
    _git("clone", "-q", str(bare), str(clone), cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=clone)
    _git("config", "user.email", "test@example.invalid", cwd=clone)
    (clone / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=clone)
    _git("commit", "-q", "-m", "base", cwd=clone)
    _git("push", "-q", "origin", "main", cwd=clone)
    return bare, clone


class EditingAdapter:
    family = "claude"
    capabilities = SESSION_WORKSPACE_CAPABILITIES

    def __init__(self, *, local_commits: bool = False) -> None:
        self.requests: list[RuntimeRequest] = []
        self.local_commits = local_commits

    def available(self) -> Availability:
        return Availability(True)

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        request.on_session_started("claude-session")
        if self.local_commits:
            (request.cwd / "result.txt").write_text("native draft\n")
            _git("add", "result.txt", cwd=request.cwd)
            _git("commit", "-qm", "native: draft result", cwd=request.cwd)
        (request.cwd / "result.txt").write_text("completed\n")
        if self.local_commits:
            _git("add", "result.txt", cwd=request.cwd)
            _git("commit", "-qm", "native: complete result", cwd=request.cwd)
        output = (
            "Implemented the requested result.\n"
            "COMMIT: feat: add task result\nDISPOSITION: idle\nQUESTION: NONE"
        )
        return RuntimeResult(
            output=output,
            resolved=request.resolved,
            effective_model=request.resolved.model,
            provider_session_id="claude-session",
        )

class MoveOnceTransport(ControllerGitTransport):
    def __init__(
        self,
        state_db: str | Path,
        repository: str,
        remote_url: str,
        branch: str,
        *,
        allow_local: bool,
        move_remote: Callable[[], None],
    ) -> None:
        super().__init__(
            state_db,
            repository,
            remote_url,
            branch,
            allow_local=allow_local,
        )
        self._move_remote = move_remote
        self._moved = False

    def push_candidate(self, tested_sha: str, expected_base_sha: str) -> bool:
        if not self._moved:
            self._moved = True
            self._move_remote()
        return super().push_candidate(tested_sha, expected_base_sha)


class ExplodingCheckpointState(StateDatabase):
    def __init__(self, path) -> None:
        super().__init__(path)
        finish = self.tasks.finish_slice

        def exploding(*args: Any, **kwargs: Any) -> NoReturn:
            finish(*args, **kwargs)
            raise ValueError("unexpected state defect after closing the task slice")
        self.tasks.finish_slice = exploding


class InterruptingAdapter(EditingAdapter):
    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        request.on_session_started("claude-interrupted")
        (request.cwd / "partial.txt").write_text("preserve me\n")
        raise KeyboardInterrupt("process stopped before checkpoint")


class AskingOnceAdapter(EditingAdapter):
    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        first = not self.requests
        result = super().execute(request)
        return replace(result, output=(
            "More context is needed.\nCOMMIT: feat: add task result\n"
            "DISPOSITION: ask\nQUESTION: Which option should I use?"
        )) if first else result


class CancellingAdapter(EditingAdapter):
    def __init__(self, cancel: Callable[[], None]) -> None:
        super().__init__()
        self._request_cancel = cancel

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        request.on_session_started("claude-cancelled")
        (request.cwd / "partial.txt").write_text("preserve this\n")
        self._request_cancel()
        return RuntimeResult(
            output="Cancellation arrived after useful partial work.",
            resolved=request.resolved,
            effective_model=request.resolved.model,
            provider_session_id="claude-cancelled",
        )


class SwitchingContinueAdapter(EditingAdapter):
    def __init__(self, switch: Callable[[], None], *, before_start: bool = False) -> None:
        super().__init__()
        self._switch = switch
        self._before_start = before_start

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        if self._before_start:
            self._switch()
        request.on_session_started("claude-session")
        (request.cwd / "phase-one.txt").write_text("from claude\n")
        if not self._before_start:
            self._switch()
        output = (
            "First phase complete; continue in the retained worktree.\n"
            "COMMIT: feat: complete first phase\nDISPOSITION: continue\nQUESTION: NONE"
        )
        return RuntimeResult(
            output=output,
            resolved=request.resolved,
            effective_model=request.resolved.model,
            provider_session_id="claude-session",
        )


class CodexCompletionAdapter(EditingAdapter):
    family = "codex"

    def __init__(self) -> None:
        super().__init__()
        self.saw_retained_work = False

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        request.on_session_started("codex-session")
        self.saw_retained_work = (request.cwd / "phase-one.txt").read_text() == "from claude\n"
        (request.cwd / "phase-two.txt").write_text("from codex\n")
        output = (
            "Finished from the existing task state.\n"
            "COMMIT: feat: complete second phase\nDISPOSITION: idle\nQUESTION: NONE"
        )
        return RuntimeResult(
            output=output,
            resolved=request.resolved,
            effective_model=request.resolved.model,
            provider_session_id="codex-session",
        )


class RecoveringSessionAdapter(EditingAdapter):
    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        if request.provider_session_id == "stale-session":
            raise MissingProviderSession("saved task session is gone")
        request.on_session_started("replacement-session")
        (request.cwd / "recovered.txt").write_text("fresh session\n")
        output = (
            "Recovered with the full task and worktree.\n"
            "COMMIT: feat: recover task session\nDISPOSITION: idle\nQUESTION: NONE"
        )
        return RuntimeResult(
            output=output,
            resolved=request.resolved,
            effective_model=request.resolved.model,
            provider_session_id="replacement-session",
        )


def _counting_gate(tmp_path: Path) -> tuple[CommandSpec, Path]:
    counter = tmp_path / "gate-count"
    code = (
        "from pathlib import Path; "
        f"p=Path({str(counter)!r}); "
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')"
    )
    return CommandSpec(argv=(sys.executable, "-c", code)), counter


@pytest.mark.parametrize("local_commits", [False, True])
def test_task_runs_in_one_retained_worktree_and_lands_exact_tested_sha(
    tmp_path: Path, local_commits: bool,
) -> None:
    bare, clone = _repository(tmp_path)
    original = _git("rev-parse", "HEAD", cwd=clone)
    state = StateDatabase(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("app", "Add result", "Create result.txt containing completed."),
        provider="claude",
        profile="balanced",

    )
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    repository = RepositoryConfig(
        path=str(clone),
        remote_url=str(bare),
        default_branch="main",
    )
    transport = ControllerGitTransport(
        tmp_path / "controller.db",
        "app",
        str(bare),
        "main",
        allow_local=True,
    )
    class RetainingAdapter(EditingAdapter):
        def execute(self, request):
            common = Path(_git("rev-parse", "--git-common-dir", cwd=request.cwd))
            (common / "info/exclude").write_text("ignored-cache\n")
            (request.cwd / "ignored-cache").write_text("survive accepted task")
            return super().execute(request)
    adapter = RetainingAdapter(local_commits=local_commits)
    runner = TaskRunner(
        state=state,
        repositories={"app": repository},
        transports={"app": transport},
        worktrees_root=tmp_path / "worktrees",
        broker=broker,
        cognition=Cognition({"claude": adapter}),
        provider_fallbacks=(),
        timeout_seconds=30,
    )

    assert run_task(runner) == admitted.task_id

    assert task_status(runner, admitted.task_id) is TaskStatus.DONE
    retained = tmp_path / "worktrees" / str(admitted.task_id)
    assert (retained / "ignored-cache").read_text() == "survive accepted task"
    runner.reconcile_worktrees()
    assert (retained / "ignored-cache").read_text() == "survive accepted task"
    assert not _git("status", "--porcelain", cwd=retained)
    assert (
        _git(f"--git-dir={bare}", "show", "main:result.txt", cwd=tmp_path)
        == "completed"
    )
    landed = state.tasks.get(admitted.task_id)
    assert _git("rev-parse", "main", cwd=bare) == landed.landed
    assert _git("rev-parse", admitted.branch, cwd=clone) == landed.work_sha
    assert landed.landed != landed.work_sha
    assert _git("rev-list", "--count", f"{original}..main", cwd=bare) == "1"
    assert "feat: add task result" in _git("log", "-1", "--format=%B", admitted.branch, cwd=clone)
    subjects = _git(
        f"--git-dir={bare}", "log", "--format=%s", f"{original}..main", cwd=tmp_path
    ).splitlines()
    assert subjects == [f"steward: accept {admitted.branch}"]




def test_remote_input_change_truthfully_requeues_instead_of_blocking(
    tmp_path: Path,
) -> None:
    bare, clone = _repository(tmp_path)
    operator = tmp_path / "operator"
    _git("clone", "-q", str(bare), str(operator), cwd=tmp_path)
    _git("config", "user.name", "Operator", cwd=operator)
    _git("config", "user.email", "operator@example.invalid", cwd=operator)

    def move_remote() -> None:
        (operator / "operator.txt").write_text("new main input\n")
        _git("add", "operator.txt", cwd=operator)
        _git("commit", "-q", "-m", "operator input", cwd=operator)
        _git("push", "-q", "origin", "main", cwd=operator)

    state = StateDatabase(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("app", "Add result", "Create result.txt containing completed."),
        provider="claude",
        profile="balanced",

    )
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    gate_log = tmp_path / "gated-shas.txt"
    repository = RepositoryConfig(
        path=str(clone), remote_url=str(bare),
        gates=(CommandSpec(argv=(
            sys.executable, "-c",
            "from pathlib import Path\n"
            "import subprocess, sys\n"
            "assert Path('result.txt').read_text() == 'completed\\n'\n"
            "with Path(sys.argv[1]).open('a') as output:\n"
            "    output.write(subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True))\n",
            str(gate_log),
        )),),
    )
    transport = MoveOnceTransport(
        tmp_path / "controller.db",
        "app",
        str(bare),
        "main",
        allow_local=True,
        move_remote=move_remote,
    )
    adapter = EditingAdapter()
    runner = TaskRunner(
        state=state,
        repositories={"app": repository},
        transports={"app": transport},
        worktrees_root=tmp_path / "worktrees",
        broker=broker,
        cognition=Cognition({"claude": adapter}),
        provider_fallbacks=(),
        timeout_seconds=30,
    )

    assert run_task(runner) == admitted.task_id

    assert task_status(runner, admitted.task_id) is TaskStatus.RUNNING
    assert (tmp_path / "worktrees" / str(admitted.task_id)).is_dir()
    assert _git("show", f"{admitted.branch}:result.txt", cwd=clone) == "completed"
    # The work was never rejected: the turn closed idle, and it is the push
    # that lost the race against the moved base. Nothing recorded that.
    assert latest_slice(runner, admitted.task_id).disposition is CheckpointDisposition.IDLE
    assert len(gate_log.read_text().splitlines()) == 1
    with pytest.raises(subprocess.CalledProcessError):
        _git(f"--git-dir={bare}", "show", "main:result.txt", cwd=tmp_path)

    provider_turns = len(adapter.requests)
    assert run_task(runner) == admitted.task_id

    assert len(adapter.requests) == provider_turns
    assert task_status(runner, admitted.task_id) is TaskStatus.DONE
    assert (
        _git(f"--git-dir={bare}", "show", "main:operator.txt", cwd=tmp_path)
        == "new main input"
    )
    assert (
        _git(f"--git-dir={bare}", "show", "main:result.txt", cwd=tmp_path)
        == "completed"
    )
    assert latest_slice(runner, admitted.task_id).disposition is CheckpointDisposition.IDLE
    # Two gate runs at two different identities: the first against the base the
    # slice ended on, the second against the moved one. The second is what the
    # remote took.
    gated = gate_log.read_text().splitlines()
    assert len(gated) == 2 and gated[0] != gated[1]
    assert gated[1] == _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path)


def test_unexpected_exception_escapes_with_owned_task_recoverable(
    tmp_path: Path,
) -> None:
    bare, clone = _repository(tmp_path)
    state = ExplodingCheckpointState(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("app", "Fail unexpectedly", "Exercise recovery ownership."),
        provider="claude",
        profile="balanced",

    )
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    repository = RepositoryConfig(path=str(clone), remote_url=str(bare))
    adapter = EditingAdapter()
    runner = TaskRunner(
        state=state,
        repositories={"app": repository},
        transports={
            "app": ControllerGitTransport(
                tmp_path / "controller.db",
                "app",
                str(bare),
                "main",
                allow_local=True,
            )
        },
        worktrees_root=tmp_path / "worktrees",
        broker=broker,
        cognition=Cognition({"claude": adapter}),
        provider_fallbacks=(),
        timeout_seconds=30,
    )

    with pytest.raises(ValueError, match="unexpected state defect"):
        run_task(runner)

    assert task_status(runner, admitted.task_id) is TaskStatus.RUNNING
    # The turn closed and submitted its candidate in one write before the
    # defect escaped, so it is a pending publication, and that is what makes it
    # recoverable — it holds no lock and is owed no further slice.
    assert state.tasks.queued() == ()
    assert any(
        task.publishable for task in runner.state.tasks.all()
    )

    provider_turns = len(adapter.requests)
    assert (
        publish_task(runner)
        == admitted.task_id
    )

    assert len(adapter.requests) == provider_turns
    assert task_status(runner, admitted.task_id) is TaskStatus.DONE


def test_pre_checkpoint_recovery_preserves_partial_work_attempt_and_session(
    tmp_path: Path,
) -> None:
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("app", "Recover partial work", "Complete the retained task."),
        provider="claude",
        profile="balanced",

    )
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    repository = RepositoryConfig(path=str(clone), remote_url=str(bare))
    transport = ControllerGitTransport(
        tmp_path / "controller.db",
        "app",
        str(bare),
        "main",
        allow_local=True,
    )
    interrupted = InterruptingAdapter()
    first = TaskRunner(
        state=state,
        repositories={"app": repository},
        transports={"app": transport},
        worktrees_root=tmp_path / "worktrees",
        broker=broker,
        cognition=Cognition({"claude": interrupted}),
        provider_fallbacks=(),
        timeout_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt, match="before checkpoint"):
        run_task(first)

    # Nothing closed: an interrupted slice leaves no commit, so the branch has
    # no history and the task never left the queue.
    assert latest_slice(first, admitted.task_id) is None
    assert state.tasks.queued() == (admitted.task_id,)
    assert state.get_conversation(state.tasks.get(admitted.task_id).session_id).provider_session_id == "claude-interrupted"
    worktree = tmp_path / "worktrees" / str(admitted.task_id)
    assert (worktree / "partial.txt").read_text() == "preserve me\n"

    resumed = EditingAdapter()
    second = TaskRunner(
        state=StateDatabase(state.path),
        repositories={"app": repository},
        transports={"app": transport},
        worktrees_root=tmp_path / "worktrees",
        broker=broker,
        cognition=Cognition({"claude": resumed}),
        provider_fallbacks=(),
        timeout_seconds=30,
    )

    # Recovery is ordinary dispatch now: the first worker's lock died with it,
    # and there is no turn left to resume — the task simply never left the
    # queue, and its retained worktree and session are where it got to.
    assert second.prepare(admitted.task_id) == admitted.task_id
    assert publish_task(second) == admitted.task_id

    assert len(resumed.requests) == 1, "publication must not repeat recovered cognition"
    task_request = resumed.requests[0]
    assert task_request.cwd == worktree
    # The execution names the task, not an attempt at it: continuity across
    # dispatches is the native session, which is the same one.
    assert task_request.execution_id == str(admitted.session_id)
    assert task_request.provider_session_id == "claude-interrupted"
    for request in (interrupted.requests[0], task_request):
        assert "preserve completed work" in request.prompt
        assert "finish only what remains of the accepted task" in request.prompt
    assert task_status(second, admitted.task_id) is TaskStatus.DONE
    assert (
        _git(f"--git-dir={bare}", "show", "main:partial.txt", cwd=tmp_path)
        == "preserve me"
    )
    assert (
        _git(f"--git-dir={bare}", "show", "main:result.txt", cwd=tmp_path)
        == "completed"
    )


def test_mismatched_recovery_worktree_blocks_only_that_task_without_rewriting_it(
    tmp_path: Path,
) -> None:
    bare, clone = _repository(tmp_path)
    state = ExplodingCheckpointState(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("app", "Retain mismatch", "Create result.txt."),
        provider="claude",
        profile="balanced",

    )
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    repository = RepositoryConfig(path=str(clone), remote_url=str(bare))
    adapter = EditingAdapter()
    runner = TaskRunner(
        state=state,
        repositories={"app": repository},
        transports={
            "app": ControllerGitTransport(
                tmp_path / "controller.db",
                "app",
                str(bare),
                "main",
                allow_local=True,
            )
        },
        worktrees_root=tmp_path / "worktrees",
        broker=broker,
        cognition=Cognition({"claude": adapter}),
        provider_fallbacks=(),
        timeout_seconds=30,
    )
    with pytest.raises(ValueError, match="unexpected state defect"):
        run_task(runner)

    _manager, worktree, _checkpointer = runner._open_worktree(
        state.tasks.get(admitted.task_id), repository
    )
    (worktree / "untrusted.txt").write_text("must survive\n")
    _git("add", "untrusted.txt", cwd=worktree)
    _git("commit", "-q", "-m", "unexpected retained commit", cwd=worktree)
    unexpected_head = _git("rev-parse", "HEAD", cwd=worktree)

    # No checkpoint comparison is needed, and there is none. A commit the
    # harness did not make carries no `Disposition` trailer, so the tip does
    # not say `idle`, so the branch does not say there is anything to publish.
    # The unexpected work is left exactly where it is.
    assert publish_task(runner) is None

    assert _git("rev-parse", "HEAD", cwd=worktree) == unexpected_head
    assert (worktree / "untrusted.txt").read_text() == "must survive\n"
    with pytest.raises(subprocess.CalledProcessError):
        _git(f"--git-dir={bare}", "show", "main:result.txt", cwd=tmp_path)


def test_queued_retained_worktree_is_checkpointed_before_claim(tmp_path: Path) -> None:
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("app", "Keep retained work", "Create result.txt."),
        provider="claude",
        profile="balanced",

    )
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    repository = RepositoryConfig(path=str(clone), remote_url=str(bare))
    transport = ControllerGitTransport(
        tmp_path / "controller.db", "app", str(bare), "main", allow_local=True
    )
    runner = TaskRunner(
        state=state,
        repositories={"app": repository},
        transports={"app": transport},
        worktrees_root=tmp_path / "worktrees",
        broker=broker,
        cognition=Cognition({"claude": EditingAdapter()}),
        provider_fallbacks=(),
        timeout_seconds=30,
    )
    _manager, worktree, _checkpointer = runner._open_worktree(
        state.tasks.get(admitted.task_id), repository
    )
    (worktree / "retained.txt").write_text("canonical queued work\n")
    runner.reconcile_worktrees()
    assert (worktree / "retained.txt").read_text() == "canonical queued work\n"

    assert run_task(runner) == admitted.task_id
    assert (
        _git(f"--git-dir={bare}", "show", "main:retained.txt", cwd=tmp_path)
        == "canonical queued work"
    )


def test_task_worktree_reconciliation_retains_inactive_environment(
    tmp_path: Path,
) -> None:
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("app", "Remove stale checkout", "Return without changes."),
        provider="claude",
        profile="balanced",

    )
    root = tmp_path / "worktrees"
    stale = root / str(admitted.task_id)
    stale.mkdir(parents=True)
    (stale / "provider-cache").write_text("obsolete\n")
    state.tasks.cancel(admitted.task_id, "obsolete")
    runner = TaskRunner(
        state=state,
        repositories={"app": RepositoryConfig(path=str(clone), remote_url=str(bare))},
        transports={},
        worktrees_root=root,
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
        cognition=Cognition({"claude": EditingAdapter()}),
        provider_fallbacks=(),
        timeout_seconds=30,
    )

    runner.reconcile_worktrees()

    assert (stale / "provider-cache").read_text() == "obsolete\n"


def test_answer_is_prompt_context_and_is_consumed_by_the_next_checkpoint(
    tmp_path: Path,
) -> None:
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("app", "Choose an option", "Implement the selected option."),
        provider="claude",
        profile="balanced",

    )
    adapter = AskingOnceAdapter()
    runner = TaskRunner(
        state=state,
        repositories={
            "app": RepositoryConfig(path=str(clone), remote_url=str(bare))
        },
        transports={
            "app": ControllerGitTransport(
                tmp_path / "controller.db",
                "app",
                str(bare),
                "main",
                allow_local=True,
            )
        },
        worktrees_root=tmp_path / "worktrees",
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
        cognition=Cognition({"claude": adapter}),
        provider_fallbacks=(),
        timeout_seconds=30,
    )

    assert run_task(runner) == admitted.task_id
    assert task_status(runner, admitted.task_id) is TaskStatus.WAITING

    state.tasks.answer(admitted.task_id, "Use option B")
    assert [text for _kind, text in tuple((k, t) for _, k, t, _ in state.tasks.get(admitted.task_id).pending)] == [
        "Use option B"
    ]

    assert run_task(runner) == admitted.task_id

    task_requests = adapter.requests
    assert "answer: Use option B" in task_requests[-1].prompt
    assert tuple((k, t) for _, k, t, _ in state.tasks.get(admitted.task_id).pending) == ()
    assert task_status(runner, admitted.task_id) is TaskStatus.DONE


@pytest.mark.parametrize("disposition", ["accepted", "rejected", "unresolved"])
def test_running_task_receives_notes_and_only_consumes_acknowledged_input(
    tmp_path: Path, disposition: str,
) -> None:
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    task = admit_task(
        state, TaskSpec("app", "Live steering", "Use operator context."),
        provider="claude", profile="balanced",
    )
    state.tasks.note(task.task_id, "initial context")
    delivered = []

    class StreamingAdapter(EditingAdapter):
        capabilities = replace(SESSION_WORKSPACE_CAPABILITIES, ongoing_input=True)

        def execute(self, request):
            assert "initial context" in request.prompt
            assert request.on_input_ready is not None
            def send(message):
                delivered.append(message)
                request.on_input_result(RuntimeInputResult(message.source_id, disposition))
            request.on_input_ready(send)
            state.tasks.note(task.task_id, "arrived during execution")
            runner.flush_inputs()
            runner.flush_inputs()
            # This arrives too late to be offered; closing must retain it.
            state.tasks.note(task.task_id, "arrived at completion")
            return super().execute(request)

    runner = TaskRunner(
        state=state,
        repositories={"app": RepositoryConfig(path=str(clone), remote_url=str(bare))},
        transports={"app": ControllerGitTransport(
            tmp_path / "controller.db", "app", str(bare), "main", allow_local=True,
        )},
        worktrees_root=tmp_path / "worktrees",
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
        cognition=Cognition({"claude": StreamingAdapter()}),
        provider_fallbacks=(), timeout_seconds=30,
    )
    runner.prepare(task.task_id)
    assert [message.text for message in delivered] == ["note: arrived during execution"]
    expected = [("note", "arrived at completion")]
    if disposition != "accepted":
        expected.insert(0, ("note", "arrived during execution"))
    assert tuple((k, t) for _, k, t, _ in StateDatabase(state.path).tasks.get(task.task_id).pending) == tuple(expected)
    assert task.task_id in state.tasks.queued()
    runner.flush_inputs()
    assert len(delivered) == 1


def test_running_cancellation_preserves_partial_work_without_publication(
    tmp_path: Path,
) -> None:
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("app", "Cancelable work", "Write useful partial work."),
        provider="claude",
        profile="balanced",

    )
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    repository = RepositoryConfig(path=str(clone), remote_url=str(bare))
    transport = ControllerGitTransport(
        tmp_path / "controller.db",
        "app",
        str(bare),
        "main",
        allow_local=True,
    )
    runner = TaskRunner(
        state=state,
        repositories={"app": repository},
        transports={"app": transport},
        worktrees_root=tmp_path / "worktrees",
        broker=broker,
        cognition=Cognition(
            {
                "claude": CancellingAdapter(
                    lambda: state.tasks.cancel(
                        admitted.task_id
                    )
                )
            }
        ),
        provider_fallbacks=(),
        timeout_seconds=30,
    )

    assert run_task(runner) == admitted.task_id

    retained = tmp_path / "worktrees" / str(admitted.task_id)
    assert task_status(runner, admitted.task_id) is TaskStatus.CANCELLED
    assert retained.is_dir()
    assert _git("show", f"{admitted.branch}:partial.txt", cwd=clone) == "preserve this"
    remote_partial = subprocess.run(
        ["git", f"--git-dir={bare}", "cat-file", "-e", "main:partial.txt"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert remote_partial.returncode != 0

    state.tasks.retry(admitted.task_id, "Continue from the preserved work")
    resumed = EditingAdapter()
    retry_runner = TaskRunner(
        state=state,
        repositories={"app": repository},
        transports={"app": transport},
        worktrees_root=tmp_path / "worktrees",
        broker=broker,
        cognition=Cognition({"claude": resumed}),
        provider_fallbacks=(),
        timeout_seconds=30,
    )
    assert run_task(retry_runner) == admitted.task_id
    assert "retry: Continue from the preserved work" in resumed.requests[0].prompt
    assert _git(f"--git-dir={bare}", "show", "main:partial.txt", cwd=tmp_path) == (
        "preserve this"
    )


def test_cancellation_after_testing_stops_the_exact_pre_push_boundary(
    tmp_path: Path,
) -> None:
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("app", "Cancel tested work", "Create result.txt."),
        provider="claude",
        profile="balanced",

    )
    runner = TaskRunner(
        state=state,
        repositories={
            "app": RepositoryConfig(path=str(clone), remote_url=str(bare))
        },
        transports={
            "app": ControllerGitTransport(
                tmp_path / "controller.db",
                "app",
                str(bare),
                "main",
                allow_local=True,
            )
        },
        worktrees_root=tmp_path / "worktrees",
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
        cognition=Cognition({"claude": EditingAdapter()}),
        provider_fallbacks=(),
        timeout_seconds=30,
    )

    # The slice finishes and commits; the withdrawal lands before anything is
    # published. The push is the write, so that is where the marker is read --
    # and the writer that honours it is the one that records the withdrawal.
    runner.prepare(admitted.task_id)
    state.tasks.cancel(admitted.task_id)
    assert publish_task(runner) is None

    assert task_status(runner, admitted.task_id) is TaskStatus.CANCELLED
    assert (tmp_path / "worktrees" / str(admitted.task_id)).is_dir()
    assert _git("show", f"{admitted.branch}:result.txt", cwd=clone) == "completed"
    remote_result = subprocess.run(
        ["git", f"--git-dir={bare}", "cat-file", "-e", "main:result.txt"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert remote_result.returncode != 0
    # Nothing was published, and nothing wrote that down: the remote simply
    # does not have it.


@pytest.mark.parametrize("initial_provider", ["claude", "glm"])
@pytest.mark.parametrize("before_start", [False, True])
def test_mid_task_provider_switch_keeps_worktree_and_rejects_old_session_write(
    tmp_path: Path, initial_provider: str, before_start: bool,
) -> None:
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("app", "Two provider task", "Complete both phases."),
        provider=initial_provider,
        profile="balanced",

    )
    task = state.tasks.get(admitted.task_id)
    claude = SwitchingContinueAdapter(
        lambda: state.bind_conversation_provider(task.session_id, "codex", None),
        before_start=before_start,
    )
    codex = CodexCompletionAdapter()
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    repository = RepositoryConfig(path=str(clone), remote_url=str(bare))
    runner = TaskRunner(
        state=state,
        repositories={"app": repository},
        transports={
            "app": ControllerGitTransport(
                tmp_path / "controller.db",
                "app",
                str(bare),
                "main",
                allow_local=True,
            )
        },
        worktrees_root=tmp_path / "worktrees",
        broker=broker,
        cognition=Cognition({"claude": claude, "codex": codex}),
        provider_fallbacks=("claude", "codex"),
        timeout_seconds=30,
    )

    assert run_task(runner) == admitted.task_id

    selected = state.get_conversation(state.tasks.get(task.task_id).session_id)
    assert task_status(runner, admitted.task_id) is TaskStatus.QUEUED
    assert selected.provider == "codex"
    assert selected.provider_session_id is None
    # One rotation per event that leaves nothing resumable: the switch itself,
    # and a fallback where one happened.
    assert selected.generation == 2 + (initial_provider != "claude" and not before_start)

    assert run_task(runner) == admitted.task_id

    codex_task_request = next(
        request
        for request in codex.requests

    )
    assert codex_task_request.provider_session_id is None
    assert "Two provider task" in codex_task_request.prompt
    for request in (claude.requests[0], codex_task_request):
        assert "inspect this retained branch" in request.prompt.lower()
        assert "finish only what remains of the accepted task" in request.prompt
    assert codex.saw_retained_work
    assert task_status(runner, admitted.task_id) is TaskStatus.DONE
    assert _git(f"--git-dir={bare}", "show", "main:phase-one.txt", cwd=tmp_path) == (
        "from claude"
    )
    assert _git(f"--git-dir={bare}", "show", "main:phase-two.txt", cwd=tmp_path) == (
        "from codex"
    )


def test_missing_task_session_rotates_once_and_binds_the_replacement(
    tmp_path: Path,
) -> None:
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("app", "Recover session", "Create recovered.txt."),
        provider="claude",
        profile="balanced",

    )
    task = state.tasks.get(admitted.task_id)
    state.bind_conversation_provider(state.tasks.get(task.task_id).session_id, "claude", "stale-session")
    adapter = RecoveringSessionAdapter()
    runner = TaskRunner(
        state=state,
        repositories={
            "app": RepositoryConfig(path=str(clone), remote_url=str(bare))
        },
        transports={
            "app": ControllerGitTransport(
                tmp_path / "controller.db",
                "app",
                str(bare),
                "main",
                allow_local=True,
            )
        },
        worktrees_root=tmp_path / "worktrees",
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
        cognition=Cognition({"claude": adapter}),
        provider_fallbacks=(),
        timeout_seconds=30,
    )

    assert run_task(runner) == admitted.task_id

    lineage = state.get_conversation(state.tasks.get(task.task_id).session_id)
    task_requests = adapter.requests
    assert [request.provider_session_id for request in task_requests] == [
        "stale-session",
        None,
    ]
    assert task_requests[0].prompt == task_requests[1].prompt
    assert lineage.generation == 2
    assert lineage.provider_session_id == "replacement-session"
    assert task_status(runner, admitted.task_id) is TaskStatus.DONE


def test_cancellation_after_possible_push_preserves_remote_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    task = admit_task(
        state,
        TaskSpec("app", "Publish once", "Create result.txt."),
        provider="claude",
        profile="balanced",

    )

    class LostObservation(ControllerGitTransport):
        pushes = 0
        fail_observation = False

        def push_candidate(self, tested_sha, expected_base_sha):
            self.pushes += 1
            accepted = super().push_candidate(tested_sha, expected_base_sha)
            state.tasks.cancel(task.task_id)
            self.fail_observation = True
            return accepted

    transport = LostObservation(tmp_path / "controller.db", "app", str(bare), "main", allow_local=True)
    run = subprocess.run

    def lose_observation(argv, *args, **kwargs):
        if (
            transport.fail_observation and "fetch" in argv
            and f"--git-dir={transport.git_dir}" in argv
        ):
            transport.fail_observation = False
            raise OSError("lost observation after push")
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", lose_observation)
    adapter = EditingAdapter()
    runner = TaskRunner(
        state=state, repositories={"app": RepositoryConfig(path=str(clone), remote_url=str(bare))},
        transports={"app": transport}, worktrees_root=tmp_path / "worktrees",
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
        cognition=Cognition({"claude": adapter}),
        provider_fallbacks=(), timeout_seconds=15,
    )
    with pytest.raises(GitTransportError, match="controller Git fetch"):
        run_task(runner)
    assert task_status(runner, task.task_id) is TaskStatus.CANCELLED
    # The next pass observes the remote and finds the work already landed.
    assert publish_task(runner) is None
    assert task_status(runner, task.task_id) is TaskStatus.DONE
    assert transport.pushes == len(adapter.requests) == 1
    assert _git(f"--git-dir={bare}", "show", "main:result.txt", cwd=tmp_path) == "completed"



def test_retry_cannot_republish_its_old_idle_checkpoint(tmp_path):
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / 'state.db')
    admitted = admit_task(state, TaskSpec('app', 'Correct red candidate', 'Correct the gate'),
        provider='claude', profile='balanced')
    original = _git('rev-parse', 'main', cwd=bare)
    class CorrectingAdapter(EditingAdapter):
        corrected = False
        def execute(self, request):
            if self.corrected:
                # This was allowed to gate the OLD idle tip and block this
                # correction before it had even finished writing.
                assert publish_task(runner) is None
                assert state.tasks.get(admitted.task_id).status is not TaskStatus.BLOCKED
            result = super().execute(request)
            (request.cwd / 'result.txt').write_text('green' if self.corrected else 'red')
            return result
    adapter = CorrectingAdapter()
    repository = RepositoryConfig(path=str(clone), remote_url=str(bare), gates=(
        CommandSpec(argv=(sys.executable, '-c',
            "from pathlib import Path; assert Path('result.txt').read_text() == 'green'")),))
    runner = TaskRunner(state=state, repositories={'app': repository},
        transports={'app': ControllerGitTransport(tmp_path/'controller.db','app',str(bare),'main',allow_local=True)},
        worktrees_root=tmp_path/'worktrees', broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
        cognition=Cognition({'claude':adapter}), provider_fallbacks=(), timeout_seconds=30)
    run_task(runner)
    assert task_status(runner, admitted.task_id) is TaskStatus.QUEUED
    assert any("Gate failed" in text for _, text in tuple((k, t) for _, k, t, _ in state.tasks.get(admitted.task_id).pending))
    assert publish_task(runner) is None  # queued but no worker has acquired it yet
    assert _git('rev-parse','main',cwd=bare) == original
    adapter.corrected = True
    run_task(runner)
    assert task_status(runner, admitted.task_id) is TaskStatus.DONE
    assert _git('show','main:result.txt',cwd=bare) == 'green'
