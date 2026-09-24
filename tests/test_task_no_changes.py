"""Executions that change no working-tree file still keep their disposition,
still publish real work — their findings, appended to the task's own file —
and never claim a landing the harness did not perform."""

from dataclasses import replace
from steward_harness.deploy.config import SystemdReleaseConfig

import os
import signal
import sys
import threading
import time
from textwrap import dedent

import pytest

from state_fixtures import accept_conversation_turn, advance
from steward_harness.cognition import Cognition
from steward_harness.config.schema import (
    RepositoryConfig,
    UntrustedExecutionConfig,
)
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.landing.checkpoint import WorktreeCheckpointer
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime import process as process_module
from steward_harness.runtime.process import ProcessController
from steward_harness.runtime.providers.claude import ClaudeRuntime
from steward_harness.state import StateDatabase, TaskId, TaskSpec
from steward_harness.task_runner import TaskRunner
from steward_harness.task_lock import locked_tasks
from test_task_runner_kernel import (
    EditingAdapter,
    ExplodingCheckpointState,
    _git,
    _repository,
    publish_task,
    run_task,
)


class InvestigationAdapter(EditingAdapter):
    def __init__(self, disposition="idle", *, edit_first=False):
        super().__init__()
        self.disposition = disposition
        self.edit_first = edit_first
        self.work_turns = 0

    def execute(self, request):
        result = (
            super().execute(request)
            if self.edit_first and self.work_turns == 0
            else None
        )
        if result is None:
            # Reuse only the provider result shape; this worker intentionally edits nothing.
            from steward_harness.runtime.contracts import RuntimeResult

            self.requests.append(request)
            result = RuntimeResult(
                output="",
                resolved=request.resolved,
                effective_model=request.resolved.model,
                provider_session_id="claude-session",
            )
        self.work_turns += 1
        question = "Which consumer is authoritative?" if self.disposition == "ask" else "NONE"
        output = (
            "The public consumer still reads the old feed. Evidence: src/feed.py.\n"
            f"COMMIT: steward: inspect consumer\nDISPOSITION: {self.disposition}\nQUESTION: {question}"
        )
        return replace(result, output=output)


def result_text(state, task_id, runner=None):
    """The text the admitting desk would receive for this task's terminal state."""
    for conversation_id in state.pending_task_result_conversations():
        pending = state.pending_task_result_for(conversation_id)
        if pending is not None and pending[0] == task_id:
            return pending[1]
    return None


def setup_task(tmp_path, adapter, state_type=StateDatabase):
    bare, clone = _repository(tmp_path)
    state = state_type(tmp_path / "state.db")
    conversation = state.get_or_create_conversation(
        "telegram",
        "7",
        provider="claude",
        profile="balanced",
    )
    turn, _ = state.start_turn(
        conversation.conversation_id, "request", "operator", "Inspect consumer"
    )
    receipt = accept_conversation_turn(
        state,
        turn.turn_id,
        reply_text="Investigating.",
        provider="claude",
        model="model",
        provider_session_id="claude-session",
        spec=TaskSpec("app", "Inspect consumer", "Find the consumer, ask if needed."),
        # Admission is the branch, so the fixture opens a real one.

    )
    admitted = state.tasks.get(TaskId(receipt["task_id"]))
    repository = RepositoryConfig(
        path=str(clone),
        remote_url=str(bare),

    )
    runner = TaskRunner(
        state=state,
        repositories={"app": repository},
        transports={
            "app": ControllerGitTransport(
                tmp_path / "controller.db", "app", str(bare), "main", allow_local=True
            )
        },
        worktrees_root=tmp_path / "worktrees",
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
        cognition=Cognition({"claude": adapter}),
        provider_fallbacks=(),
        timeout_seconds=30,
    )
    return state, runner, admitted.task_id, bare


def task_status(runner, task_id):
    """Status as the harness reports it: derived from the task's branch."""
    return runner.state.tasks.get(task_id).status.value


