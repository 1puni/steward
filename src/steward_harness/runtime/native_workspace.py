"""Private native launch configuration with direct, workspace-owned records."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import subprocess
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeRequest
from steward_harness.runtime.execution import UntrustedExecutionBroker

log = logging.getLogger(__name__)

_PREPARE = r'''
import json, os, pathlib, shutil, stat, sys, tempfile, uuid

source, mappings, session, pattern, bundled_skills, owner = json.load(sys.stdin)
source = pathlib.Path(source)
world = pathlib.Path.cwd()
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# Config/auth remain private. Each invocation owns its links; no shared link
# is retargeted when different worlds or concurrent conversations execute.
persistent = owner is not None
if session and str(uuid.UUID(session)) != session:
    raise ValueError('native session must be a canonical UUID')
if persistent:
    launch = source / ('.steward-owner-' + owner['key'])
    try:
        launch.mkdir(mode=0o700)
    except FileExistsError:
        pass
    descriptor = os.open(launch, flags)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('native owner home must be owned by execution user with mode 0700')
    finally:
        os.close(descriptor)
else:
    launch = pathlib.Path(tempfile.mkdtemp(prefix='.steward-launch-', dir=source))

def link(path, target, directory=False, provider_owned=False):
    if os.path.lexists(path):
        if provider_owned and not path.is_symlink() and path.is_file():
            return
        if not path.is_symlink() or pathlib.Path(os.readlink(path)) != target:
            raise ValueError('native home link changed: ' + str(path))
    else:
        path.symlink_to(target, target_is_directory=directory)

def directory_at(root, relative, create=False):
    directory = os.open(root, flags)
    try:
        for component in pathlib.PurePosixPath(relative).parts:
            if component in ('.', '..', '/'):
                raise ValueError('invalid native record path')
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return directory
    except BaseException:
        os.close(directory)
        raise

def record_at(root, relative):
    directory = directory_at(root, relative.parent)
    try:
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError('native session must be a regular file')
        return descriptor
    finally:
        os.close(directory)

try:
    for entry in source.iterdir():
        if entry.name.startswith(('.steward-launch-', '.steward-owner-')) or entry.name in mappings or entry.name == 'skills':
            continue
        # Durable runtime state belongs to this owner, never the seed home.
        if persistent and entry.name not in owner['shared']:
            continue
        if '.sqlite' in entry.name:
            continue
        link(launch / entry.name, entry, entry.is_dir(),
             persistent and entry.name == '.claude.json')
    # Both native providers discover skills beneath their configured home.
    # Overlay links per launch; never write through the instance's skills link.
    skills = launch / 'skills'
    skills.mkdir(exist_ok=True)
    descriptor = os.open(skills, flags)
    os.close(descriptor)
    if (source / 'skills').is_dir():
        for entry in (source / 'skills').iterdir():
            destination = skills / entry.name
            if entry.name in bundled_skills and destination.is_dir() and not destination.is_symlink():
                # Preserve a previously installed default before an instance
                # override. Never erase provider edits or partially written files.
                destination.rename(launch / ('.steward-preserved-skill-' + uuid.uuid4().hex))
            link(destination, entry, entry.is_dir())
    for name, content in bundled_skills.items():
        destination = skills / name
        if destination.is_symlink():
            if pathlib.Path(os.readlink(destination)) != source / 'skills' / name:
                raise ValueError('native skill link changed: ' + str(destination))
            if destination.exists():
                continue
            # A removed instance override falls back to the bundled default.
            destination.unlink()
        directory = directory_at(launch, pathlib.Path('skills') / name, create=True)
        temporary = '.steward-skill-' + uuid.uuid4().hex
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            with os.fdopen(descriptor, 'w') as installed:
                installed.write(content)
                installed.flush()
                os.fsync(installed.fileno())
            os.replace(temporary, 'SKILL.md', src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)
    private = persistent and not owner['writable']
    record_root = launch if private else world
    for name, relative in mappings.items():
        if private:
            if pattern.startswith(relative + '/'):
                pattern = name + pattern[len(relative):]
            os.close(directory_at(launch, name, create=True))
        else:
            os.close(directory_at(world, relative, create=True))
            link(launch / name, world / relative, True)
    resume = None
    if session:
        matches = list(record_root.glob(pattern.format(session=session)))
        if private and not matches:
            # Import only the requested lineage's original, never another
            # owner's database, queue or whole private history directory.
            originals = list(source.glob(pattern.format(session=session)))
            if len(originals) == 1:
                relative = originals[0].relative_to(source)
                def refuse_companions():
                    # Claude/GLM keep session-scoped children beside the primary
                    # JSONL in <session>/. Never silently import only the parent.
                    # This detects visible companions, not an exclusive handoff.
                    if relative.parts[0] != 'projects' or relative.name != session + '.jsonl':
                        return
                    parent = directory_at(source, relative.parent)
                    try:
                        try:
                            os.stat(session, dir_fd=parent, follow_symlinks=False)
                        except FileNotFoundError:
                            return
                        raise RuntimeError('legacy native session has companion state requiring inspected migration')
                    finally:
                        os.close(parent)
                refuse_companions()
                descriptor = record_at(source, relative)
                directory = directory_at(launch, relative.parent, create=True)
                temporary = '.steward-import-' + uuid.uuid4().hex
                try:
                    with os.fdopen(descriptor, 'rb') as original:
                        # A stable destination is insufficient if the source is
                        # being written. Linux leases exclude existing writers
                        # and signal new writers; unsupported filesystems refuse.
                        import fcntl, signal
                        lease_broken = [False]
                        def source_changed(signum, frame):
                            lease_broken[0] = True
                        signal.signal(signal.SIGIO, source_changed)
                        try:
                            fcntl.fcntl(original.fileno(), fcntl.F_SETLEASE, fcntl.F_RDLCK)
                        except (AttributeError, OSError) as error:
                            raise RuntimeError('legacy native session requires an available Linux read lease: ' + str(error)) from error
                        target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=directory)
                        with os.fdopen(target, 'wb') as imported:
                            shutil.copyfileobj(original, imported)
                            imported.flush()
                            os.fsync(imported.fileno())
                        if lease_broken[0]:
                            raise RuntimeError('legacy native session writer requested access')
                        if fcntl.fcntl(original.fileno(), fcntl.F_GETLEASE) != fcntl.F_RDLCK:
                            raise RuntimeError('legacy native session read lease was broken')
                        # Closing the source releases the lease. The complete,
                        # fsynced destination no longer depends on source writes.
                    # An interrupted copy can never masquerade as a complete
                    # resumable original. Existing records are never replaced.
                    refuse_companions()
                    os.link(temporary, relative.name, src_dir_fd=directory,
                            dst_dir_fd=directory, follow_symlinks=False)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
                    os.close(directory)
                matches = [launch / relative]
        if len(matches) != 1 or matches[0].is_symlink():
            raise FileNotFoundError('native session absent or ambiguous in candidate or owner home')
        os.close(record_at(record_root, matches[0].relative_to(record_root)))
        resume = str(matches[0])
    print(json.dumps({'home': str(launch), 'resume': resume}))
except BaseException:
    if not persistent:
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

# Removes every generation and provider of one owner. rmtree unlinks the links
# into the seed home and the world checkout; it never follows them.
_RETIRE = """
import json, pathlib, shutil, sys
homes, prefix = json.load(sys.stdin)
for home in map(pathlib.Path, homes):
    if home.is_dir():
        for entry in home.iterdir():
            if entry.name.startswith(prefix) and entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
                print(entry)
