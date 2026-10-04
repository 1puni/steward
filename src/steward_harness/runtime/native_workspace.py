"""Private native launch configuration; retained homes own their native records."""

from __future__ import annotations

import hashlib
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
from steward_harness.runtime.native_evidence import CAPTURED_ROOT

_PREPARE = r'''
import json, os, pathlib, shutil, stat, subprocess, sys, tempfile, uuid

source, mappings, session, pattern, bundled_skills, owner, owned, records = json.load(sys.stdin)
source = pathlib.Path(source)
world = pathlib.Path.cwd()
# Where this run's records are committed, when that is not where it works.
records = pathlib.Path(records) if records else world
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
        if entry.name.startswith(('.steward-launch-', '.steward-owner-', '.steward-evidence')) or entry.name in mappings or entry.name in {'skills', '.steward-tmp'}:
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
    # The home owns these records as real files; after each run they are
    # staged into the record checkout's index, so no checkout has to
    # materialize them. See native_evidence.capture.
    owned = set(owned)
    record_root, world_pattern, session_mapping = world, pattern, None
    for name, relative in mappings.items():
        if name in owned:
            if pattern.startswith(relative + '/'):
                pattern = name + pattern[len(relative):]
                record_root, session_mapping = launch, (name, relative)
            if persistent and owner['writable'] and (launch / name).is_symlink():
                # This home used to write through into a world checkout. Those
                # records are in the world's Git; the lineage's own are imported.
                (launch / name).unlink()
            os.close(directory_at(launch, name, create=True))
        else:
            # Native memory is a world file the run edits in its record checkout.
            os.close(directory_at(records, relative, create=True))
            link(launch / name, records / relative, True)

    def place(relative, data):
        # An interrupted copy can never masquerade as a complete resumable
        # original. Existing records are never replaced.
        directory = directory_at(launch, relative.parent, create=True)
        temporary = '.steward-import-' + uuid.uuid4().hex
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            with os.fdopen(descriptor, 'wb') as imported:
                imported.write(data)
                imported.flush()
                os.fsync(imported.fileno())
            try:
                os.link(temporary, relative.name, src_dir_fd=directory,
                        dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                pass
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

    def import_from_world():
        # Only this lineage's session and its companions (Claude/GLM keep
        # session-scoped children in <session>/). A file still in the checkout
        # was written after the last capture and is newer than the index's.
        name, relative = session_mapping
        spec = world_pattern.format(session=session)
        specs = [spec] + ([spec[:-len('.jsonl')] + '/**/*'] if spec.endswith(session + '.jsonl') else [])
        git_env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
        listed = subprocess.run(['git', 'ls-files', '-z', '--', *(':(glob)' + s for s in specs)],
                                cwd=records, capture_output=True, check=True, env=git_env).stdout
        tracked = {path for path in listed.decode().split('\0') if path}
        inside = os.path.realpath(records) + os.sep
        on_disk = {p.relative_to(records).as_posix() for s in specs for p in records.glob(s)
                   if p.is_file() and not p.is_symlink() and os.path.realpath(p).startswith(inside)}
        for path in sorted(tracked | on_disk):
            if not path.startswith(relative + '/'):
                continue
            data = (records / path).read_bytes() if path in on_disk else subprocess.run(
                ['git', 'cat-file', 'blob', ':' + path], cwd=records, capture_output=True,
                check=True, env=git_env).stdout
            place(pathlib.PurePosixPath(name, path[len(relative) + 1:]), data)
        return list(launch.glob(pattern.format(session=session)))

    resume = None
    if session:
        matches = list(record_root.glob(pattern.format(session=session)))
        if not matches and session_mapping and not private:
            matches = import_from_world()
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
    # Setup may have created unique native state; preserve it for recovery.
    raise
'''


def _owner_prefix(owner: str) -> str:
    return ".steward-owner-" + hashlib.sha256(owner.encode()).hexdigest()[:32] + "-"


