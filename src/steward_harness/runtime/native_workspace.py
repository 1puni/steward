"""Private native launch configuration with direct, workspace-owned records."""

from __future__ import annotations

import json
import math
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeRequest
from steward_harness.runtime.execution import UntrustedExecutionBroker

_PREPARE = r'''
import json, os, pathlib, shutil, stat, sys, tempfile

source, mappings, session, pattern, bundled_skills = json.load(sys.stdin)
source = pathlib.Path(source)
world = pathlib.Path.cwd()
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# Config/auth remain private. Each invocation owns its links; no shared link
# is retargeted when different worlds or concurrent conversations execute.
launch = pathlib.Path(tempfile.mkdtemp(prefix='.steward-launch-', dir=source))
try:
    for entry in source.iterdir():
        if entry.name.startswith('.steward-launch-') or entry.name in mappings or entry.name == 'skills':
            continue
        # Runtime SQLite belongs to this launch, not to a different Git world.
        if '.sqlite' in entry.name:
            continue
        (launch / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    # Both native providers discover skills beneath their configured home.
    # Overlay links per launch; never write through the instance's skills link.
    skills = launch / 'skills'
    skills.mkdir()
    if (source / 'skills').is_dir():
        for entry in (source / 'skills').iterdir():
            (skills / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    for name, content in bundled_skills.items():
        if not os.path.lexists(skills / name):
            (skills / name).mkdir()
            (skills / name / 'SKILL.md').write_text(content)
    for name, relative in mappings.items():
        directory = os.open('.', flags)
        try:
            for component in pathlib.PurePosixPath(relative).parts:
                if component in ('.', '..', '/'):
                    raise ValueError('invalid native record path')
                try:
                    os.mkdir(component, 0o755, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=directory)
                os.close(directory)
                directory = child
        finally:
            os.close(directory)
        (launch / name).symlink_to(world / relative, target_is_directory=True)
    resume = None
    if session:
        # Paths are selected only from this candidate's native records. Existing
        # private histories require an explicit import during upgrade.
        matches = list(world.glob(pattern.format(session=session)))
        if len(matches) != 1 or not matches[0].is_file() or matches[0].is_symlink():
            raise FileNotFoundError('native session absent or ambiguous in candidate')
        directory = os.open('.', flags)
        try:
            for component in matches[0].relative_to(world).parts[:-1]:
                child = os.open(component, flags, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(matches[0].name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError('native session must be a regular file')
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)
        resume = str(matches[0])
    print(json.dumps({'home': str(launch), 'resume': resume}))
except BaseException:
    shutil.rmtree(launch)
    raise
'''

_REMOVE = """
import pathlib, shutil, sys
path = pathlib.Path(sys.argv[1])
if not path.name.startswith('.steward-launch-') or path.is_symlink():
    raise ValueError('not a native launch directory')
shutil.rmtree(path)
"""


@dataclass(frozen=True)
class NativeWorkspace:
    home: Path
    resume: str | None
    deadline: float | None

    def remaining_request(self, request: RuntimeRequest) -> RuntimeRequest:
        if self.deadline is None:
            return request
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeExecutionError("Native workspace setup exceeded execution deadline")
        return replace(request, timeout_seconds=math.ceil(remaining))


@contextmanager
def native_workspace(
    broker: UntrustedExecutionBroker,
    request: RuntimeRequest,
    home: Path,
    *,
    mappings: Mapping[str, str],
    resume_pattern: str,
) -> Iterator[NativeWorkspace]:
    """Map native writes before launch; remove private links after writer exit."""
    # Setup steps stay finite even when the native turn has no deadline.
    deadline, setup_timeout = (None, 30) if request.timeout_seconds is None else (
        time.monotonic() + request.timeout_seconds, min(30, request.timeout_seconds))
    checked = broker.run([
        broker.python_executable, "-I", "-c",
        "import json,pathlib,sys; cwd,images,roots=json.load(sys.stdin); "
        "assert pathlib.Path(cwd).is_dir(), 'Working directory must exist'; "
        "assert all(pathlib.Path(p).is_file() for p in images), 'Images must be existing files'; "
        "assert all(pathlib.Path(p).is_dir() for p in roots), 'Writable roots must be existing directories'",
    ], cwd="/", timeout=setup_timeout,
        input_text=json.dumps([str(request.cwd), list(map(str, request.images)),
                              list(map(str, request.writable_roots))]))
    if checked.returncode:
        raise RuntimeExecutionError("Invalid native execution paths: " + checked.stderr.strip()[-1000:])
    if request.sandbox_mode != "workspace-write":
        yield NativeWorkspace(home, request.provider_session_id, deadline)
        return
    try:
        prepared = broker.run(
            [broker.python_executable, "-c", _PREPARE], cwd=request.cwd,
            timeout=setup_timeout,
            input_text=json.dumps([
                str(home), dict(mappings), request.provider_session_id, resume_pattern,
                {p.parent.name: p.read_text() for p in
                 (Path(__file__).resolve().parents[1] / "skills").glob("*/SKILL.md")},
            ]),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeExecutionError("Native workspace setup failed") from error
    if prepared.returncode:
        errors = prepared.stderr.strip().splitlines()
        detail = errors[-1][-1000:] if errors else f"setup process exited {prepared.returncode}"
        raise RuntimeExecutionError(
            f"Native workspace could not be prepared: {detail}",
            session_id=request.provider_session_id,
        )
    record = json.loads(prepared.stdout)
    workspace = NativeWorkspace(Path(record["home"]), record["resume"], deadline)
    try:
        yield workspace
    finally:
        removed = broker.run(
            [broker.python_executable, "-c", _REMOVE, str(workspace.home)],
            cwd=request.cwd, timeout=30,
        )
        if removed.returncode:
            raise RuntimeExecutionError("Native private launch cleanup failed")
