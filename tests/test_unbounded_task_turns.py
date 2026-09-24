"""Ordinary task turns have no routine deadline; only intent ends them early.

A task turn outlives the provider deadline and closes naturally. Operator
cancellation and controller shutdown still end it promptly, through the native
interrupt and then containment, and neither is mistaken for the other: a
cancelled task is cancelled, an interrupted one is retained as a continuation.
Procedure runs are finite reviews and keep the deadline.
"""

import os
import signal
import sys
import threading
import time
from textwrap import dedent

import pytest

from steward_harness.runtime.process import ProcessController
from steward_harness.runtime.providers.claude import ClaudeRuntime
from test_quiet_rhythms import commit, quiet_harness
from test_task_cancellation import _commands
from test_task_no_changes import InvestigationAdapter, result_text, setup_task, task_status
from test_task_runner_kernel import EditingAdapter, _git, run_task

SESSION = "5f0c7a52-8a4e-4b8e-9d51-3e1f2c7b9a10"
DEADLINE = 1

_HEARTBEAT = (
    "import os, sys, time\n"
    "while True:\n"
    "    with open(sys.argv[1], 'a') as beat:\n"
    "        beat.write(f'{os.getpid()} {time.time()}\\n')\n"
    "    time.sleep(0.1)\n"
)


def _native(tmp_path, *, finish_after=None):
    """A stream-json Claude that spawns a heartbeat child, then either closes
    after ``finish_after`` seconds or waits for the native interrupt."""
    pids, heartbeat = tmp_path / "native.pids", tmp_path / "heartbeat"
    executable = tmp_path / "long-provider"
    executable.write_text(f"#!{sys.executable}\n" + dedent(f'''
        import json, os, pathlib, subprocess, sys, time
        def emit(**event):
            print(json.dumps({{'session_id': {SESSION!r}, **event}}), flush=True)
        command = json.loads(sys.stdin.readline())
        emit(type='system', subtype='init', model=sys.argv[sys.argv.index('--model') + 1])
        emit(type='command_lifecycle', state='started', command_uuid=command['uuid'])
        child = subprocess.Popen([sys.executable, '-c', {_HEARTBEAT!r}, {str(heartbeat)!r}])
        pathlib.Path({str(pids)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')
        if {finish_after!r} is not None:
            time.sleep({finish_after!r})
            pathlib.Path('result.txt').write_text('completed past the former deadline\\n')
            emit(type='result', user_message_uuid=command['uuid'], subtype='success',
                 terminal_reason='completed', usage={{}}, result=(
                     'Worked past the provider deadline.\\n'
                     'COMMIT: feat: long native work\\nDISPOSITION: idle\\nQUESTION: NONE'))
        else:
            interrupt = json.loads(sys.stdin.readline())
            assert interrupt['request'] == {{'subtype': 'interrupt'}}
            print(json.dumps({{'type': 'control_response', 'response': {{
                'subtype': 'success', 'request_id': interrupt['request_id']}}}}), flush=True)
            emit(type='result', user_message_uuid=command['uuid'],
                 subtype='error_during_execution', is_error=True,
                 terminal_reason='interrupted', errors=['interrupted'])
        emit(type='command_lifecycle', state='completed', command_uuid=command['uuid'])
        assert sys.stdin.read() == ''
    '''))
    executable.chmod(0o755)
    native_home = tmp_path / "native"
    native_home.mkdir()

    class Native(EditingAdapter):
        def execute(self, request):
            self.requests.append(request)
            return runtime.execute(request)

    adapter = Native()
    state, runner, task_id, bare = setup_task(tmp_path, adapter)
    runtime = ClaudeRuntime(
        executable, controller=ProcessController(runner.broker), native_home=native_home,
    )
    runner.timeout_seconds = DEADLINE
    return state, runner, task_id, bare, adapter, pids, heartbeat


def _wait(condition, seconds, message):
    until = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < until, message
        time.sleep(0.02)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _beats(heartbeat):
    return [float(line.split()[1]) for line in heartbeat.read_text().splitlines()]