@pytest.mark.parametrize(
    "disposition,status", [("idle", "done"), ("ask", "waiting"), ("continue", "queued")]
)
def test_no_diff_execution_keeps_disposition_and_findings(
    tmp_path, disposition, status
):
    adapter = InvestigationAdapter(disposition)
    state, runner, task_id, bare = setup_task(tmp_path, adapter)
    original = _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path)
    run_task(runner)
    assert task_status(runner, task_id) == status
    assert len(adapter.requests) == 1
    task = state.tasks.get(task_id)
    repository = runner.repositories["app"].path
    # No code changed: the branch tip is the slice's own commit, carrying its
    # findings and disposition on top of the base it was cut from.
    slice_message = _git("log", "-1", "--format=%B", task.branch, cwd=repository)
    assert slice_message.startswith("steward: inspect consumer")
    assert f"Disposition: {disposition}" in slice_message
    assert "src/feed.py" in slice_message
    if disposition == "idle":
        landed = _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path)
        # Published: main moved off the base it was cut from, to exactly the
        # tested branch tip — no fake landing — and changes no file.
        assert landed != original
        assert landed == state.tasks.get(task_id).landed
        assert _git(
            f"--git-dir={bare}", "diff", "--name-status", original, "main", cwd=tmp_path
        ) == ""
    else:
        # ask/continue never reach publication: nothing to land.
        assert _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path) == original
    pending = state.pending_task_result_conversations()
    if status == "waiting":
        assert len(pending) == 1
        delivery = state.pending_task_result_for(pending[0])
        assert delivery is not None
        assert "src/feed.py" in delivery[1]
        assert "DISPOSITION:" not in delivery[1]
        assert "Landed SHA" not in delivery[1]
    elif status == "done":
        # The work landed, so the task is terminal and owes its owner a
        # receipt. It used to be held open as `running` until a follow-on
        # release finished, which delayed a result the operator was already owed.
        assert len(pending) == 1
        delivery = state.pending_task_result_for(pending[0])
        assert delivery is not None and "Landed SHA" in delivery[1]
    else:
        # continue is queued for another slice: no terminal result yet.
        assert pending == ()
    if status == "waiting":
        state.tasks.answer(
                task_id,
                "The public consumer is authoritative.",
            )
        adapter.disposition = "idle"
        run_task(runner)
        # Same landing as the idle case above: the work is on the default
        # branch, so the task is done.
        assert task_status(runner, task_id) == "done"
        assert "public consumer is authoritative" in adapter.requests[1].prompt
        assert tuple((k, t) for _, k, t, _ in state.tasks.get(task_id).pending) == ()


def test_no_diff_final_turn_still_lands_prior_turn_changes(tmp_path):
    adapter = InvestigationAdapter("continue", edit_first=True)
    state, runner, task_id, bare = setup_task(tmp_path, adapter)
    run_task(runner)
    adapter.disposition = "idle"
    run_task(runner)
    assert (
        _git(f"--git-dir={bare}", "show", "main:result.txt", cwd=tmp_path)
        == "completed"
    )


def test_restart_after_unchanged_checkpoint_does_not_repeat_investigation(tmp_path):
    adapter = InvestigationAdapter()
    state, runner, task_id, _ = setup_task(
        tmp_path, adapter, ExplodingCheckpointState
    )
    with pytest.raises(ValueError, match="unexpected state defect"):
        run_task(runner)
    reopened = StateDatabase(tmp_path / "state.db")
    runner.state = reopened
    runner.state.tasks.transports = runner.transports
    # Recovery is dispatch: the open turn is resumed by the same call that
    # would have started one.
    runner.prepare(task_id)
    publish_task(runner)
    task = reopened.tasks.get(task_id)
    # The task is done: its work is on the default branch, and the promotion
    # status that used to hold it open until a release finished does not exist
    # to conflate them any more.
    assert task_status(runner, task_id) == "done"
    assert len(adapter.requests) == 1
    # Investigation was not repeated: recovery did not re-run cognition, it
    # only recovered the already-durable promotion; the findings from the
    # single execution turn are what its commit carries.
    assert "src/feed.py" in _git(
        "log", "-1", "--format=%B", task.branch,
        cwd=runner.repositories["app"].path,
    )


