"""Systemd owns Linux host-UID invocations, including escaped descendants."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import time
import subprocess
import sys
import tempfile
import threading
from typing import Any, Mapping, Sequence
import uuid

from steward_harness.runtime.ownership_launcher import process_identity

_LAUNCHER = Path(__file__).with_name("ownership_launcher.py").resolve()


def prerequisites() -> None:
    if os.geteuid() != 0 or not Path('/sys/fs/cgroup/cgroup.controllers').exists():
        raise ValueError('host-UID ownership requires Linux root and cgroup v2')
    if not Path('/run/systemd/system').is_dir() or Path('/proc/1/comm').read_text().strip() != 'systemd':
        raise ValueError('host-UID ownership requires the real systemd system manager')
    if not shutil.which('systemd-run') or not shutil.which('systemctl'):
        raise ValueError('host-UID ownership requires systemd-run and systemctl')
    if not hasattr(os, "pidfd_open"):
        raise ValueError("host-UID ownership requires Python pidfd support")
    # Verify the kernel lifetime primitive before preparing an invocation.
    controller_fd = os.pidfd_open(os.getpid())
    os.close(controller_fd)
    # This executable is run by PID 1 as root; neither it nor its namespace may
    # be replaced by the execution account.
    for target in (_LAUNCHER, Path(sys.executable).resolve()):
        for path in (target, *target.parents):
            metadata = path.stat()
            if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                raise ValueError(f'host-UID launcher is not protected: {path}')


class OwnedProcess(subprocess.Popen):
    """Wait covers service teardown; signals target only this invocation."""

    _kill_whom = "all"

    def __init__(self, argv: Sequence[str], *, unit: str, payload: Path, **kwargs: Any):
        self.unit = unit
        self.payload = payload
        super().__init__(argv, **kwargs)

    def send_signal(self, sig: int) -> None:
        if self.poll() is not None:
            return
        # Signals address the service, including descendants that escaped its leader.
        action = ["systemctl", "stop", "--no-block", self.unit]
        if sig == signal.SIGKILL:
            action = ["systemctl", "kill", "--kill-whom=" + self._kill_whom, "--signal=KILL", self.unit]
        deadline = time.monotonic() + 15
        while self.poll() is None:
            result = subprocess.run(action, capture_output=True, timeout=15)
            if result.returncode == 0 or self.poll() is not None:
                return
            # Popen returns before systemd-run has registered its unit. An
            # immediate cancellation must follow that launch through to a stop.
            missing = b"not loaded" in result.stderr or b"not found" in result.stderr
            if not missing or time.monotonic() >= deadline:
                raise RuntimeError(f"cannot stop owned invocation {self.unit}")
            time.sleep(0.05)


    def _verify_empty(self) -> None:
        events = Path('/sys/fs/cgroup/system.slice') / self.unit / 'cgroup.events'

        def populated() -> bool:
            try:
                return 'populated 1' in events.read_text()
            except FileNotFoundError:
                return False

        if getattr(self, '_ownership_finished', False):
            return
        # Revoke a not-yet-consumed launch before checking the unit. A queued
        # StartTransientUnit may not have created its cgroup yet. Once the
        # launcher consumes this file it is already inside the owned cgroup.
        self.payload.unlink(missing_ok=True)
        try:
            self.payload.parent.rmdir()
        except FileNotFoundError:
            pass
        # Reconcile the named unit even before its cgroup exists: the client
        # may die while systemd is still starting an accepted invocation job.
        result = subprocess.run(['systemctl', 'stop', self.unit],
                                capture_output=True, timeout=15)
        missing = b'not loaded' in result.stderr or b'not found' in result.stderr
        if populated() or (result.returncode and not missing):
            raise RuntimeError(f'ownership teardown failed for {self.unit}: {result.stderr.decode()}')
        self._ownership_finished = True

    def poll(self):
        result = super().poll()
        if result is not None:
            self._verify_empty()
        return result

    def communicate(self, input=None, timeout=None):
        # Native children inherit the pipe directly. Watch the owning client
        # independently: its death need not close those pipes. communicate must
        # run exactly once so a slow reader still receives the entire input.
        finished = threading.Event()
        failures: list[BaseException] = []

        def watch() -> None:
            try:
                while not finished.wait(.1):
                    if self.poll() is not None:
                        return
            except BaseException as exc:
                failures.append(exc)

        watcher = threading.Thread(target=watch, name='steward-invocation-cleanup')
        watcher.start()
        try:
            return super().communicate(input, timeout=timeout)
        finally:
            finished.set()
            watcher.join()
            if failures:
                raise failures[0]

    def wait(self, timeout=None):
        result = super().wait(timeout)
        self._verify_empty()
        return result


def popen(command: Sequence[str], cwd: str | Path, environment: Mapping[str, str],
          *, uid: int, gid: int, **kwargs: Any) -> OwnedProcess:
    prerequisites()
    scratch = Path(tempfile.mkdtemp(prefix='steward-exec-', dir='/run'))
    unit = 'steward-exec-' + uuid.uuid4().hex + '.service'
    try:
        payload = scratch / 'invocation.json'
        payload.write_text(json.dumps(dict(argv=list(command), cwd=str(cwd),
                                          env=dict(environment), uid=uid, gid=gid,
                                          controller_pid=os.getpid(), controller_start=process_identity(os.getpid()))))
        argv = ['systemd-run', '--quiet', '--pipe', '--wait', '--collect',
                '--unit=' + unit,
                '-p', 'KillMode=control-group', '-p', 'TimeoutStopSec=5s',
                '-p', 'Slice=system.slice',
                '-p', 'ExecStopPost=' + shlex.join(['/bin/rm', '-rf', str(scratch)]),
                str(Path(sys.executable).resolve()), '-I', '-S', str(_LAUNCHER), str(payload)]
        return OwnedProcess(argv, unit=unit, payload=payload, cwd='/',
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'}, **kwargs)
    except BaseException:
        shutil.rmtree(scratch)
        raise