"""


def _owner_prefix(owner: str) -> str:
    return ".steward-owner-" + hashlib.sha256(owner.encode()).hexdigest()[:32] + "-"


def retire_native_owner(broker: UntrustedExecutionBroker, homes: Iterable[Path], owner: str) -> None:
    """Delete an owner's durable homes; the caller holds the owner's admission fence."""
    retired = broker.run(
        [broker.python_executable, "-I", "-c", _RETIRE], cwd="/", timeout=60,
        input_text=json.dumps([list(map(str, homes)), _owner_prefix(owner)]),
    )
    if retired.returncode:
        raise RuntimeError("native owner home retirement failed: " + retired.stderr.strip()[-500:])
    for path in retired.stdout.split():
        log.info("retired native owner home %s", path)


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
    """Prepare owner state before launch; only anonymous launch homes are removed."""
    # Setup steps stay finite even when the native turn has no deadline.
    deadline, setup_timeout = (None, 30) if request.timeout_seconds is None else (
        time.monotonic() + request.timeout_seconds, min(30, request.timeout_seconds))
    try:
        checked = broker.run([
            broker.python_executable, "-I", "-c",
            "import json,pathlib,sys; cwd,images,roots=json.load(sys.stdin); "
            "assert pathlib.Path(cwd).is_dir(), 'Working directory must exist'; "
            "assert all(pathlib.Path(p).is_file() for p in images), 'Images must be existing files'; "
            "assert all(pathlib.Path(p).is_dir() for p in roots), 'Writable roots must be existing directories'",
        ], cwd="/", timeout=setup_timeout,
            input_text=json.dumps([str(request.cwd), list(map(str, request.images)),
                                  list(map(str, request.writable_roots))]))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeExecutionError(
            "Native workspace path validation failed", session_id=request.provider_session_id,
        ) from error
    if checked.returncode:
        raise RuntimeExecutionError(
            "Invalid native execution paths: " + checked.stderr.strip()[-1000:],
            session_id=request.provider_session_id,
        )
    if request.sandbox_mode != "workspace-write" and request.native_owner is None:
        yield NativeWorkspace(home, request.provider_session_id, deadline)
        return
    if deadline is not None:
        setup_timeout = min(30, deadline - time.monotonic())
        if setup_timeout <= 0:
            raise RuntimeExecutionError(
                "Native workspace setup exceeded execution deadline",
                session_id=request.provider_session_id,
            )
    try:
        prepared = broker.run(
            [broker.python_executable, "-I", "-c", _PREPARE], cwd=request.cwd,
            timeout=setup_timeout,
            input_text=json.dumps([
                str(home), dict(mappings), request.provider_session_id, resume_pattern,
                {p.parent.name: p.read_text() for p in
                 (Path(__file__).resolve().parents[1] / "skills").glob("*/SKILL.md")},
                None if request.native_owner is None else {
                    # One prefix per owner lets retirement find every generation.
                    "key": _owner_prefix(request.native_owner)[len(".steward-owner-"):]
                    + hashlib.sha256(json.dumps([
                        request.resolved.provider, request.native_generation,
                    ]).encode()).hexdigest()[:16],
                    "writable": request.sandbox_mode == "workspace-write",
                    "shared": ["auth.json", "config.toml", "requirements.toml", "plugins", "AGENTS.md",
                               "AGENTS.override.md", "rules", "prompts", "hooks.json", "agents"]
                    if request.resolved.provider == "codex" else
                    [".claude.json", "settings.json", "settings.local.json",
                     "plugins", "commands", "agents", "CLAUDE.md", "rules", "output-styles"],
                },
            ]),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeExecutionError(
            "Native workspace setup failed", session_id=request.provider_session_id,
        ) from error
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
        if request.native_owner is None:
            removed = broker.run(
                [broker.python_executable, "-I", "-c", _REMOVE, str(workspace.home)],
                cwd=request.cwd, timeout=30,
            )
            if removed.returncode:
                raise RuntimeExecutionError("Native private launch cleanup failed")