@pytest.mark.parametrize("closure", [
    "Task completed.",
    "COMMIT: feat: work\nDISPOSITION: deploy\nQUESTION: NONE",
    "COMMIT: feat: work\nDISPOSITION: ask\nQUESTION: NONE",
    "COMMIT: feat: work\nDISPOSITION: idle\nQUESTION: NONE\nActually, more work remains.",
])
def test_invalid_working_session_closure_retains_retryable_work_without_publication(tmp_path, closure):
    class InvalidOnceAdapter(EditingAdapter):
        def execute(self, request):
            result = super().execute(request)
            return replace(result, output=closure) if len(self.requests) == 1 else result

    adapter = InvalidOnceAdapter()
    state, runner, task_id, bare = setup_task(tmp_path, adapter)
    original = _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path)
    run_task(runner)
    task = state.tasks.get(task_id)
    assert task.status.value == "blocked"
    assert "Invalid task closure" in result_text(state, task_id)
    assert len(adapter.requests) == 1
    assert _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path) == original
    assert _git("show", f"{task.branch}:result.txt", cwd=runner.repositories["app"].path) == "completed"

    state.tasks.retry(task_id, "Finish the task with its explicit closure.")
    run_task(runner)
    assert len(adapter.requests) == 2
    assert adapter.requests[1].provider_session_id == "claude-session"
    assert _git(f"--git-dir={bare}", "show", "main:result.txt", cwd=tmp_path) == "completed"


def restart(runner):
    """The runner a replacement controller builds over the same durable state."""
    return TaskRunner(
        state=StateDatabase(runner.state.path), repositories=runner.repositories,
        transports=runner.transports, worktrees_root=runner.worktrees_root,
        broker=runner.broker, cognition=runner.cognition,
        provider_fallbacks=runner.provider_fallbacks,
        timeout_seconds=runner.timeout_seconds,
    )