class _Running:
    """One task turn on its own thread, as a dispatch worker runs it."""

    def __init__(self, runner, pids, heartbeat):
        self.pids, self.heartbeat, self.errors = pids, heartbeat, []
        self.thread = threading.Thread(target=self._run, args=(runner,))
        self.started = time.time()
        self.thread.start()

    def _run(self, runner):
        try:
            run_task(runner)
        except BaseException as error:
            self.errors.append(error)

    def outlive_deadline(self):
        _wait(self.pids.exists, 10, f"native turn never started: {self.errors}")
        _wait(lambda: self.heartbeat.exists() and any(
            beat > self.started + DEADLINE + 0.5 for beat in _beats(self.heartbeat)
        ), 10, "native child did not outlive the former deadline")
        assert self.thread.is_alive(), self.errors
        return [int(pid) for pid in self.pids.read_text().split()]

    def stop(self):
        if self.thread.is_alive() and self.pids.exists():
            for pid in map(int, self.pids.read_text().split()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.thread.join(15)


def test_task_turn_outlives_provider_deadline_and_closes_naturally(tmp_path):
    state, runner, task_id, bare, adapter, pids, heartbeat = _native(
        tmp_path, finish_after=DEADLINE + 3
    )
    running = _Running(runner, pids, heartbeat)
    try:
        parent, child = running.outlive_deadline()
        running.thread.join(20)
        assert not running.thread.is_alive()
    finally:
        running.stop()
    assert running.errors == []
    assert adapter.requests[0].timeout_seconds is None
    assert state.tasks.get(task_id).status.value != "blocked"
    assert task_status(runner, task_id) == "done"
    assert _git(f"--git-dir={bare}", "show", "main:result.txt", cwd=tmp_path) == (
        "completed past the former deadline"
    )
    # Natural completion still releases the whole group, descendants included.
    _wait(lambda: not _alive(parent) and not _alive(child), 5, "native group survived")


def test_cancel_still_stops_an_unbounded_task_turn_promptly(tmp_path):
    state, runner, task_id, bare, _, pids, heartbeat = _native(tmp_path)
    commands, _ = _commands(tmp_path, state, runner.cognition)
    original = _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path)
    running = _Running(runner, pids, heartbeat)
    try:
        parent, child = running.outlive_deadline()
        asked = time.monotonic()
        assert "Cancellation recorded" in commands("task", f"cancel {task_id}", 1, 7, 1)
        running.thread.join(5)
        assert not running.thread.is_alive(), "cancellation did not reach the native turn"
        # The native interrupt was honoured; the grace was not waited out.
        assert time.monotonic() - asked < 5
    finally:
        running.stop()
    assert running.errors == []
    assert state.tasks.get(task_id).status.value == "cancelled"
    assert not _alive(parent) and not _alive(child)
    assert _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path) == original


def test_shutdown_interrupt_retains_an_unbounded_task_turn_as_continuation(tmp_path):
    state, runner, task_id, bare, _, pids, heartbeat = _native(tmp_path)
    original = _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path)
    running = _Running(runner, pids, heartbeat)
    try:
        parent, child = running.outlive_deadline()
        asked = time.monotonic()
        runner.interrupt_running()
        running.thread.join(15)
        assert not running.thread.is_alive(), "shutdown did not end the native turn"
        assert time.monotonic() - asked < 5
    finally:
        running.stop()
    assert running.errors == []
    task = state.tasks.get(task_id)
    assert task.status.value != "blocked"
    assert state.get_conversation(task.session_id).provider_session_id == SESSION
    assert state.tasks.queued() == (task_id,)
    assert result_text(state, task_id, runner) is None
    assert not _alive(parent) and not _alive(child)
    assert _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path) == original


def test_procedure_runs_keep_the_provider_deadline(tmp_path):
    (tmp_path / "procedure").mkdir()
    (tmp_path / "plain").mkdir()
    clone, state, runner, _, procedures, adapter = quiet_harness(tmp_path / "procedure")
    procedures.advance_rhythms(now=0)
    commit(clone, "changed.txt")
    procedures.advance_rhythms(now=10)
    procedures.advance_rhythms(now=310)
    runner.prepare(state.tasks.queued()[0])
    assert adapter.requests[0].timeout_seconds == runner.timeout_seconds == 30

    plain = InvestigationAdapter()
    _, plain_runner, _, _ = setup_task(tmp_path / "plain", plain)
    run_task(plain_runner)
    assert plain.requests[0].timeout_seconds is None


@pytest.mark.parametrize("timeout", [0, -1])
def test_a_finite_deadline_must_still_be_positive(tmp_path, timeout):
    from steward_harness.runtime.contracts import RuntimeRequest, resolve_model

    with pytest.raises(ValueError):
        RuntimeRequest(
            execution_id="turn", resolved=resolve_model("claude", "balanced"),
            provider_session_id=None, prompt="work", cwd=tmp_path, timeout_seconds=timeout,
        )