_PRIVATE_TEMP = r'''
import os, stat, sys
home = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    try:
        os.mkdir('.steward-tmp', 0o700, dir_fd=home)
    except FileExistsError:
        pass
    child = os.open('.steward-tmp', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home)
    try:
        info = os.fstat(child)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('native temporary directory must be private and owned by execution user')
    finally:
        os.close(child)
    os.fsync(home)
finally:
    os.close(home)
'''


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
    """Prepare native state and preserve independent, private recovery archives."""
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
    writable = request.sandbox_mode == "workspace-write"
    # Every run's native records are committed somewhere: its own checkout when
    # it writes, else the checkout its caller names (a read-only task's branch).
    checkout = request.record_checkout or (request.cwd if writable else None)
    evidence_script = Path(__file__).with_name("native_evidence.py").read_text()

    def evidence(operation, native_home, records):
        result = broker.run(
            [broker.python_executable, "-I", "-c", evidence_script],
            cwd=request.cwd, timeout=300,
            input_text=json.dumps([operation, str(native_home), records,
                                  str(checkout) if checkout else None]),
        )
        if result.returncode:
            raise RuntimeExecutionError("Native evidence preservation failed; originals retained: "
                                        + result.stderr.strip()[-500:])

    def prepare_temporary(native_home):
        if request.resolved.provider not in {"claude", "glm"}:
            return
        result = broker.run([broker.python_executable, "-I", "-c", _PRIVATE_TEMP,
                             str(native_home)], cwd=request.cwd, timeout=setup_timeout)
        if result.returncode:
            raise RuntimeExecutionError("Native temporary directory preparation failed; originals retained")

    evidence("check", home, {"workspace": str(request.cwd)})
    # Only a read-only, ownerless run with nowhere to commit uses the seed home
    # directly; given a record checkout it gets a launch home and is captured.
    if request.sandbox_mode != "workspace-write" and request.native_owner is None and checkout is None:
        prepare_temporary(home)
        yield NativeWorkspace(home, request.provider_session_id, deadline)
        return
    if deadline is not None:
        setup_timeout = min(30, deadline - time.monotonic())
        if setup_timeout <= 0:
            raise RuntimeExecutionError(
                "Native workspace setup exceeded execution deadline",
                session_id=request.provider_session_id,
            )
    # Provider records live in the home and reach Git by capture. A writable
    # run's other mappings (native memory) are world files it edits directly;
    # a read-only run can edit nothing, so the home owns and captures them all.
    owned = {name for name, relative in mappings.items()
             if not writable or relative.startswith(CAPTURED_ROOT)}
    owner_key = None if request.native_owner is None else (
        # One prefix per owner lets retirement find every generation.
        _owner_prefix(request.native_owner)[len(".steward-owner-"):]
        + hashlib.sha256(json.dumps([
            request.resolved.provider, request.native_generation,
        ]).encode()).hexdigest()[:16]
    )
    try:
        prepared = broker.run(
            [broker.python_executable, "-I", "-c", _PREPARE], cwd=request.cwd,
            timeout=setup_timeout,
            input_text=json.dumps([
                str(home), dict(mappings), request.provider_session_id, resume_pattern,
                {p.parent.name: p.read_text() for p in
                 (Path(__file__).resolve().parents[1] / "skills").glob("*/SKILL.md")},
                None if owner_key is None else {
                    "key": owner_key,
                    "writable": writable,
                    "shared": ["auth.json", "config.toml", "requirements.toml", "plugins", "AGENTS.md",
                               "AGENTS.override.md", "rules", "prompts", "hooks.json", "agents"]
                    if request.resolved.provider == "codex" else
                    [".claude.json", "settings.json", "settings.local.json",
                     "plugins", "commands", "agents", "CLAUDE.md", "rules", "output-styles"],
                },
                sorted(owned),
                str(checkout) if checkout else None,
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
    captured = {name: mappings[name] for name in owned} if checkout else {}
    prepare_temporary(workspace.home)
    try:
        yield workspace
    finally:
        # ProcessController has already torn down the provider and descendants.
        # Homes are retained; Git now holds a copy, not the only one.
        evidence("capture", workspace.home, captured)
