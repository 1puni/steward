"""Live stdin remains bounded and cancellable without owning provider semantics."""

import os
import sys
import threading
import time

import pytest

from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.runtime.contracts import RuntimeExecutionError
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.process import ProcessController


def controller():
    return ProcessController(UntrustedExecutionBroker(UntrustedExecutionConfig()))


def test_input_can_follow_output_then_deliver_eof(tmp_path):
    inputs = []
    observed = []

    def output(line):
        observed.append(line)
        if line == "READY":
            inputs[0].write("first\n")
        elif line == "first":
            inputs[0].write("second\n")
            inputs[0].close()

    result = controller().run(
        [
            sys.executable,
            "-c",
            (
                "import sys; print('READY',flush=True); "
                "[(print(line.strip(),flush=True)) for line in sys.stdin]; print('EOF',flush=True)"
            ),
        ],
        cwd=tmp_path,
        env=os.environ,
        timeout_seconds=5,
        on_input_ready=inputs.append,
        on_stdout_line=output,
    )
    assert result.returncode == 0
    assert observed == ["READY", "first", "second", "EOF"]
    with pytest.raises(RuntimeExecutionError, match="input is closed"):
        inputs[0].write("stale input")


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
def test_unread_stdin_does_not_block_stop(tmp_path, stop):
    runtime = controller()
    ready = threading.Event()
    finished = threading.Event()
    cancelled = threading.Event()
    inputs = []
    errors = []
    stoppers = []

    def input_ready(writer):
        inputs.append(writer)
        writer.write("x" * 800_000)  # More than a pipe can hold.
        ready.set()

    def run():
        try:
            runtime.run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=tmp_path,
                env=os.environ,
                timeout_seconds=1 if stop == "timeout" else 30,
                on_input_ready=input_ready,
                on_started=stoppers.append,
            )
        except RuntimeExecutionError as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert ready.wait(5)
        if stop == "cancel":
            stoppers[0]()
        assert finished.wait(5), "stdin blocked process cancellation/deadline"
    finally:
        thread.join(5)
    assert errors and ("timed out" if stop == "timeout" else "cancelled") in str(
        errors[0]
    )
    with pytest.raises(RuntimeExecutionError, match="input is closed"):
        inputs[0].write("late")


def test_initial_unread_prompt_obeys_timeout(tmp_path):
    started = time.monotonic()
    with pytest.raises(RuntimeExecutionError, match="timed out"):
        controller().run(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            env=os.environ,
            timeout_seconds=1,
            stdin_text="x" * 800_000,
        )
    assert time.monotonic() - started < 5


def test_input_callback_failure_closes_its_writer_and_releases_execution(tmp_path):
    runtime = controller()
    inputs = []

    def reject(writer):
        inputs.append(writer)
        raise LookupError("caller invariant")

    with pytest.raises(LookupError, match="caller invariant"):
        runtime.run(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            env=os.environ,
            timeout_seconds=5,
            on_input_ready=reject,
        )
    with pytest.raises(RuntimeExecutionError, match="input is closed"):
        inputs[0].write("late")
    result = runtime.run(
        [sys.executable, "-c", "print('released')"],
        cwd=tmp_path,
        env=os.environ,
        timeout_seconds=5,
    )
    assert result.returncode == 0


def test_pending_input_limit_fails_before_child_launch(tmp_path):
    marker = tmp_path / "should-not-exist"
    with pytest.raises(RuntimeExecutionError, match="pending input exceeded"):
        controller().run(
            [sys.executable, "-c", f"open({str(marker)!r},'w').close()"],
            cwd=tmp_path,
            env=os.environ,
            timeout_seconds=5,
            stdin_text="x" * 1_048_577,
        )
    assert not marker.exists()


def test_cancellation_during_output_stops_the_child(tmp_path):
    runtime = controller()
    stoppers = []

    with pytest.raises(RuntimeExecutionError, match="cancelled"):
        runtime.run(
            [sys.executable, "-c", "import time; print('READY',flush=True); time.sleep(60)"],
            cwd=tmp_path, env={}, timeout_seconds=5,
            on_stdout_line=lambda _line: stoppers[0](),
            on_started=stoppers.append,
        )
    assert runtime.run(
        [sys.executable, "-c", "print('released')"],
        cwd=tmp_path, env={}, timeout_seconds=5,
    ).returncode == 0


def test_unresponsive_child_is_killed_within_a_bounded_grace(tmp_path):
    runtime = controller()
    started = time.monotonic()
    with pytest.raises(RuntimeExecutionError, match="timed out"):
        runtime.run(
            [sys.executable, "-c", ("import signal,time; "
             "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")],
            cwd=tmp_path, env=os.environ, timeout_seconds=1,
        )
    # SIGTERM ignored, so this is SIGKILL arriving after the grace.
    assert time.monotonic() - started < 6


@pytest.mark.parametrize("reason", ["cancel", "timeout"])
def test_native_stop_drains_final_output_before_containment(tmp_path, reason):
    writers, stoppers, observed = [], [], []
    def consume(line):
        observed.append(line)
        if line == "READY" and reason == "cancel":
            stoppers[0]()
            stoppers[0]()  # Duplicate requests cannot restart the grace.
    with pytest.raises(RuntimeExecutionError, match="cancelled" if reason == "cancel" else "timed out"):
        controller().run(
            [sys.executable, "-c", "import sys; print('READY',flush=True); assert input() == 'interrupt'; print('CHECKPOINT',flush=True)"],
            cwd=tmp_path, env=os.environ, timeout_seconds=1 if reason == "timeout" else 30,
            on_input_ready=writers.append, on_started=stoppers.append,
            on_stop=lambda: writers[0].write("interrupt\n"), on_stdout_line=consume,
        )
    assert observed == ["READY", "CHECKPOINT"]
    stoppers[0]()  # A stale route has no process side effects.


def test_ignored_native_stop_escalates_and_preserves_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr("steward_harness.runtime.process._COOPERATIVE_GRACE_SECONDS", 0.1)
    monkeypatch.setattr("steward_harness.runtime.process._TERMINATION_GRACE_SECONDS", 0.1)
    stoppers, stops = [], []
    started = time.monotonic()
    with pytest.raises(RuntimeExecutionError, match="cancelled"):
        controller().run(
            [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('READY',flush=True); time.sleep(60)"],
            cwd=tmp_path, env=os.environ, timeout_seconds=30,
            on_started=stoppers.append, on_stdout_line=lambda _: stoppers[0](),
            on_stop=lambda: stops.append(True),
        )
    assert time.monotonic() - started < 5
    assert stops == [True]


def test_success_cleans_descendants_with_grace_after_leader_exits(tmp_path):
    marker = tmp_path / "checkpoint"
    child = (
        "import signal,time,pathlib; "
        f"signal.signal(signal.SIGTERM, lambda *_: (time.sleep(.15), pathlib.Path({str(marker)!r}).write_text('saved'), exit(0))); "
        "print('READY',flush=True); time.sleep(60)"
    )
    # Child signals its readiness through a private pipe before leader exits;
    # stdout/stderr do not keep the parent alive, exposing success-path leaks.
    parent = (
        "import subprocess,sys; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.PIPE); "
        "assert p.stdout.readline() == b'READY\\n'"
    )
    result = controller().run([sys.executable, "-c", parent], cwd=tmp_path, env=os.environ, timeout_seconds=10)
    assert result.returncode == 0
    assert marker.read_text() == "saved"