@pytest.mark.parametrize('local_commits', [False, True])
def test_shutdown_contains_unresponsive_writer_and_autosaves_without_landing(tmp_path, monkeypatch, local_commits):
    # Exercise the shipped Claude adapter and process containment with a real
    # writer that ignores the native interrupt. Task turns have no deadline;
    # shutdown asks them to end, and the grace then falls back to SIGTERM.
    # This fixture does not emulate Codex App Server's cleanup protocol.
    pid_file = tmp_path / 'writer.pid'
    stopped = tmp_path / 'writer-stopped'
    native_sha = tmp_path / 'native-sha'
    executable = tmp_path / 'writing-provider'
    executable.write_text(f'#!{sys.executable}\n' + dedent(f'''
        import os, pathlib, signal, subprocess, sys, time
        root = pathlib.Path.cwd()
        def stop(*_):
            (root / 'stop-write.txt').write_text('last write before exit\\n')
            pathlib.Path({str(stopped)!r}).touch()
            sys.exit(0)
        signal.signal(signal.SIGTERM, stop)
        pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))
        (root / 'partial.txt').write_text('native progress\\n')
        if {local_commits!r}:
            subprocess.run(['git', 'add', 'partial.txt'], check=True)
            subprocess.run(['git', '-c', 'user.name=Native', '-c',
                            'user.email=native@localhost', '-c', 'commit.gpgsign=false',
                            'commit', '-qm', 'native: cohesive progress'], check=True)
            sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True)
            pathlib.Path({str(native_sha)!r}).write_text(sha.strip())
        (root / 'remaining.txt').write_text('unfinished work\\n')
        for tick in range(100_000):
            with (root / 'active.txt').open('a') as heartbeat:
                heartbeat.write(str(tick) + '\\n')
            time.sleep(0.02)
    '''))
    executable.chmod(0o755)
    native_home = tmp_path / 'native'
    native_home.mkdir()

    class InterruptedOnce(InvestigationAdapter):
        def execute(self, request):
            if not self.requests:
                self.requests.append(request)
                return native.execute(request)
            assert (request.cwd / 'remaining.txt').read_text() == 'unfinished work\n'
            return super().execute(request)

    adapter = InterruptedOnce()
    state, runner, task_id, bare = setup_task(tmp_path, adapter)
    native = ClaudeRuntime(
        executable, controller=ProcessController(runner.broker), native_home=native_home,
    )
    runner.timeout_seconds = 1  # Would have expired long before shutdown.
    original = _git(f'--git-dir={bare}', 'rev-parse', 'main', cwd=tmp_path)
    stage = WorktreeCheckpointer.stage
    monkeypatch.setattr(process_module, '_COOPERATIVE_GRACE_SECONDS', 0.5)
    active = tmp_path / 'worktrees'

    def stage_after_writer_exit(checkpointer, **kwargs):
        assert stopped.exists(), 'autosave ran before native termination'
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        return stage(checkpointer, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(WorktreeCheckpointer, 'stage', stage_after_writer_exit)
            worker = threading.Thread(target=run_task, args=(runner,))
            worker.start()
            until = time.monotonic() + 10
            while not any(active.rglob('active.txt')) or len(
                next(active.rglob('active.txt')).read_text().splitlines()
            ) < 150:  # Beyond the one-second provider deadline.
                assert time.monotonic() < until and worker.is_alive(), 'writer never ran'
                time.sleep(0.02)
            assert worker.is_alive()
            runner.interrupt_running()
            worker.join(10)
            assert not worker.is_alive(), 'shutdown did not contain the writer'
    finally:
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                os.waitpid(int(pid_file.read_text()), 0)
            except (ProcessLookupError, ChildProcessError):
                pass
    # Interrupted, not blocked: the task is a continuation, and no result is due.
    assert state.tasks.queued() == (task_id,)
    assert result_text(state, task_id) is None
    path = adapter.requests[0].cwd
    saved = _git('rev-parse', 'HEAD', cwd=path)
    assert saved != original
    assert _git('status', '--porcelain', cwd=path) == ''
    assert _git('log', '-1', '--format=%s', cwd=path) == 'steward: autosave interrupted task work'
    assert _git('show', 'HEAD:active.txt', cwd=path).splitlines()[:2] == ['0', '1']
    assert _git('show', 'HEAD:stop-write.txt', cwd=path) == 'last write before exit'
    if local_commits:
        assert _git('rev-parse', 'HEAD^', cwd=path) == native_sha.read_text()
    assert _git(f'--git-dir={bare}', 'rev-parse', 'main', cwd=tmp_path) == original
    # A replacement controller continues it. Only the new successful closure can land.
    runner = restart(runner)
    run_task(runner)
    assert len(adapter.requests) == 2
    task = runner.state.tasks.get(task_id)
    repository_path = runner.repositories['app'].path
    landed = _git(f'--git-dir={bare}', 'rev-parse', 'main', cwd=tmp_path)
    # The retried tick commits unconditionally, so the landed SHA is one commit past the autosaved
    # work, not the autosave commit itself — while still retaining it.
    assert landed != saved
    assert runner.state.tasks.contains(saved, runner.state.tasks.read(task_id)[0])
    assert landed == runner.state.tasks.get(task_id).landed
    assert _git(f'--git-dir={bare}', 'show', 'main:remaining.txt', cwd=tmp_path) == 'unfinished work'
    assert 'src/feed.py' in _git(
        'log', '-1', '--format=%B', task.branch, cwd=repository_path
    )


def test_kernel_continues_interrupted_native_session_after_restart_before_gating_and_landing(tmp_path):
    import hashlib
    import json
    import threading
    import time

    from steward_harness.repository_reconciler import RepositoryReconciler
    from steward_harness.config.schema import CommandSpec
    from steward_harness.kernel import Dispatch, StewardKernel

    session = "c69a4e91-3933-4f93-aa2a-2ce7637ce6da"
    pid_file, stopped, native_sha = (
        tmp_path / "writer.pid", tmp_path / "writer-stopped", tmp_path / "native-sha"
    )
    executable = tmp_path / "unfinished-provider"
    executable.write_text(f"#!{sys.executable}\n" + dedent(f'''
        import json, os, pathlib, signal, subprocess, sys, time
        root = pathlib.Path.cwd()
        def stop(*_):
            (root / 'stop-write.txt').write_text('last native write\\n')
            pathlib.Path({str(stopped)!r}).touch()
            print(json.dumps({{'type': 'result', 'session_id': {session!r},
                              'user_message_uuid': command['uuid'],
                              'subtype': 'error_during_execution', 'is_error': True,
                              'terminal_reason': 'interrupted', 'errors': ['interrupted']}}), flush=True)
            print(json.dumps({{'type': 'command_lifecycle', 'state': 'completed',
                              'session_id': {session!r}, 'command_uuid': command['uuid']}}), flush=True)
            assert sys.stdin.read() == ''  # Harness drains completion then sends EOF.
            sys.exit(0)
        pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))
        command = json.loads(sys.stdin.readline())
        model = sys.argv[sys.argv.index('--model') + 1]
        print(json.dumps({{'type': 'system', 'subtype': 'init',
                          'session_id': {session!r}, 'model': model}}), flush=True)
        print(json.dumps({{'type': 'command_lifecycle', 'state': 'started',
                          'session_id': {session!r}, 'command_uuid': command['uuid']}}), flush=True)
        (root / 'partial.txt').write_text('native progress\\n')
        subprocess.run(['git', 'add', 'partial.txt'], check=True)
        subprocess.run(['git', '-c', 'user.name=Native', '-c',
                        'user.email=native@localhost', '-c', 'commit.gpgsign=false',
                        'commit', '-qm', 'native: cohesive progress'], check=True)
        pathlib.Path({str(native_sha)!r}).write_text(
            subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip())
        (root / 'remaining.txt').write_text('unfinished work\\n')
        interrupt = json.loads(sys.stdin.readline())
        assert interrupt['request'] == {{'subtype': 'interrupt'}}
        print(json.dumps({{'type': 'control_response', 'response': {{
            'subtype': 'success', 'request_id': interrupt['request_id']}}}}), flush=True)
        stop()
    '''))
    executable.chmod(0o755)
    native_home = tmp_path / "native"
    native_home.mkdir()

    class FinishNextSlice(InvestigationAdapter):
        def execute(self, request):
            if not self.requests:
                self.requests.append(request)
                return native.execute(request)
            assert request.execution_id == self.requests[0].execution_id
            assert request.provider_session_id == session
            assert request.cwd == self.requests[0].cwd
            assert (request.cwd / "remaining.txt").read_text() == "unfinished work\n"
            assert stopped.exists()
            with pytest.raises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
            (request.cwd / "finished.txt").write_text("finished in the same session\n")
            return replace(super().execute(request), provider_session_id=session)

    adapter = FinishNextSlice()
    state, runner, task_id, bare = setup_task(tmp_path, adapter)
    native = ClaudeRuntime(
        executable, controller=ProcessController(runner.broker), native_home=native_home,
    )
    runner.timeout_seconds = 1  # Task turns ignore it; only shutdown ends this one.
    gate = CommandSpec(argv=(sys.executable, "-c", (
        "from pathlib import Path; "
        "assert Path('partial.txt').read_text() == 'native progress\\n'; "
        "assert Path('stop-write.txt').read_text() == 'last native write\\n'; "
        "assert Path('finished.txt').read_text() == 'finished in the same session\\n'; "
        "print('retained native work verified')"
    )))
    runner.repositories["app"] = runner.repositories["app"].model_copy(
        update={"deploy": None, "gates": (gate,)}
    )
    ambient = RepositoryReconciler(
        state=state, repositories=runner.repositories, transports=runner.transports,
        worktrees_root=runner.worktrees_root, broker=runner.broker,
    )
    kernel = StewardKernel(
        state, ambient, runner,
        dispatch=Dispatch(1),
    )
    original = _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path)
    try:
        advance(kernel)
        deadline = time.monotonic() + 10
        while not adapter.requests or not (adapter.requests[0].cwd / "remaining.txt").exists():
            assert time.monotonic() < deadline, "native writer never started"
            threading.Event().wait(0.01)
        threading.Event().wait(1.5)  # Past the former provider deadline.
        assert locked_tasks(runner.state.tasks.locks_root)
        # Controller shutdown, in the daemon's order.
        runner.interrupt_running()
        kernel.stop()
        # Wait for the first writer without dispatching its continuation yet.
        # Its lock *is* its `running` state, so releasing the lock is the
        # durable form of the dispatch set this used to read.
        while not adapter.requests or locked_tasks(runner.state.tasks.locks_root):
            assert time.monotonic() < deadline, "interrupted writer did not drain"
            threading.Event().wait(0.01)
        assert len(adapter.requests) == 1
        retained = state.tasks.get(task_id)
        lineage = state.get_conversation(retained.session_id)
        assert lineage.provider_session_id == session
        # Still mid-execution. No slice *closed*: the autosave commit is the
        # branch refusing to leave an older slice's trailer on its tip, not a
        # disposition anything decided. The lock went with the worker, so the
        # task is an ordinary queued task again.
        history = state.tasks.checkpoints(retained, 5)
        assert [item.disposition.value for item in history] == ["blocked"]
        assert state.tasks.queued() == (task_id,)
        assert result_text(state, task_id, runner) is None
        assert stopped.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        path = adapter.requests[0].cwd
        saved = _git("rev-parse", "HEAD", cwd=path)
        assert _git("rev-parse", "HEAD^", cwd=path) == native_sha.read_text()
        assert _git("log", "-1", "--format=%s", cwd=path) == "steward: autosave interrupted task work"
        assert _git("show", "HEAD:remaining.txt", cwd=path) == "unfinished work"
        assert _git("show", "HEAD:stop-write.txt", cwd=path) == "last native write"
        assert _git("status", "--porcelain", cwd=path) == ""
        assert _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path) == original

        # The replacement controller dispatches the retained continuation.
        runner = restart(runner)
        kernel = StewardKernel(
            runner.state, RepositoryReconciler(
                state=runner.state, repositories=runner.repositories,
                transports=runner.transports, worktrees_root=runner.worktrees_root,
                broker=runner.broker,
            ), runner, dispatch=Dispatch(1),
        )
        deadline = time.monotonic() + 10
        while task_status(runner, task_id) != "done":
            advance(kernel)
            assert time.monotonic() < deadline, "retained native task did not finish"
            threading.Event().wait(0.01)
        assert len(adapter.requests) == 2
        # Same native session continued: no rotation, so no new generation.
        assert state.get_conversation(state.tasks.get(task_id).session_id).generation == lineage.generation
        # Two slices, both on the one native session: the autosave that let the
        # session keep running, then the one that closed it idle. This asked
        # for *one execution turn* covering both, and the reason it could is
        # gone with the row — a turn spanning two dispatches was the only way
        # to say "the same session continued", and the lineage says it now.
        assert [
            item.disposition.value
            for item in state.tasks.checkpoints(state.tasks.get(task_id), 5)
        ] == ["idle", "blocked"]
        # What landed is what the branch points at: publication retips it at
        # the commit the remote took, so one ref read answers both.
        landed = _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path)
        assert state.tasks.get(task_id).tip == landed
        assert state.tasks.contains(saved, state.tasks.read(task_id)[0])
        assert _git(f"--git-dir={bare}", "show", "main:finished.txt", cwd=tmp_path) == "finished in the same session"
    finally:
        runner.interrupt_running()
        kernel.stop()
