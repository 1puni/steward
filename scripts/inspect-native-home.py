#!/usr/bin/env python3
"""Inventory one explicitly selected native home; never authorize its retirement."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat


MAX_ENTRIES = 10_000
MAX_DEPTH = 32


def inspect_home(path: Path) -> dict:
    """Read directory metadata only, without traversing links or special files.

    Open each component relative to an already opened directory. The result is
    an observation, not a coherent snapshot or evidence that writers are absent.
    Any traversal error refuses a complete report.
    """
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('home must be an absolute path without parent traversal')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root = os.open('/', flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=root)
            os.close(root)
            root = child
        entries = []
        total_bytes = 0

        def visit(directory: int, prefix: str, depth: int) -> None:
            nonlocal total_bytes
            # scandir streams names; listdir would allocate an unbounded list
            # before our entry limit could refuse a large home.
            with os.scandir(directory) as children:
                for child in children:
                    if len(entries) >= MAX_ENTRIES:
                        raise ValueError('native home exceeds inspection entry limit')
                    info = os.stat(child.name, dir_fd=directory, follow_symlinks=False)
                    name = prefix + child.name
                    kind = ('directory' if stat.S_ISDIR(info.st_mode) else
                            'file' if stat.S_ISREG(info.st_mode) else
                            'symlink' if stat.S_ISLNK(info.st_mode) else 'special')
                    entries.append(dict(path=name, kind=kind, device=info.st_dev,
                                        inode=info.st_ino, mode=stat.S_IMODE(info.st_mode),
                                        uid=info.st_uid, gid=info.st_gid, size=info.st_size,
                                        links=info.st_nlink, mtime_ns=info.st_mtime_ns))
                    if kind == 'file':
                        total_bytes += info.st_size
                    if kind == 'directory':
                        if depth >= MAX_DEPTH:
                            raise ValueError('native home exceeds inspection depth limit')
                        nested = os.open(child.name, flags, dir_fd=directory)
                        try:
                            opened = os.fstat(nested)
                            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                                raise ValueError('native home directory changed during inspection')
                            visit(nested, name + '/', depth + 1)
                        finally:
                            os.close(nested)

        identity = os.fstat(root)
        visit(root, '', 0)
        return dict(home=str(path), device=identity.st_dev, inode=identity.st_ino,
                    entries=sorted(entries, key=lambda entry: entry['path']),
                    regular_file_bytes=total_bytes,
                    coherent_snapshot=False, writer_absence_verified=False,
                    retirement_authorized=False)
    finally:
        os.close(root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('home', type=Path)
    args = parser.parse_args()
    try:
        report = inspect_home(args.home)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Native home inspection incomplete: {error}\n')
    print(json.dumps(report, indent=2, ensure_ascii=True))


if __name__ == '__main__':
    main()
