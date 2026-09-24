"""Focused process/thread ownership tests for leases."""

from __future__ import annotations

import threading

import pytest

from steward_harness.lease import Busy, Lease


def test_one_lease_is_reentrant_only_for_its_owning_thread(tmp_path) -> None:
    lease = Lease(tmp_path, timeout_seconds=0)
    contender = Lease(tmp_path, timeout_seconds=0)

    with lease:
        with lease:
            with pytest.raises(Busy, match="busy"):
                contender.acquire()

        # Leaving the nested context must retain the underlying kernel lock.
        with pytest.raises(Busy, match="busy"):
            contender.acquire()

    with contender:
        pass


def test_same_lease_serializes_ownership_across_threads(tmp_path) -> None:
    lease = Lease(tmp_path, timeout_seconds=1)
    waiting = threading.Event()
    acquired = threading.Event()
    errors: list[BaseException] = []

    def contend() -> None:
        waiting.set()
        try:
            with lease:
                acquired.set()
        except BaseException as exc:
            errors.append(exc)

    with lease:
        thread = threading.Thread(target=contend)
        thread.start()
        assert waiting.wait(1)
        assert not acquired.wait(0.05)

    thread.join(1)
    assert not thread.is_alive()
    assert acquired.is_set()
    assert errors == []

    # Ownership state and the shared mutex must both survive the handoff.
    with lease:
        pass


def test_another_thread_times_out_and_cannot_release_the_owners_lease(tmp_path) -> None:
    lease = Lease(tmp_path, timeout_seconds=0)
    errors: list[BaseException] = []

    def contend() -> None:
        try:
            lease.acquire()
        except BaseException as exc:
            errors.append(exc)
        try:
            lease.release()
        except BaseException as exc:
            errors.append(exc)

    with lease:
        thread = threading.Thread(target=contend)
        thread.start()
        thread.join()

        assert len(errors) == 2
        assert isinstance(errors[0], Busy)
        assert str(errors[0]) == "busy (thread mutex contention)"
        assert isinstance(errors[1], RuntimeError)
        assert str(errors[1]) == "Lease is owned by another thread"

        # The failed cross-thread release must not unlock the path.
        with pytest.raises(Busy, match="busy"):
            Lease(tmp_path, timeout_seconds=0).acquire()


def test_lease_never_follows_an_attacker_controlled_lock_symlink(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("do not lock through this file")
    (tmp_path / "world.lock").symlink_to(target)

    with pytest.raises(OSError):
        Lease(tmp_path, timeout_seconds=0).acquire()

    assert target.read_text() == "do not lock through this file"


def test_explicit_shared_lease_requires_and_retains_one_existing_inode(tmp_path) -> None:
    lock = tmp_path / "world.lock"
    with pytest.raises(FileNotFoundError):
        Lease(tmp_path, timeout_seconds=0, require_existing=True).acquire()
    assert not lock.exists()

    lock.touch()
    inode = lock.stat().st_ino
    with Lease(tmp_path, timeout_seconds=0, require_existing=True):
        assert lock.stat().st_ino == inode
    assert lock.stat().st_ino == inode
