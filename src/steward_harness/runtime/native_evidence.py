"""Private, versioned native evidence snapshots; deliberately no pruning.

Executed by the execution broker using the installed rsync. Snapshots
are local recovery copies, not distributed backups or deletion authorization.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import re
import subprocess
import uuid

# Stop admitting native writers before repeat generation growth exhausts disk.
# This is admission backpressure, not a quota on an already-running provider.
EVIDENCE_PENDING = ".steward-native-evidence-pending"

MIN_FREE_BYTES = 1024 ** 3
MIN_FREE_FRACTION = 0.05


def check_headroom(paths: list[Path], required: int = 0) -> None:
    for path in paths:
        usage = shutil.disk_usage(path)
        reserve = max(MIN_FREE_BYTES, int(usage.total * MIN_FREE_FRACTION))
        if usage.free - required < reserve:
            raise RuntimeError('native evidence storage reserve reached; retain evidence and stop new native work')


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('native evidence directory must be private and owned by execution user')
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _digest(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('native evidence must be a regular file')
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _identity(path: Path) -> tuple:
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def snapshot(home: Path, records: dict[str, Path], *, seed: bool = False) -> Path:
    """Make an independently verifiable rsync --link-dest snapshot.

    Only unchanged *backup* files are hardlinked. Never link a live source,
    mutate an earlier snapshot, propagate deletion, or reclaim originals.
    Native writers must be stopped; arbitrary peer exclusion is not inferred.
    """
    if home.is_symlink() or not home.is_dir():
        raise ValueError('native evidence source must be a real home')
    store = (home if seed else home.parent) / '.steward-evidence'
    _private_directory(store)
    # Reuse identical evidence across one owner's lineage changes; never choose
    # another owner's home as the comparison snapshot. Old tar archives stay.
    group = home.name.rsplit('-', 1)[0] if re.fullmatch(
        r'\.steward-owner-[0-9a-f]{32}-[0-9a-f]{16}', home.name) else home.name
    destination = store / ('_seed' if seed else group)
    _private_directory(destination)
    previous = max((p for p in destination.glob('*.snapshot')
                    if p.is_dir() and not p.is_symlink()),
                   key=lambda p: p.stat().st_mtime_ns, default=None)
    roots = {'home': home, **{f'records/{key}': value for key, value in records.items()
                            if key != 'auto-memory' or value.exists() or value.is_symlink()}}
    exclusions = ['.steward-evidence', '.steward-read-scopes', '.steward-owner-*', '.steward-launch-*'] if seed else []
    observed, manifest, metadata = {}, {}, {}
    required = 0

    def failed_walk(error):
        raise error

    for prefix, root in roots.items():
        if root.is_symlink() or not root.is_dir():
            raise ValueError('native evidence record root must be a real directory')
        observed[root] = _identity(root)
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed_walk):
            if prefix == 'home' and Path(directory) == root and exclusions:
                import fnmatch
                dirs[:] = [name for name in dirs if not any(fnmatch.fnmatchcase(name, pattern) for pattern in exclusions)]
                files = [name for name in files if not any(fnmatch.fnmatchcase(name, pattern) for pattern in exclusions)]
            for name in dirs + files:
                path = Path(directory) / name
                info = path.lstat()
                if stat.S_ISSOCK(info.st_mode):
                    continue  # Transport endpoint, not durable contents.
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                    raise ValueError('unsupported native evidence file type')
                key = prefix + '/' + path.relative_to(root).as_posix()
                observed[path] = _identity(path)
                metadata[key] = {'mode': info.st_mode, 'mtime_ns': info.st_mtime_ns,
                                 'size': info.st_size}
                if path.is_symlink():
                    metadata[key]['target'] = os.readlink(path)
                elif stat.S_ISREG(info.st_mode):
                    manifest[key] = _digest(path)
                    prior = previous / key if previous else None
                    # A prior manifest alone is not evidence that its bytes
                    # still exist. Verify reusable data before reserving space.
                    if prior is None or not prior.is_file() or prior.is_symlink() or _digest(prior) != manifest[key]:
                        required += info.st_size
    check_headroom([store], required + len(metadata) * 4096)
    partial = destination / (uuid.uuid4().hex + '.partial')
    _private_directory(partial)
    final = partial.with_suffix('.snapshot')
    for prefix, root in roots.items():
        target = partial / prefix
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        # No --delete, --inplace, --copy-links, or source hardlinks. Omit -t:
        # source timestamps are in metadata.json, so identical content can be
        # reused even when Git rematerialized a file with a different mtime.
        command = ['rsync', '-rlp', '--checksum', '--chmod=D700,F600']
        if prefix == 'home':
            command.extend('--exclude=/' + pattern for pattern in exclusions)
        if previous is not None and (previous / prefix).is_dir():
            command.append('--link-dest=' + str(previous / prefix))
        result = subprocess.run([*command, '--', str(root) + '/', str(target) + '/'],
                                capture_output=True, timeout=240, check=False)
        if result.returncode:
            raise RuntimeError('native evidence rsync failed; partial and sources retained')
    for key, digest in manifest.items():
        target = partial / key
        if _digest(target) != digest:
            raise RuntimeError('native evidence snapshot verification failed')
        with target.open('rb') as stream:
            os.fsync(stream.fileno())
    for key, attributes in metadata.items():
        target = partial / key
        if stat.S_ISLNK(attributes['mode']):
            if not target.is_symlink() or os.readlink(target) != attributes['target']:
                raise RuntimeError('native evidence link verification failed')
        elif stat.S_ISDIR(attributes['mode']) and (target.is_symlink() or not target.is_dir()):
            raise RuntimeError('native evidence directory verification failed')
    if any(_identity(path) != before for path, before in observed.items()):
        raise RuntimeError('native evidence tree changed during snapshot')
    # Version directories share an owner namespace across generations. Keep
    # exact source roots and exclusions so a restore does not guess the lineage
    # or mistake a home-only copy for captured mapped records.
    sources = {'roots': {prefix: str(root) for prefix, root in roots.items()},
               'seed_exclusions': exclusions, 'external_symlinks': 'metadata only'}
    for name, contents in [('SHA256.json', manifest), ('metadata.json', metadata),
                           ('sources.json', sources)]:
        descriptor = os.open(partial / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(contents, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
    # Persist directory entries before publishing the complete version.
    for directory, _, _ in os.walk(partial, followlinks=False):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    os.rename(partial, final)
    descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return final


if __name__ == '__main__':
    operation, home, records, workspace = json.load(sys.stdin)
    if operation == 'check':
        check_headroom([Path(home), *map(Path, records.values())])
        if workspace:
            marker = Path(workspace) / EVIDENCE_PENDING
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
            os.fsync(descriptor)
            os.close(descriptor)
    elif operation in {'snapshot', 'snapshot-final', 'snapshot-seed', 'snapshot-seed-final'}:
        seed = operation.startswith('snapshot-seed')
        roots = {key: Path(value) for key, value in records.items()}
        if seed:
            # Actual seed directories are already in home/. Include only known
            # native record aliases that point outside that physical tree.
            roots = {key: value.resolve(strict=True) for key, value in roots.items() if value.is_symlink()}
        snapshot(Path(home), roots, seed=seed)
        if operation == 'snapshot-final' and workspace:
            (Path(workspace) / EVIDENCE_PENDING).unlink(missing_ok=True)
    else:
        raise ValueError('unknown native evidence operation')
