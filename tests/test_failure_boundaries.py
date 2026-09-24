"""Operational failures recover; unexpected defects retain ownership and escape."""

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from steward_harness.config.schema import CommandSpec
from steward_harness.landing.gates import GateRunner
from steward_harness.runtime.contracts import RuntimeExecutionError
from test_runtime_lifecycle import _controller
from test_task_no_changes import InvestigationAdapter, setup_task


@pytest.mark.parametrize("error_type", [RuntimeExecutionError, ValueError])
def test_task_defect_retains_owner_instead_of_becoming_blocked(tmp_path, error_type):
    class FailingAdapter(InvestigationAdapter):
        def execute(self, request):
            request.on_session_started("known-session")
            (request.cwd / "partial.txt").write_text("Retain this evidence.\n")
            raise error_type("injected failure")

    state, runner, task_id, _ = setup_task(tmp_path, FailingAdapter())
    if error_type is RuntimeExecutionError:
        runner.prepare(task_id)
        assert state.tasks.get(task_id).status.value == "blocked"
    else:
        with pytest.raises(ValueError, match="injected failure"):
            runner.prepare(task_id)
        # It never left the queue, so the next tick dispatches it again.
        # Nothing had to remember it: the slice left no record at all — no
        # commit, no row — and the lock the defect blew through is simply gone.
        assert state.tasks.get(task_id).status.value == "queued"
        assert state.tasks.queued() == (task_id,)
    assert (
        (tmp_path / "worktrees" / str(task_id) / "partial.txt")
        .read_text()
        .startswith("Retain")
    )
    assert (
        state.get_conversation(state.tasks.get(state.tasks.get(task_id).task_id).session_id).provider_session_id
        == "known-session"
    )


@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_gate_does_not_disguise_a_defect_as_a_red_command(tmp_path, error_type):
    def fail(*args, **kwargs):
        raise error_type("injected failure")

    broker = SimpleNamespace(popen_command=fail, candidate_directory=lambda root, relative: root / relative)
    command = CommandSpec(argv=(sys.executable, "-c", "pass"))
    if error_type is OSError:
        result = GateRunner.run_command(command, tmp_path, broker=broker)
        assert not result.passed
        assert "injected failure" in result.stderr
    else:
        with pytest.raises(ValueError, match="injected failure"):
            GateRunner.run_command(command, tmp_path, broker=broker)




def test_stdout_callback_defect_escapes_after_process_cleanup(tmp_path):
    controller = _controller()

    def fail(line):
        raise ValueError("broken stream callback")

    with pytest.raises(ValueError, match="broken stream callback"):
        controller.run(
            (
                sys.executable,
                "-c",
                "import time; print('event', flush=True); time.sleep(30)",
            ),
            cwd=tmp_path,
            env={},
            timeout_seconds=5,
            on_stdout_line=fail,
        )
    again = controller.run(
        (sys.executable, "-c", "print('recovered')"),
        cwd=tmp_path,
        env={},
        timeout_seconds=5,
    )
    assert again.stdout.strip() == "recovered"
