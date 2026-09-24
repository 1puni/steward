"""Bounded, cancellable process execution for native providers and commands."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event, RLock
from typing import Any, cast

from steward_harness.runtime.contracts import RuntimeExecutionError
from steward_harness.runtime.execution import UntrustedExecutionBroker

_STREAM_LIMIT_BYTES = 64 * 1024 * 1024
_DIAGNOSTIC_LIMIT_BYTES = 1024 * 1024
_TERMINATION_GRACE_SECONDS = 2.0
_COOPERATIVE_GRACE_SECONDS = 10.0
_INPUT_LIMIT_BYTES = 1024 * 1024


class ProcessTimeout(RuntimeExecutionError):
    """The process exceeded its declared execution deadline."""


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    returncode: int
    stdout: str
    stderr: str


class ProcessInput:
    """Bounded local input buffer; queued bytes are not provider acceptance."""

    def __init__(self) -> None:
        self._pending = bytearray()
        self._closed = False
        self._finished = False
        self._lock = RLock()

    def write(self, text: str) -> None:
        data = text.encode("utf-8")
        with self._lock:
            if self._closed or self._finished:
                raise RuntimeExecutionError("provider input is closed")
            if len(self._pending) + len(data) > _INPUT_LIMIT_BYTES:
                raise RuntimeExecutionError(
                    "provider pending input exceeded safe limit"
                )
            self._pending.extend(data)

    def close(self) -> None:
        """Request EOF after the already-queued bytes have been written."""
        with self._lock:
            self._closed = True

    def _flush(self, stream: Any) -> None:
        with self._lock:
            if self._finished or stream.closed:
                return
            if self._pending:
                try:
                    written = os.write(stream.fileno(), self._pending[:65_536])
                except BlockingIOError:
                    return
                except OSError as error:
                    raise RuntimeExecutionError(
                        "provider input pipe could not be written"
                    ) from error
                del self._pending[:written]
            if self._closed and not self._pending:
                stream.close()

    def _finish(self) -> None:
        with self._lock:
            self._finished = True
            self._pending.clear()


class ProcessController:
    """Controls brokered process groups; children that escape need separate ownership."""

    def __init__(self, broker: UntrustedExecutionBroker) -> None:
        self._broker = broker

    @property
    def broker(self) -> UntrustedExecutionBroker:
        """Use the same enforced identity for adapter-owned artifact capture."""
        return self._broker

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int | None,
        on_stdout_line: Callable[[str], None] | None = None,
        stdin_text: str | None = None,
        on_input_ready: Callable[[ProcessInput], None] | None = None,
        on_started: Callable[[Callable[[], None]], None] | None = None,
        on_stop: Callable[[], None] | None = None,
        command_only: bool = False,
    ) -> ProcessOutput:
        """Run a bounded child; command-only calls use the broker's credentialless policy.

        Provider calls supply ``env``. Command-only calls inherit only the host
        command allowlist and never receive that provider environment.

        ``on_started`` receives an ephemeral cancellation route. Cancellation
        and deadlines ask ``on_stop`` for native interruption first, while pipes
        continue draining. A bounded grace then falls back to broker containment.
        Callers must remove their cancellation route when this call returns.
        ``timeout_seconds=None`` means no routine deadline; cancellation still
        takes the same cooperative-then-containment path.
        """
        if timeout_seconds is not None and timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")
        input_buffer = (
            ProcessInput() if stdin_text is not None or on_input_ready else None
        )
        if input_buffer is not None:
            if stdin_text is not None:
                input_buffer.write(stdin_text)
            if on_input_ready is None:
                input_buffer.close()
        child_env = dict(env)
        try:
            process_args = {
                "stdin": subprocess.PIPE if input_buffer is not None else None,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "start_new_session": True,
            }
            if command_only:
                process_args.pop("start_new_session")
                process = self._broker.popen_command(command, cwd=cwd, **process_args)
            else:
                process = self._broker.popen(
                    command, cwd=cwd, env=child_env, **process_args
                )
        except (OSError, ValueError) as error:
            if command_only:
                raise
            raise RuntimeExecutionError(
                "provider process could not be started"
            ) from error

        if process.stdout is None or process.stderr is None:
            self._stop_process_group(process)
            raise RuntimeExecutionError("provider process pipes were unavailable")

        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        output = {"stdout": bytearray(), "stderr": bytearray()}
        pending_stdout = bytearray()
        deadline = (
            None if timeout_seconds is None else time.monotonic() + timeout_seconds
        )
        returncode: int | None = None
        cancelled = Event()
        stop_error: RuntimeExecutionError | None = None
        stop_deadline: float | None = None
        cleaned = False

        def stop() -> None:
            # Only the execution thread touches the protocol or process. Stale
            # routes cannot signal a recycled PID, and repeated stops cannot
            # extend the grace window.
            cancelled.set()

        try:
            if input_buffer is not None:
                assert process.stdin is not None
                os.set_blocking(process.stdin.fileno(), False)
                if on_input_ready is not None:
                    on_input_ready(input_buffer)
            if on_started is not None:
                on_started(stop)
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if stop_error is None and (
                    cancelled.is_set() or (deadline is not None and now >= deadline)
                ):
                    stop_error = (
                        RuntimeExecutionError("provider process was cancelled")
                        if cancelled.is_set() else ProcessTimeout(
                            f"provider process timed out after {timeout_seconds}s"
                        )
                    )
                    if on_stop is not None:
                        try:
                            on_stop()
                        except (RuntimeExecutionError, OSError):
                            # A closed native channel cannot cooperate. Contain
                            # it below without changing cancellation into timeout.
                            stop_deadline = now
                        else:
                            stop_deadline = now + _COOPERATIVE_GRACE_SECONDS
                    else:
                        stop_deadline = now
                if stop_deadline is not None and now >= stop_deadline:
                    raise stop_error

                if process.poll() is not None and not cleaned:
                    # A leader's success does not release its descendants.
                    self._stop_process_group(process)
                    cleaned = True
                if input_buffer is not None:
                    input_buffer._flush(process.stdin)

                wake_at = stop_deadline if stop_deadline is not None else deadline
                wait_seconds = (
                    0.2 if wake_at is None
                    else max(0, min(wake_at - time.monotonic(), 0.2))
                )

                if not selector.get_map():
                    try:
                        returncode = process.wait(timeout=wait_seconds)
                    except subprocess.TimeoutExpired:
                        continue
                    continue

                events = selector.select(wait_seconds)
                if not events:
                    continue
                for key, _ in events:
                    stream = cast(Any, key.fileobj)
                    stream_name = cast(str, key.data)
                    try:
                        chunk = os.read(stream.fileno(), 65_536)
                    except OSError as error:
                        raise RuntimeExecutionError(
                            "provider process stream could not be read"
                        ) from error
                    if not chunk:
                        if stream_name == "stdout" and pending_stdout.strip():
                            if on_stdout_line:
                                on_stdout_line(pending_stdout.decode("utf-8", errors="replace"))
                            pending_stdout.clear()
                        selector.unregister(stream)
                        continue
                    if stream_name == "stderr":
                        output[stream_name].extend(chunk)
                        overflow = len(output["stderr"]) - _DIAGNOSTIC_LIMIT_BYTES
                        if overflow > 0:
                            del output["stderr"][:overflow]
                        continue
                    remaining = _STREAM_LIMIT_BYTES - len(output["stdout"])
                    safe_chunk = chunk[: max(0, remaining)]
                    output["stdout"].extend(safe_chunk)
                    pending_stdout.extend(safe_chunk)
                    while b"\n" in pending_stdout:
                        raw_line, _, remainder = pending_stdout.partition(b"\n")
                        pending_stdout = bytearray(remainder)
                        if raw_line.strip() and on_stdout_line:
                            on_stdout_line(raw_line.decode("utf-8", errors="replace"))
                    if len(safe_chunk) != len(chunk):
                        raise RuntimeExecutionError(
                            "provider process stream exceeded safe limit"
                        )

            if returncode is None:
                returncode = process.wait()
            if stop_error is not None:
                raise stop_error
            if cancelled.is_set():
                raise RuntimeExecutionError("provider process was cancelled")
            return ProcessOutput(
                returncode=returncode,
                stdout=output["stdout"].decode("utf-8", errors="replace"),
                stderr=output["stderr"].decode("utf-8", errors="replace"),
            )
        except RuntimeExecutionError as error:
            if stop_error is not None and error is not stop_error:
                raise stop_error from error
            raise
        finally:
            if not cleaned:
                self._stop_process_group(process)
            if input_buffer is not None:
                input_buffer._finish()
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            selector.close()
            process.stdout.close()
            process.stderr.close()

    @staticmethod
    def _signal_process_group(
        process: subprocess.Popen[bytes], signal_number: int
    ) -> None:
        UntrustedExecutionBroker.signal_process(process, signal_number)

    def _stop_process_group(self, process: subprocess.Popen[bytes]) -> None:
        self._signal_process_group(process, signal.SIGTERM)
        grace_end = time.monotonic() + _TERMINATION_GRACE_SECONDS
        while self._broker.process_alive(process) and time.monotonic() < grace_end:
            process.poll()  # Reap the leader; descendants retain the full grace.
            time.sleep(0.02)
        self._signal_process_group(process, signal.SIGKILL)
        # Ownership cannot be released until the provider process has exited.
        process.wait()
