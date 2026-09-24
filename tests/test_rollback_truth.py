"""Rollback success proves the previous runnable release or stopped absence."""
from pathlib import Path

import pytest

from test_deploy import _commit, _transport
from test_deploy import git_repo as git_repo
from steward_harness.config.schema import CommandSpec
from steward_harness.deploy.config import SystemdReleaseConfig
from steward_harness.deploy.engine import DeployEngine
from steward_harness.deploy.health import HealthGate
from steward_harness.deploy.systemd import ServiceError, SystemdController


class Services:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.operations = []

    def restart(self, service):
        self.operations.append(("restart", service))
        if self.failure == "restart":
            raise ServiceError("previous process could not start")

    def stop(self, service):
        self.operations.append(("stop", service))
        if self.failure == "stop":
            raise ServiceError("process did not stop")


def setup_rollback(tmp_path, git_repo, monkeypatch, *, previous_present=True, failure=None, already_restored=False):
    previous = _commit(git_repo, "previous") if previous_present else None
    desired = _commit(git_repo, "desired")
    config = SystemdReleaseConfig(
        release_root=str(tmp_path / "releases"), current_symlink=str(tmp_path / "current"),
        service="app.service", health_url="http://unused.invalid/health", timeout_seconds=10,
    )
    transport = _transport(git_repo)
    services = Services(failure=failure)
    health_checks = []

    def health(_gate, expected_sha=None, **_kwargs):
        health_checks.append(expected_sha)
        return expected_sha == previous and failure != "health", "live process SHA mismatch" if failure == "health" else "healthy previous SHA"

    monkeypatch.setattr(HealthGate, "wait_healthy", health)
    engine = DeployEngine("repo", config, transport, services)
    for sha in (previous, desired):
        if sha is not None:
            engine.releases.stage(sha)
    engine.releases.activate(desired)
    if previous is not None:
        # The release that actually served traffic before this one. Staging and
        # activating it by hand is not that fact, and rollback reads the fact:
        # after a crash between activation and health the pointer already names
        # the release being escaped, and the ref is the only thing that does not.
        transport.set_deployed(previous)
    if already_restored:
        if previous is None:
            engine.releases.deactivate()
        else:
            engine.releases.activate(previous)
    return engine, services, previous, desired, health_checks


@pytest.mark.parametrize("previous_present,failure", [(True, "restart"), (True, "health"), (False, "stop")])
@pytest.mark.parametrize("already_restored", [False, True])
def test_matching_pointer_cannot_claim_failed_service_rollback_converged(tmp_path, git_repo, monkeypatch, previous_present, failure, already_restored):
    """A restored pointer is not a converged rollback if the service is not up.

    This used to be read off the deployment row's terminal status. The row is
    gone; the same fact is the `RollbackResult` the call returns.
    """
    engine, services, previous, desired, health = setup_rollback(
        tmp_path, git_repo, monkeypatch, previous_present=previous_present,
        failure=failure, already_restored=already_restored,
    )
    result = engine.rollback(desired, previous)
    assert not result.success
    assert result.active_sha == previous
    assert "rollback converged" not in result.details
    assert engine.releases.current_release() == previous
    assert services.operations == [("restart" if previous_present else "stop", "app.service")]
    assert health == ([previous] if failure == "health" else [])


@pytest.mark.parametrize("failure", [None, "stop"])
def test_rollback_to_absence_distinguishes_stopped_service_from_stop_failure(tmp_path, git_repo, monkeypatch, failure):
    engine, services, _, desired, health = setup_rollback(
        tmp_path, git_repo, monkeypatch, previous_present=False, failure=failure,
    )
    result = engine.rollback(desired, None)
    assert result.success is (failure is None)
    assert result.active_sha is None
    assert engine.releases.current_release() is None
    assert services.operations == [("stop", "app.service")]
    assert not health
    assert ("rollback converged to no release" in result.details) is (failure is None)


def test_rollback_verifies_already_restored_artifact_before_starting_it(tmp_path, git_repo, monkeypatch):
    engine, services, previous, desired, health = setup_rollback(
        tmp_path, git_repo, monkeypatch, already_restored=True,
    )
    (Path(engine.deploy_config.current_symlink) / "app.txt").write_text("tampered")
    result = engine.rollback(desired, previous)
    assert not result.success
    assert result.active_sha == previous
    assert "verification" in result.details
    assert not services.operations and not health


def test_successful_rollback_records_intent_before_restarting_and_checks_exact_previous_sha(tmp_path, git_repo, monkeypatch):
    """The failure is durable before anything else moves.

    `before_rollback` and `rollback_started` were the hook and the column that
    carried this. It is one directory entry beside `releases/<sha>/` now, and
    the ordering it owes is the same one: written before the pointer moves and
    before the service is touched, so a rollback that dies halfway still leaves
    the release condemned while the next pass can finish recovery.
    """
    engine, services, previous, desired, health = setup_rollback(
        tmp_path, git_repo, monkeypatch, already_restored=True,
    )
    condemned_before_the_service_moved = []
    restart = services.restart

    def record_then_restart(service):
        condemned_before_the_service_moved.append(engine.releases.failed(desired))
        restart(service)

    services.restart = record_then_restart
    assert not engine.releases.failed(desired)

    result = engine.rollback(desired, previous)

    assert result.success and result.active_sha == previous
    assert health == [previous]
    assert condemned_before_the_service_moved == [True]
    assert engine.releases.failed(desired)
    assert not engine.releases.failed(previous)


