"""A kernel flock plus a process mutex: one holder per lock file, released on exit."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import threading
import time
from pathlib import Path


class Busy(RuntimeError):
    """The lease did not become available before its deadline."""


_LOCK_REGISTRY_GUARD = threading.Lock()
_LOCK_REGISTRY: dict[str, threading.Lock] = {}


class Lease:
    """In-process mutex plus kernel advisory lock for single-writer access."""

    def __init__(
        self,
        state_dir: str | Path,
        *,
        timeout_seconds: float = 5.0,
        lock_name: str = "world.lock",
        require_existing: bool = False,
    ) -> None:
        self._state_dir = Path(state_dir).resolve()
        self._require_existing = require_existing
        if not self._state_dir.is_dir() and require_existing:
            raise FileNotFoundError(
                f"Required world lock directory is unavailable: {self._state_dir}"
            )
        if not self._state_dir.is_dir():
            self._state_dir.mkdir(parents=True, exist_ok=True)
        if timeout_seconds < 0:
            raise ValueError("Lease timeout must be non-negative")
        self._timeout = float(timeout_seconds)
        if not lock_name or Path(lock_name).name != lock_name:
            raise ValueError("Lock name must be one file name")
        self.path = self._state_dir / lock_name

        with _LOCK_REGISTRY_GUARD:
            self._mutex = _LOCK_REGISTRY.setdefault(str(self.path), threading.Lock())
        self._descriptor: int | None = None
        self._mutex_held = False
        self._owner_thread: int | None = None
        self._depth = 0

    def acquire(self) -> Lease:
        if self._descriptor is not None:
            if self._owner_thread == threading.get_ident():
                self._depth += 1
                return self
        deadline = time.monotonic() + self._timeout
        if self._timeout == 0:
            acquired = self._mutex.acquire(blocking=False)
        else:
            acquired = self._mutex.acquire(timeout=max(0.0, deadline - time.monotonic()))

        if not acquired:
            raise Busy("busy (thread mutex contention)")

        self._mutex_held = True
        descriptor: int | None = None
        try:
            flags = (
                os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            if not self._require_existing:
                flags |= os.O_CREAT
            descriptor = os.open(self.path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("Lock file is unavailable")

            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._descriptor = descriptor
                    self._owner_thread = threading.get_ident()
                    self._depth = 1
                    descriptor = None
                    return self
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Busy("busy (kernel lock contention)") from None
                    time.sleep(0.01)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            if self._mutex_held:
                self._mutex_held = False
                self._mutex.release()
            raise

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        if self._owner_thread != threading.get_ident():
            raise RuntimeError("Lease is owned by another thread")
        if self._depth > 1:
            self._depth -= 1
            return
        self._descriptor = None
        self._owner_thread = None
        self._depth = 0
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            if self._mutex_held:
                self._mutex_held = False
                self._mutex.release()

    def held(self) -> bool:
        """Whether any holder, in this process or another, has the lease now.

        A probe, not a claim: it takes the kernel lock for an instant only if
        nobody holds it, so a concurrent acquire may see a momentary Busy.
        """
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return True
            raise
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)

    def __enter__(self) -> Lease:
        return self.acquire()

    def __exit__(self, _type, _value, _traceback) -> None:
        self.release()
