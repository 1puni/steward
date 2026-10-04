"""Native records reach Git from the home that owns them, never from a copy.

Executed by the execution broker. A retained owner home holds its provider's
records as ordinary files. After each invocation they are staged into the
writable checkout's index at their world paths, so the turn's commit carries
them, Git deltas them, and every push replicates them. The checkout never has
to materialize `artefacts/`; see `leave_to_git`.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

# Stop admitting native writers before generation growth exhausts disk.
# This is admission backpressure, not a quota on an already-running provider.
EVIDENCE_PENDING = ".steward-native-evidence-pending"

#: Provider-written records under this world path live in owner homes and are
#: captured into Git; world checkouts leave the path out.
CAPTURED_ROOT = "artefacts/"

MIN_FREE_BYTES = 1024 ** 3
MIN_FREE_FRACTION = 0.05

_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def check_headroom(paths: list[Path], required: int = 0) -> None:
    for path in paths:
        usage = shutil.disk_usage(path)
        reserve = max(MIN_FREE_BYTES, int(usage.total * MIN_FREE_FRACTION))
        if usage.free - required < reserve:
            raise RuntimeError('native evidence storage reserve reached; retain evidence and stop new native work')


def _git(workspace: Path, *args: str, input_text: str | None = None,
         env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ['git', *args], cwd=workspace, input=input_text, capture_output=True, text=True,
        timeout=240, check=False, env={**os.environ, **_GIT_ENV, **(env or {})},
    )
    if result.returncode:
        raise RuntimeError(f'native record capture: git {args[0]} failed: {result.stderr.strip()[-500:]}')
    return result.stdout


def capture(home: Path, mappings: dict[str, str], workspace: Path) -> int:
    """Stage every record under home/<name> at <relative>/... in `workspace`'s index.

    Each mapping keeps a private Git index in the home, so Git's stat cache
    hashes only what changed since the last capture into this repository.
    Entries are marked skip-worktree: the checkout does not hold these files,
    and a later `git add --all` must not read their absence as deletion. Files
    the provider removed stay in Git; capture only adds and updates.
    """
    roots = {name: home / name for name in mappings}
    if not any(files for root in roots.values() if root.is_dir() and not root.is_symlink()
               for _, _, files in os.walk(root)):
        return 0  # Nothing written, so nothing to ask Git about.
    git_dir = _git(workspace, 'rev-parse', '--absolute-git-dir').strip()
    common = _git(workspace, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()
    # Blobs live in one repository; an index from another would name missing ones.
    tag = hashlib.sha256(common.encode()).hexdigest()[:16]
    entries: list[tuple[str, str, str]] = []
    for name, relative in mappings.items():
        root = home / name
        if root.is_symlink() or not root.is_dir():
            continue
        index = home / f'.steward-index-{tag}-{name}'
        # Only this capture writes this index; a lock left by a killed one is stale.
        Path(str(index) + '.lock').unlink(missing_ok=True)
        private = {'GIT_DIR': git_dir, 'GIT_WORK_TREE': str(root), 'GIT_INDEX_FILE': str(index)}
        _git(root, 'add', '--all', '--force', '--', '.', env=private)
        for line in _git(root, 'ls-files', '--stage', '-z', env=private).split('\0'):
            if not line:
                continue
            meta, path = line.split('\t', 1)
            mode, blob, _ = meta.split(' ')
            if mode in {'100644', '100755'}:
                entries.append((mode, blob, f'{relative}/{path}'))
    if not entries:
        return 0
    _git(workspace, 'update-index', '--add', '-z', '--index-info', input_text=''.join(
        f'{mode} {blob}\t{target}\0' for mode, blob, target in entries))
    _git(workspace, 'update-index', '-z', '--skip-worktree', '--stdin',
         input_text=''.join(f'{target}\0' for _, _, target in entries))
    # A checkout from before capture may still hold an older copy. The home is
    # the authority now; left in place, a later release would commit it back.
    root = Path(os.path.realpath(workspace))
    for _, _, target in entries:
        stale = workspace / target
        if Path(os.path.realpath(stale.parent)) == root / Path(target).parent and stale.is_file():
            stale.unlink()
    return len(entries)


def leave_to_git(workspace: Path) -> int:
    """Keep committed provider records out of a checkout; Git already holds them.

    Entries become skip-worktree and their clean copies are removed. A record
    with local changes stays, unmarked, so the next commit still takes it.
    Git reports a path beyond a symlink as absent, so nothing outside the
    checkout is ever unlinked.
    """
    listed = [path for path in _git(workspace, 'ls-files', '-z', '--', CAPTURED_ROOT).split('\0') if path]
    if not listed:
        return 0
    _git(workspace, 'update-index', '-z', '--no-skip-worktree', '--stdin',
         input_text=''.join(f'{path}\0' for path in listed))
    # Restat files a merge or checkout just wrote; exit 1 only means "changed".
    subprocess.run(['git', 'update-index', '-q', '--refresh'], cwd=workspace, capture_output=True,
                   timeout=240, check=False, env={**os.environ, **_GIT_ENV})
    changed = set(_git(workspace, 'diff-files', '--name-only', '-z', '--', CAPTURED_ROOT).split('\0'))
    absent = set(_git(workspace, 'ls-files', '--deleted', '-z', '--', CAPTURED_ROOT).split('\0'))
    clean = [path for path in listed if path not in changed]
    for path in clean:
        os.unlink(workspace / path)
    for directory, _, _ in os.walk(workspace / CAPTURED_ROOT, topdown=False):
        try:
            os.rmdir(directory)
        except OSError:
            pass
    released = clean + [path for path in listed if path in absent]
    _git(workspace, 'update-index', '-z', '--skip-worktree', '--stdin',
         input_text=''.join(f'{path}\0' for path in released))
    return len(clean)


if __name__ == '__main__':
    operation, home, records, workspace = json.load(sys.stdin)
    if operation == 'check':
        try:
            check_headroom([Path(home), *map(Path, records.values())])
        except RuntimeError as error:
            print(error, file=sys.stderr)
            sys.exit(75)  # EX_TEMPFAIL: admission deferred, nothing started.
        if workspace:
            marker = Path(workspace) / EVIDENCE_PENDING
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
            os.fsync(descriptor)
            os.close(descriptor)
    elif operation == 'capture':
        # The marker fences the checkout until its records are in the index.
        if workspace:
            capture(Path(home), records, Path(workspace))
            (Path(workspace) / EVIDENCE_PENDING).unlink(missing_ok=True)
    elif operation == 'release':
        leave_to_git(Path(workspace))
    else:
        raise ValueError('unknown native evidence operation')