@pytest.mark.parametrize("previous_present", [False, True])
@pytest.mark.parametrize("already_restored", [False, True])
def test_failed_rollback_intent_prevents_pointer_and_service_mutation(
    tmp_path, git_repo, monkeypatch, previous_present, already_restored,
):
    """An unrecordable failure stops the rollback rather than proceeding silently.

    If the marker cannot be written, a rollback that went ahead would leave a
    release that broke and no record that it did — and the next pass would
    redeploy it, which is the flapping correction 3 exists to stop.
    """
    engine, services, previous, desired, health = setup_rollback(
        tmp_path, git_repo, monkeypatch,
        previous_present=previous_present, already_restored=already_restored,
    )
    current = engine.releases.current_release()

    def refuse_to_condemn(_sha: str) -> None:
        raise RuntimeError("release could not be condemned")

    monkeypatch.setattr(engine.releases, "mark_failed", refuse_to_condemn)
    with pytest.raises(RuntimeError, match="release could not be condemned"):
        engine.rollback(desired, previous)

    assert engine.releases.current_release() == current
    assert not services.operations and not health


@pytest.mark.parametrize("already_restored", [False, True])
def test_rollback_verifies_previous_receipt_under_changed_build_policy(tmp_path, git_repo, monkeypatch, already_restored):
    engine, services, previous, desired, health = setup_rollback(
        tmp_path, git_repo, monkeypatch, already_restored=already_restored,
    )
    # The previous release was accepted without a build. A later deployment's
    # build policy must not invalidate that intact, previously accepted artifact.
    config = engine.deploy_config.model_copy(update={
        "artifact_build": CommandSpec(argv=("must-not-run-during-rollback",)),
    })
    new_engine = DeployEngine("repo", config, _transport(git_repo), services)
    result = new_engine.rollback(desired, previous)
    assert result.success and result.active_sha == previous
    assert health == [previous]
    assert services.operations == [("restart", "app.service")]


def test_forward_failure_includes_failed_rollback_to_the_observed_previous_release(tmp_path, git_repo, monkeypatch):
    """The rollback target is the last release known to have served traffic.

    Its old name was "even when the previous pointer was restored", which named
    the gap between an inspection and a later `deploy(previous_sha=...)`. There
    is no gap to name any more — and correction 6 says the answer is not
    `readlink(current)` either, because a crash can already have moved that
    onto the release being escaped. It is `refs/steward/deployed/<branch>`,
    which only a passing health gate writes.
    """
    engine, _, previous, desired, _ = setup_rollback(
        tmp_path, git_repo, monkeypatch, failure="health", already_restored=True,
    )
    result = engine.deploy(desired)
    assert not result.success
    assert result.rolled_back_to is None
    assert "rollback failed" in result.details
    assert engine.releases.current_release() == previous


def test_systemd_stop_waits_for_the_service_operation_and_reports_failure(monkeypatch):
    import subprocess
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 1, "", "service still running")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ServiceError, match="systemctl stop app.service failed"):
        SystemdController().stop("app.service")
    assert calls[0][0] == ["systemctl", "stop", "app.service"]
    assert calls[0][1]["timeout"] is None  # systemd owns the job deadline


@pytest.mark.parametrize("published", [False, True])
def test_condemned_recovery_preserves_newer_active_release(tmp_path, git_repo, monkeypatch, published):
    engine, services, previous, desired, health = setup_rollback(tmp_path, git_repo, monkeypatch)
    engine.releases.mark_failed(desired)
    newer = _commit(git_repo, "newer")
    engine.releases.transport.fetch()
    engine.releases.stage(newer)
    engine.releases.activate(newer)
    if published:
        engine.releases.transport.set_deployed(newer)
        monkeypatch.setattr(HealthGate, "wait_healthy", lambda *a, **kw: (True, "newer healthy"))
    result = engine.deploy(desired)
    assert not result.success
    assert engine.releases.current_release() == newer
    assert not services.operations
    assert engine.releases.failed(desired)


def test_condemned_recovery_retains_pointer_lease(tmp_path, git_repo, monkeypatch):
    engine, services, previous, desired, health = setup_rollback(tmp_path, git_repo, monkeypatch)
    engine.releases.mark_failed(desired)
    competing = DeployEngine("repo", engine.deploy_config, _transport(git_repo), services)
    with competing._lease:
        result = engine.deploy(desired)
    assert not result.success and "already in progress" in result.details
    assert engine.releases.current_release() == desired
    assert not services.operations and not health


@pytest.mark.parametrize("failure", ["restart", "health", "tamper"])
def test_condemned_recovery_does_not_claim_unverified_success(tmp_path, git_repo, monkeypatch, failure):
    engine, services, previous, desired, health = setup_rollback(
        tmp_path, git_repo, monkeypatch, already_restored=True, failure=failure,
    )
    engine.releases.mark_failed(desired)
    if failure == "restart":
        monkeypatch.setattr(HealthGate, "wait_healthy", lambda *a, **kw: (False, "service down"))
    if failure == "tamper":
        (Path(engine.deploy_config.current_symlink) / "app.txt").write_text("tampered")
    result = engine.deploy(desired)
    assert not result.success and result.rolled_back_to is None
    assert "rollback converged" not in result.details
    assert engine.releases.failed(desired)
    if failure == "tamper":
        assert not services.operations


def test_condemned_last_healthy_release_cannot_be_its_own_recovery_target(tmp_path, git_repo, monkeypatch):
    engine, services, previous, desired, health = setup_rollback(tmp_path, git_repo, monkeypatch)
    engine.releases.transport.set_deployed(desired)
    engine.releases.mark_failed(desired)
    result = engine.deploy(desired)
    assert not result.success and "no safe rollback target" in result.details
    assert not services.operations and not health
    assert engine.releases.failed(desired)
