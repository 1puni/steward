"""The per-task execution lock, and the trailers a slice commit carries."""

from __future__ import annotations

from pathlib import Path

from steward_harness.lease import Lease

#: The trailers the harness writes on every commit that closes a slice.
DISPOSITION_TRAILER = "Disposition"
REASON_TRAILER = "Reason"


def task_lock(root: str | Path, task_id: object, *, timeout: float = 0) -> Lease:
    """The lock that *is* a task's ``running`` state; acquiring does not wait by default.

    A ``flock`` is released by the kernel when its holder dies, so a crashed
    task simply stops being running and no recovery pass has to find it. A
    second holder, in this process or another, is refused with ``Busy``.
    """
    return Lease(root, lock_name=f"{task_id}.lock", timeout_seconds=timeout)


def locked_tasks(root: str | Path) -> frozenset[str]:
    """Every task whose lock some live process currently holds.

    Bounded by the number of tasks ever dispatched in this workdir, and it
    costs no Git invocation at all.
    """
    directory = Path(root)
    if not directory.is_dir():
        return frozenset()
    running = set()
    for path in sorted(directory.glob("*.lock")):
        try:
            if task_lock(directory, path.stem).held():
                running.add(path.stem)
        except OSError:
            continue  # not a lock anyone can hold
    return frozenset(running)
