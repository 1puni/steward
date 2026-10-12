"""Git maintenance runs in retention, never detached inside an invocation."""

import os
import subprocess
import time

import pytest

from steward_harness import retention
from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.git import hardened_git_argv
from steward_harness.retention import maintain_git
from steward_harness.runtime.execution import GIT_AUTO_MAINTENANCE_OFF, UntrustedExecutionBroker


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _loose(git_dir, count=600):
    """Enough reachable loose objects that every objects/xx directory Git samples has some."""
    payload = git_dir.parent / f"payload-{git_dir.name}"
    payload.mkdir(exist_ok=True)
    for index in range(count):
        (payload / f"{index}").write_text(f"object {index}\n")
    work = ["git", f"--git-dir={git_dir}", f"--work-tree={payload}",
            "-c", "user.name=t", "-c", "user.email=t@t", "-c", "gc.auto=0"]
    subprocess.run([*work, "add", "-A"], check=True, capture_output=True)
    subprocess.run([*work, "commit", "-q", "-m", "objects"], check=True, capture_output=True)


def _counts(git_dir):
    lines = _git(f"--git-dir={git_dir}", "count-objects", "-v").splitlines()
    return dict(line.split(": ") for line in lines)


@pytest.fixture
def low_threshold(monkeypatch):
    # Git samples objects/17 against gc.auto/256; a low threshold makes a
    # small fixture cross it the way a real backlog does.
    gc = list(retention._GC)
    gc[gc.index("gc.auto=6700")] = "gc.auto=1"
    monkeypatch.setattr(retention, "_GC", tuple(gc))


def test_invocations_and_harness_git_never_auto_maintain():
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    environment = broker._command_environment()
    assert {k: environment[k] for k in GIT_AUTO_MAINTENANCE_OFF} == GIT_AUTO_MAINTENANCE_OFF
    argv = hardened_git_argv("status")
    assert "gc.auto=0" in argv and "maintenance.auto=false" in argv
    assert argv.index("gc.auto=0") < argv.index("status")


def test_retention_packs_agent_and_controller_repositories(tmp_path, low_threshold):
    checkout = tmp_path / "checkout"
    _git("init", "-q", str(checkout))
    store = tmp_path / "store.git"
    _git("init", "-q", "--bare", str(store))
    _loose(checkout / ".git")
    _loose(store)

    maintain_git(UntrustedExecutionBroker(UntrustedExecutionConfig()), [checkout], [store])

    for git_dir in (checkout / ".git", store):
        counts = _counts(git_dir)
        assert counts["count"] == "0", counts
        assert int(counts["in-pack"]) >= 600


def test_a_stale_gc_lock_is_cleared_and_a_fresh_one_respected(tmp_path, low_threshold, caplog):
    stale, fresh = tmp_path / "stale.git", tmp_path / "fresh.git"
    for git_dir in (stale, fresh):
        _git("init", "-q", "--bare", str(git_dir))
        _loose(git_dir)
        (git_dir / "gc.pid.lock").write_text("")
    old = time.time() - (retention._STALE_LOCK_MINUTES + 5) * 60
    os.utime(stale / "gc.pid.lock", (old, old))

    maintain_git(UntrustedExecutionBroker(UntrustedExecutionConfig()), [], [stale, fresh])

    assert not (stale / "gc.pid.lock").exists()
    assert _counts(stale)["count"] == "0"
    # A recent lock may belong to a gc still running: never taken over.
    assert (fresh / "gc.pid.lock").exists()
    assert _counts(fresh)["count"] != "0"
    assert "Git maintenance deferred" in caplog.text


def test_an_agent_repository_stale_lock_is_cleared_as_the_execution_identity(tmp_path, low_threshold):
    checkout = tmp_path / "checkout"
    _git("init", "-q", str(checkout))
    _loose(checkout / ".git")
    lock = checkout / ".git" / "gc.pid.lock"
    lock.write_text("")
    old = time.time() - (retention._STALE_LOCK_MINUTES + 5) * 60
    os.utime(lock, (old, old))

    maintain_git(UntrustedExecutionBroker(UntrustedExecutionConfig()), [checkout], [])

    assert not lock.exists()
    assert _counts(checkout / ".git")["count"] == "0"
