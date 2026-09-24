"""Task intent stops the working native process and preserves useful edits."""

import os
import signal
import sys
import threading
import time

from steward_harness.cognition import Cognition
from steward_harness.config.schema import StewardConfig
from steward_harness.conversations import ConversationService
from steward_harness.daemon import KernelCommands
from steward_harness.runtime.contracts import RuntimeExecutionError
from steward_harness.runtime.providers.claude import ClaudeRuntime
from steward_harness.state import StateDatabase
from test_runtime_lifecycle import _controller
from test_task_no_changes import setup_task
from test_task_runner_kernel import EditingAdapter, _git, run_task


def _quiet_native(tmp_path):
    ready = tmp_path / "work-started"
    stopped = tmp_path / "work-stopped"
    executable = tmp_path / "quiet-provider"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys\n"
        "def stop(*_):\n"
        f"    pathlib.Path({str(stopped)!r}).touch()\n"
        "    sys.exit(0)\n"
        "command = json.loads(sys.stdin.readline())\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "interrupt = json.loads(sys.stdin.readline())\n"
        "assert interrupt['request'] == {'subtype': 'interrupt'}\n"
        "print(json.dumps({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': interrupt['request_id']}}), flush=True)\n"
        "stop()\n"
    )
    executable.chmod(0o755)
    controller = _controller()
    native_home = tmp_path / "native"
    native_home.mkdir()
    native = ClaudeRuntime(executable, controller=controller, native_home=native_home)
    return native, ready, stopped


def _commands(tmp_path, state, cognition):
    config = StewardConfig.model_validate({
        "identity": {"name": "Steward", "slug": "test"},
        "provider": {
            "default_family": "claude", "fallback_families": [],
            "workdir": str(tmp_path), "state_db": str(tmp_path / "state.db"),
        },
    })
    service = ConversationService(
        state, cognition, provider_order=("claude",), profile="balanced",
        workspace=tmp_path, timeout_seconds=15,

    )
    return KernelCommands(
        config, state, service, None, {}, _controller().broker,
    ), service


def test_cancel_command_stops_a_quiet_native_conversation(tmp_path):
    native, ready, stopped = _quiet_native(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    commands, service = _commands(tmp_path, state, Cognition({"claude": native}))
    errors = []

    def run():
        try:
            service.run_turn(
                transport="telegram", transport_key="7", source_event_key="quiet-turn",
                operator_id="operator", text="Inspect the current workspace.",
            )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), errors
        conversation = state.find_conversation("telegram", "7")
        assert state.active_turn(conversation.conversation_id) is not None
        assert "Cancellation requested" in commands("cancel", None, 1, 7, 1)
        thread.join(5)
        assert not thread.is_alive(), "native conversation ignored persisted cancellation"
    finally:
        if thread.is_alive() and ready.exists():
            os.killpg(int(ready.read_text()), signal.SIGKILL)
        thread.join(5)

    assert len(errors) == 1 and isinstance(errors[0], RuntimeExecutionError)
    assert "cancelled" in str(errors[0])
    assert stopped.exists(), "native child did not process the interrupt request"
    assert state.active_turn(conversation.conversation_id) is None
    assert state.pending_turns() == []


def test_cancel_quiet_native_work_preserves_work_without_publication(tmp_path):
    native, ready, stopped = _quiet_native(tmp_path)

    class Worker(EditingAdapter):
        def execute(self, request):
            super().execute(request)
            return native.execute(request)

    state, runner, task_id, bare = setup_task(tmp_path, Worker())
    commands, _ = _commands(tmp_path, state, runner.cognition)
    original = _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path)
    errors = []

    def run():
        try:
            run_task(runner)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), errors
        assert "Cancellation recorded" in commands("task", f"cancel {task_id}", 1, 7, 1)
        thread.join(5)
        assert not thread.is_alive(), "native work ignored durable cancellation"
    finally:
        if thread.is_alive() and ready.exists():
            os.killpg(int(ready.read_text()), signal.SIGKILL)
        thread.join(5)

    assert errors == []
    assert stopped.exists(), "native child did not process the interrupt request"
    task = state.tasks.get(task_id)
    assert task.status.value == "cancelled"
    repository = runner.repositories["app"].path
    assert _git("show", f"{task.branch}:result.txt", cwd=repository) == "completed"
    assert _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path) == original
