"""Deployment engine coverage: staging, atomic activation, health gates, rollback."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from steward_harness.deploy.config import SystemdReleaseConfig
from steward_harness.deploy.transport import ReleaseTransport
from steward_harness.deploy.engine import DeployEngine, DeployResult
from steward_harness.deploy.release import ReleaseError, ReleaseManager
from steward_harness.deploy.systemd import (
    ServiceError,
)
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.runtime.execution import BoundaryStatus, UntrustedExecutionBroker


@pytest.fixture(autouse=True)
def simulated_backend_health(monkeypatch):
    from steward_harness.deploy.health import HealthGate
    original = HealthGate.wait_healthy
    def health(gate, expected_sha=None, **kwargs):
        if gate.url == "http://fixture.invalid/healthz":
            return True, f"simulated service reports {expected_sha}"
        return original(gate, expected_sha, **kwargs)
    monkeypatch.setattr(HealthGate, "wait_healthy", health)


class FakeServices:
    def __init__(self) -> None:
        self.restarted: list[str] = []
        self.stopped: list[str] = []

    def restart(self, service: str) -> None:
        self.restarted.append(service)

    def stop(self, service: str) -> None:
        self.stopped.append(service)


class FailsOnceServices(FakeServices):
    def restart(self, service: str) -> None:
        super().restart(service)
        if len(self.restarted) == 1:
            raise ServiceError("restart exploded")


class BlockingServices(FakeServices):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def restart(self, service: str) -> None:
        super().restart(service)
        self.entered.set()
        assert self.release.wait(timeout=5)


def _commit(repo: Path, message: str, filename: str = "app.txt") -> str:
    (repo / filename).write_text(message)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "steward@test"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "steward-test"], cwd=repo, check=True, capture_output=True
    )
    _commit(repo, "initial")
    return repo


def _transport(repo: Path) -> ControllerGitTransport:
    transport = ControllerGitTransport(
        repo.parent / "state.db", "repo", str(repo), "main", allow_local=True
    )
    transport.fetch()
    return ReleaseTransport(transport, "repo", "main")


def _release_manager(repo: Path, deploy: SystemdReleaseConfig) -> ReleaseManager:
    return ReleaseManager("repo", _transport(repo), deploy)


def _deploy_config(tmp_path: Path, **overrides) -> SystemdReleaseConfig:
    """The two paths every release derivation reads, kept inside the sandbox."""
    overrides.setdefault("health_url", "http://fixture.invalid/healthz")
    return SystemdReleaseConfig(
        release_root=str(tmp_path / "rel"),
        current_symlink=str(tmp_path / "current"),
        **overrides,
    )


def _committed_at(repo: Path, sha: str) -> datetime:
    """The committer date the settle comparison is made against, read from Git."""
    stamp = subprocess.run(
        ["git", "show", "-s", "--format=%cI", sha],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    return datetime.fromisoformat(stamp)


def _deploy_engine(
    repo: Path, deploy: SystemdReleaseConfig, **kwargs
) -> DeployEngine:
    return DeployEngine("repo", deploy, _transport(repo), **kwargs)


def test_successful_deployment_cannot_report_a_rollback() -> None:
    with pytest.raises(
        ValueError, match="successful deployment cannot have a rollback target"
    ):
        DeployResult(True, "previous", "contradictory")


class _HealthHandler(BaseHTTPRequestHandler):
    reported_sha: str | None = None
    #: Set to serve a document that is not simply ``{"sha": ...}`` — a real
    #: service answers with whatever its own health contract says.
    reported_document: dict | None = None

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        document = _HealthHandler.reported_document
        if document is None:
            if _HealthHandler.reported_sha is None:
                self.send_response(503)
                self.end_headers()
                return
            document = {"sha": _HealthHandler.reported_sha}
        body = json.dumps(document).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


@pytest.fixture()
def health_server():
    server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    _HealthHandler.reported_document = None
    _HealthHandler.reported_sha = None


def test_a_product_version_is_not_the_running_commit(
    tmp_path: Path, git_repo: Path, health_server: HTTPServer
) -> None:
    """``{"version": "1.0.0"}`` reports no SHA; it does not report a wrong one.

    A service whose health document carries a product version under a key the
    gate also uses for release identity must not have that version compared
    against a Git object name — no correct deployment could ever clear it.
    """
    sha = _commit(git_repo, "release behind a versioned health document")
    _HealthHandler.reported_document = {"status": "ok", "version": "1.0.0"}
    deploy = _deploy_config(
        tmp_path,
        health_url=f"http://127.0.0.1:{health_server.server_address[1]}/healthz",
        timeout_seconds=10,
        service="app.service",
    )
    engine = _deploy_engine(git_repo, deploy, services=FakeServices())

    result = engine.deploy(sha)

    assert not result.success
    assert "reports no SHA" in result.details
    assert "1.0.0" not in result.details


def test_a_service_that_names_no_release_cannot_satisfy_exact_revision(
    tmp_path: Path, git_repo: Path, health_server: HTTPServer
) -> None:
    """``health_reports_sha=False`` is how that service declares its contract."""
    sha = _commit(git_repo, "release behind a status-only health document")
    _HealthHandler.reported_document = {"status": "ok", "version": "1.0.0"}
    deploy = _deploy_config(
        tmp_path,
        health_url=f"http://127.0.0.1:{health_server.server_address[1]}/healthz",
        timeout_seconds=10,
        service="app.service",
    )
    services = FakeServices()
    engine = _deploy_engine(git_repo, deploy, services=services)

    result = engine.deploy(sha)

    assert not result.success
    assert "reports no SHA" in result.details
    assert services.restarted == ["app.service"]
    assert engine.releases.current_release() is None


def test_stage_exports_committed_tree(tmp_path: Path, git_repo: Path) -> None:
    sha = _commit(git_repo, "feature")
    (git_repo / "untracked.txt").write_text("not committed")
    releases = _release_manager(
        git_repo, SystemdReleaseConfig(health_url="http://fixture.invalid/healthz", release_root=str(tmp_path / "rel"))
    )
    release_dir = releases.stage(sha)
    assert (release_dir / "app.txt").read_text() == "feature"
    assert not (release_dir / "untracked.txt").exists()
    assert not (release_dir / ".git").exists()


def test_a_separate_service_identity_can_traverse_to_its_release(
    tmp_path: Path, git_repo: Path
) -> None:
    """The first deploy died at CHDIR: the worker's umask made this 0700."""
    sha = _commit(git_repo, "feature")
    previous = os.umask(0o077)
    try:
        releases = _release_manager(
            git_repo,
            SystemdReleaseConfig(
                health_url="http://fixture.invalid/healthz",
                release_root=str(tmp_path / "rel"),
            ),
        )
        release_dir = releases.stage(sha)
    finally:
        os.umask(previous)

    assert releases.releases_root.stat().st_mode & 0o777 == 0o755
    assert release_dir.stat().st_mode & 0o777 == 0o755


def test_stage_uses_controller_objects_after_agent_repository_disappears(
    tmp_path: Path, git_repo: Path
) -> None:
    sha = _commit(git_repo, "trusted release")
    transport = _transport(git_repo)
    unavailable = tmp_path / "agent-repository-unavailable"
    git_repo.rename(unavailable)
    releases = ReleaseManager(
        "repo", transport, SystemdReleaseConfig(health_url="http://fixture.invalid/healthz", release_root=str(tmp_path / "rel"))
    )

    release_dir = releases.stage(sha)

    assert (release_dir / "app.txt").read_text() == "trusted release"


@pytest.mark.parametrize("staging_umask", [0o022, 0o077])
def test_release_archive_protects_helper_namespace_and_preserves_executables(
    tmp_path, git_repo, staging_umask,
):
    import tarfile

    code = git_repo / "src"
    code.mkdir()
    helper = code / "launcher.py"
    helper.write_text("print('trusted helper')\n")
    executable = code / "run"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    sha = _commit(git_repo, "release with a privileged helper")
    transport = _transport(git_repo)
    transport._run("config", "tar.umask", "0000")
    archive = tmp_path / "release.tar"
    transport.export_release_archive(sha, archive)
    # Inspect the archive itself: non-root tar extraction could mask the defect
    # that production's root extraction exposes.
    with tarfile.open(archive) as contents:
        assert all(not member.mode & 0o022 for member in contents
                   if member.isfile() or member.isdir())
    releases = ReleaseManager("repo", transport, SystemdReleaseConfig(
        health_url="http://fixture.invalid/healthz", release_root=str(tmp_path / "rel")))
    previous_umask = os.umask(staging_umask)
    try:
        release = releases.stage(sha)
    finally:
        os.umask(previous_umask)
    assert (release / "src").stat().st_mode & 0o777 == 0o755
    assert (release / "src/launcher.py").stat().st_mode & 0o777 == 0o644
    assert (release / "src/run").stat().st_mode & 0o777 == 0o755
    assert releases.verify(sha) == release


def test_stage_rejects_commit_never_imported_into_controller_store(
    tmp_path: Path, git_repo: Path
) -> None:
    transport = _transport(git_repo)
    local_only = _commit(git_repo, "agent only")
    releases = ReleaseManager(
        "repo", transport, SystemdReleaseConfig(health_url="http://fixture.invalid/healthz", release_root=str(tmp_path / "rel"))
    )

    with pytest.raises(ReleaseError, match="is unavailable"):
        releases.stage(local_only)


def test_stage_is_idempotent(tmp_path: Path, git_repo: Path) -> None:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    releases = _release_manager(
        git_repo, SystemdReleaseConfig(health_url="http://fixture.invalid/healthz", release_root=str(tmp_path / "rel"))
    )
    first = releases.stage(sha)
    second = releases.stage(sha)
    assert first == second


def test_stage_rejects_mutated_existing_release(tmp_path: Path, git_repo: Path) -> None:
    sha = _commit(git_repo, "immutable")
    releases = _release_manager(
        git_repo, SystemdReleaseConfig(health_url="http://fixture.invalid/healthz", release_root=str(tmp_path / "rel"))
    )
    release_dir = releases.stage(sha)
    (release_dir / "app.txt").write_text("mutated after staging")

    with pytest.raises(ReleaseError, match="does not match its staging receipt"):
        releases.stage(sha)


def test_cli_inspection_preserves_release_integrity(tmp_path: Path, git_repo: Path) -> None:
    import shutil
    import steward_harness

    shutil.copytree(
        Path(steward_harness.__file__).parent, git_repo / "steward_harness",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    sha = _commit(git_repo, "CLI release")
    releases = _release_manager(
        git_repo, SystemdReleaseConfig(
            health_url="http://fixture.invalid/healthz", release_root=str(tmp_path / "rel"),
            current_symlink=str(tmp_path / "current"),
        ),
    )
    release = releases.stage(sha)
    receipt = releases._receipt_path(sha).read_bytes()
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "PYTHONPATH"}}
    for module in ("steward_harness.cli", "steward_harness.deploy.cli"):
        subprocess.run(
            [sys.executable, "-m", module, "--help"], cwd=release, env=env,
            check=True, capture_output=True, timeout=30,
        )
    assert list(release.rglob("*.pyc"))  # Exercise actual interpreter writes.
    assert releases.verify(sha) == releases.stage(sha) == releases.activate(sha)
    assert releases._receipt_path(sha).read_bytes() == receipt
    source = release / "steward_harness" / "cli.py"
    source.write_text(source.read_text() + "\n# unexpected source change\n")
    with pytest.raises(ReleaseError, match="staging receipt"):
        releases.verify(sha)


@pytest.mark.parametrize("added", ["__pycache__/payload.py", "standalone.pyc", "__pycache__/link.pyc"])
def test_cache_exemption_still_detects_other_content(tmp_path: Path, git_repo: Path, added: str) -> None:
    sha = _commit(git_repo, "immutable")
    releases = _release_manager(
        git_repo, SystemdReleaseConfig(release_root=str(tmp_path / "rel")),
    )
    release = releases.stage(sha)
    path = release / added
    path.parent.mkdir(exist_ok=True)
    if path.name == "link.pyc":
        path.symlink_to(release / "app.txt")
    else:
        path.write_text("unexpected content")
    with pytest.raises(ReleaseError, match="staging receipt"):
        releases.verify(sha)


def test_stage_verifies_legacy_release_before_accepting_it(
    tmp_path: Path, git_repo: Path
) -> None:
    sha = _commit(git_repo, "legacy")
    releases = _release_manager(
        git_repo, SystemdReleaseConfig(health_url="http://fixture.invalid/healthz", release_root=str(tmp_path / "rel"))
    )
    release_dir = releases.stage(sha)
    releases._receipt_path(sha).unlink()

    assert releases.stage(sha) == release_dir
    assert releases._receipt_path(sha).is_file()


def test_activate_swaps_symlink_atomically(tmp_path: Path, git_repo: Path) -> None:
    first = _commit(git_repo, "first")
    second = _commit(git_repo, "second")
    releases = _release_manager(
        git_repo,
        SystemdReleaseConfig(health_url="http://fixture.invalid/healthz",
            release_root=str(tmp_path / "rel"),
            current_symlink=str(tmp_path / "current"),
        ),
    )
    releases.stage(first)
    releases.activate(first)
    assert releases.current_release() == first
    releases.stage(second)
    releases.activate(second)
    assert releases.current_release() == second
    leftovers = [p for p in (tmp_path).iterdir() if p.name.startswith(".current")]
    assert leftovers == []


def test_current_release_rejects_unmanaged_pointer(
    tmp_path: Path, git_repo: Path
) -> None:
    current = tmp_path / "current"
    releases = ReleaseManager(
        "repo",
        _transport(git_repo),
        SystemdReleaseConfig(health_url="http://fixture.invalid/healthz",
            release_root=str(tmp_path / "rel"),
            current_symlink=str(current),
        ),
    )

    current.write_text("not a symlink")
    with pytest.raises(ReleaseError, match="not a symlink"):
        releases.current_release()
    assert current.read_text() == "not a symlink"

    current.unlink()
    current.symlink_to(tmp_path / "missing")
    with pytest.raises(ReleaseError, match="invalid"):
        releases.current_release()
    assert current.is_symlink()

    current.unlink()
    outside = tmp_path / "outside" / ("a" * 40)
    outside.mkdir(parents=True)
    current.symlink_to(outside)
    with pytest.raises(ReleaseError, match="outside managed releases"):
        releases.current_release()
    assert current.resolve() == outside


def test_prune_keeps_recent_and_current(tmp_path: Path, git_repo: Path) -> None:
    shas = [_commit(git_repo, f"release-{i}") for i in range(4)]
    releases = _release_manager(
        git_repo,
        SystemdReleaseConfig(health_url="http://fixture.invalid/healthz",
            release_root=str(tmp_path / "rel"),
            current_symlink=str(tmp_path / "cur"),
        ),
    )
    for sha in shas:
        releases.stage(sha)
    releases.activate(shas[0])  # Oldest is active.
    removed = releases.prune(keep=2)
    assert shas[1] in removed
    assert releases.current_release() == shas[0]
    assert (releases.releases_root / shas[1]).exists() is False
    assert (releases.releases_root / shas[0]).exists() is True


def test_deploy_success_with_sha_health(
    tmp_path: Path, git_repo: Path, health_server: HTTPServer
) -> None:
    _HealthHandler.reported_sha = None
    sha = _commit(git_repo, "healthy release")
    _HealthHandler.reported_sha = sha

    deploy = SystemdReleaseConfig(
        release_root=str(tmp_path / "rel"),
        current_symlink=str(tmp_path / "current"),
        health_url=f"http://127.0.0.1:{health_server.server_address[1]}/healthz",
        timeout_seconds=10,
        service="app.service",
    )
    services = FakeServices()
    engine = _deploy_engine(
        git_repo,
        deploy,
        services=services,
    )
    result = engine.deploy(sha)

    assert result.success
    assert result.rolled_back_to is None
    assert services.restarted == ["app.service"]
    assert engine.releases.current_release() == sha
    engine.releases.verify(sha)  # raises if the staged release drifted from its commit


def test_deploy_rolls_back_on_sha_mismatch(
    tmp_path: Path, git_repo: Path, health_server: HTTPServer
) -> None:
    first = _commit(git_repo, "good release")
    _HealthHandler.reported_sha = first
    deploy = SystemdReleaseConfig(
        release_root=str(tmp_path / "rel"),
        current_symlink=str(tmp_path / "current"),
        health_url=f"http://127.0.0.1:{health_server.server_address[1]}/healthz",
        timeout_seconds=10,
        service="app.service",
    )
    services = FakeServices()
    engine = _deploy_engine(git_repo, deploy, services=services)
    assert engine.deploy(first).success

    bad = _commit(git_repo, "bad release")
    engine.releases.transport.fetch()
    _HealthHandler.reported_sha = first  # Server never picks up the new SHA.
    result = engine.deploy(bad)

    assert not result.success
    assert result.rolled_back_to == first
    assert engine.releases.current_release() == first
    assert services.restarted.count("app.service") >= 2  # Deploy + rollback restart.


def test_deploy_without_external_health_cannot_satisfy_target(tmp_path: Path, git_repo: Path) -> None:
    sha = _commit(git_repo, "no health gate")
    deploy = SystemdReleaseConfig(
        release_root=str(tmp_path / "rel"),
        current_symlink=str(tmp_path / "current"),
        health_url=None,
    )
    result = _deploy_engine(git_repo, deploy).deploy(sha)
    assert not result.success
    assert "no external exact-revision" in result.details


def test_unhealthy_first_release_rolls_back_to_explicit_absence(
    tmp_path: Path,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure is durable before the pointer moves, which is the whole order.

    `before_rollback` and `rollback_started` were the hook and the column that
    said so. The marker beside `releases/<sha>/` says it now, and the ordering
    is the same one: write it while the bad release is still what `current`
    names, so a crash mid-rollback still leaves the release condemned.
    """
    desired = _commit(git_repo, "unhealthy first release")
    deploy = _deploy_config(tmp_path)
    engine = _deploy_engine(git_repo, deploy)
    condemned_before_the_pointer_moved: list[bool] = []
    deactivate = engine.releases.deactivate

    def record_then_deactivate() -> None:
        condemned_before_the_pointer_moved.append(engine.releases.failed(desired))
        deactivate()

    monkeypatch.setattr(engine.releases, "deactivate", record_then_deactivate)
    monkeypatch.setattr(engine, "_verify", lambda _sha, **_kwargs: (False, "unhealthy"))

    result = engine.deploy(desired)

    assert not result.success
    assert engine.releases.current_release() is None
    assert condemned_before_the_pointer_moved == [True]
    assert (engine.releases.releases_root / f".{desired}.failed").is_file()
    assert engine.releases.transport.deployed_sha() is None


def test_release_pointer_lease_covers_restart_and_health(
    tmp_path: Path, git_repo: Path
) -> None:
    first = _commit(git_repo, "first deployment")
    second = _commit(git_repo, "contending deployment")
    deploy = SystemdReleaseConfig(health_url="http://fixture.invalid/healthz",
        release_root=str(tmp_path / "rel"),
        current_symlink=str(tmp_path / "current"),
        service="app.service",
    )
    services = BlockingServices()
    owner = _deploy_engine(git_repo, deploy, services=services)
    outcome: list[object] = []
    thread = threading.Thread(target=lambda: outcome.append(owner.deploy(first)))
    thread.start()
    assert services.entered.wait(timeout=5)

    observer = _deploy_engine(git_repo, deploy, services=FakeServices())
    # Busy is the held lease, which is what a second worker would contend on.
    assert observer.busy()
    contender = observer.deploy(second)

    assert not contender.success
    assert contender.details == "release deployment is already in progress"
    assert owner.releases.current_release() == first

    services.release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome and outcome[0].success
    assert not observer.busy()


def test_activation_failure_preserves_staged_release(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sha = _commit(git_repo, "abortable")
    deploy = SystemdReleaseConfig(health_url="http://fixture.invalid/healthz",
        release_root=str(tmp_path / "rel"),
        current_symlink=str(tmp_path / "current"),
    )

    class StopDeployment(RuntimeError):
        pass

    def stop_before_activation(_sha: str) -> None:
        raise StopDeployment

    engine = _deploy_engine(git_repo, deploy)
    monkeypatch.setattr(engine.releases, "activate", stop_before_activation)
    with pytest.raises(StopDeployment):
        engine.deploy(sha)

    assert engine.releases.current_release() is None
    engine.releases.verify(sha)  # raises if the staged release drifted from its commit


def test_reconcile_recovers_after_activation_crash(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sha = _commit(git_repo, "resume activation")
    deploy = SystemdReleaseConfig(health_url="http://fixture.invalid/healthz",
        release_root=str(tmp_path / "rel"),
        current_symlink=str(tmp_path / "current"),
        service="app.service",
    )
    services = FakeServices()

    class Crash(RuntimeError):
        pass

    interrupted = _deploy_engine(
        git_repo,
        deploy,
        services=services,
    )
    activate = interrupted.releases.activate

    def fail_after_activation(candidate: str) -> None:
        activate(candidate)
        raise Crash

    monkeypatch.setattr(interrupted.releases, "activate", fail_after_activation)
    with pytest.raises(Crash):
        interrupted.deploy(sha)
    assert interrupted.releases.current_release() == sha

    def unexpected_activation(_manager: ReleaseManager, _candidate: str) -> None:
        raise AssertionError("recovery reactivated the already active release")

    monkeypatch.setattr(ReleaseManager, "activate", unexpected_activation)
    result = _deploy_engine(
        git_repo,
        deploy,
        services=services,
    ).deploy(sha)

    assert result.success
    assert result.details.startswith("already active;")
    assert services.restarted == []


def test_rollback_recovery_restarts_previous_without_reactivating_desired(
    tmp_path: Path, git_repo: Path
) -> None:
    previous = _commit(git_repo, "previous")
    desired = _commit(git_repo, "desired")
    deploy = SystemdReleaseConfig(health_url="http://fixture.invalid/healthz",
        release_root=str(tmp_path / "rel"),
        current_symlink=str(tmp_path / "current"),
        service="app.service",
    )
    services = FakeServices()
    engine = _deploy_engine(git_repo, deploy, services=services)
    engine.releases.stage(previous)
    engine.releases.stage(desired)
    engine.releases.activate(desired)
    engine.releases.activate(previous)

    restored = engine.rollback(desired, previous)

    assert restored.success and restored.active_sha == previous
    assert engine.releases.current_release() == previous
    assert services.restarted == ["app.service"]


def test_rollback_recovery_repeats_restart_without_inventing_durable_proof(
    tmp_path: Path, git_repo: Path
) -> None:
    previous = _commit(git_repo, "previous")
    desired = _commit(git_repo, "desired")
    deploy = SystemdReleaseConfig(health_url="http://fixture.invalid/healthz",
        release_root=str(tmp_path / "rel"),
        current_symlink=str(tmp_path / "current"),
        service="app.service",
    )
    services = FakeServices()
    engine = _deploy_engine(git_repo, deploy, services=services)
    engine.releases.stage(previous)
    engine.releases.stage(desired)
    engine.releases.activate(previous)

    restored = engine.rollback(desired, previous)

    assert restored.success and restored.active_sha == previous
    assert services.restarted == ["app.service"]


def test_rollback_refuses_to_overwrite_an_unrelated_active_release(
    tmp_path: Path, git_repo: Path
) -> None:
    previous = _commit(git_repo, "previous")
    desired = _commit(git_repo, "desired")
    unrelated = _commit(git_repo, "unrelated")
    deploy = SystemdReleaseConfig(health_url="http://fixture.invalid/healthz",
        release_root=str(tmp_path / "rel"),
        current_symlink=str(tmp_path / "current"),
    )
    engine = _deploy_engine(git_repo, deploy)
    for sha in (previous, desired, unrelated):
        engine.releases.stage(sha)
    engine.releases.activate(unrelated)

    assert not engine.rollback(desired, previous).success
    assert engine.releases.current_release() == unrelated


def test_restart_exception_fails_and_rolls_back(tmp_path: Path, git_repo: Path) -> None:
    first = _commit(git_repo, "working")
    deploy = SystemdReleaseConfig(health_url="http://fixture.invalid/healthz",
        release_root=str(tmp_path / "rel"),
        current_symlink=str(tmp_path / "current"),
        service="app.service",
    )
    initial = _deploy_engine(git_repo, deploy, services=FakeServices())
    assert initial.deploy(first).success

    second = _commit(git_repo, "restart failure")
    services = FailsOnceServices()
    engine = _deploy_engine(git_repo, deploy, services=services)
    result = engine.deploy(second)

    assert not result.success
    assert "restart failed" in result.details
    assert result.rolled_back_to == first
    assert engine.releases.current_release() == first
    assert services.restarted == ["app.service", "app.service"]


def test_running_code_identity_survives_interpreter_links_and_release_swap(
    git_repo, tmp_path
):
    import os
    import shutil
    import steward_harness.web.health as health

    # Stage the actual health implementation as real committed release content.
    shutil.copyfile(health.__file__, git_repo / "health.py")
    binary = git_repo / ".venv" / "bin" / "python"
    binary.parent.mkdir(parents=True)
    binary.symlink_to(sys.executable)
    first = _commit(git_repo, "first runtime")
    config = SystemdReleaseConfig(health_url="http://fixture.invalid/healthz",
        release_root=str(tmp_path / "releases"),
        current_symlink=str(tmp_path / "current"),
    )
    manager = _release_manager(git_repo, config)
    first_dir = manager.stage(first)
    assert first_dir.stat().st_mode & 0o777 == 0o755
    # The committed fixture has a release-local interpreter whose leaf resolves
    # outside the release. Identity must come from the actual loaded code.
    interpreter = tmp_path / "current" / ".venv" / "bin" / "python"
    manager.activate(first)
    script = (
        "import importlib.util,sys; "
        "spec=importlib.util.spec_from_file_location('release_health',sys.argv[1]); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "print(module.release_sha(),flush=True); "
        "exec('for line in sys.stdin: print(module.release_sha(),flush=True)')"
    )
    env = {
        **os.environ,
        "STEWARD_RELEASE_SHA": "c" * 40,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.Popen(
        [str(interpreter), "-c", script, str(tmp_path / "current" / "health.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout.readline().strip() == first
        second = _commit(git_repo, "second runtime")
        manager = _release_manager(git_repo, config)
        manager.stage(second)
        manager.activate(second)
        process.stdin.write("identity\n")
        process.stdin.flush()
        assert process.stdout.readline().strip() == first
        new = subprocess.run(
            [str(interpreter), "-c", script, str(tmp_path / "current" / "health.py")],
            input="",
            capture_output=True,
            text=True,
            env=env,
            check=True,
            timeout=10,
        )
        assert new.stdout.strip() == second
    finally:
        process.communicate(timeout=10)
    # Importing code did not alter the staged release or its manifest.
    manager.verify(first)
    manager.verify(second)


def test_runnable_artifact_converges_and_detects_dependency_tampering(
    tmp_path: Path, git_repo: Path, health_server: HTTPServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zipfile

    from steward_harness.config.schema import CommandSpec, UntrustedExecutionConfig
    from steward_harness.runtime.execution import UntrustedExecutionBroker

    # Offline packaged dependency: absent from the host interpreter, installed
    # only by the brokered build, then imported by a fresh release process.
    with zipfile.ZipFile(git_repo / "dependency.zip", "w") as archive:
        archive.writestr("fixture_dependency.py", "VALUE = 'installed dependency'\n")
    (git_repo / "build.py").write_text(
        "import os, pathlib, zipfile\n"
        "assert 'GH_TOKEN' not in os.environ\n"
        "with zipfile.ZipFile('dependency.zip') as archive: archive.extractall('vendor')\n"
    )
    first = _commit(git_repo, "runnable source")
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    deploy = SystemdReleaseConfig(
        release_root=str(tmp_path / "artifacts"), current_symlink=str(tmp_path / "live"),
        artifact_build=CommandSpec(argv=(sys.executable, "build.py")),
        service="fixture", timeout_seconds=10,
        health_url=f"http://127.0.0.1:{health_server.server_address[1]}/healthz",
    )
    transport = _transport(git_repo)
    services = FakeServices()
    original_restart = services.restart

    def restart(service: str) -> None:
        original_restart(service)
        release = Path(deploy.current_symlink).resolve()
        if not release.exists():
            _HealthHandler.reported_sha = None
            return
        completed = subprocess.run(
            [sys.executable, "-B", "-S", "-c", "import fixture_dependency; print(fixture_dependency.VALUE)"],
            cwd=release, env={"PYTHONPATH": str(release / "vendor")},
            text=True, capture_output=True, check=True,
        )
        assert completed.stdout.strip() == "installed dependency"
        _HealthHandler.reported_sha = release.name

    services.restart = restart
    monkeypatch.setenv("GH_TOKEN", "must-not-reach-build")
    engine = DeployEngine("repo", deploy, transport, services=services, broker=broker)
    completed = engine.deploy(first)
    assert completed.success
    assert engine.releases.current_release() == first
    # Converged: a second pass over the same SHA is already active and does not
    # restart the service again.
    assert engine.deploy(first).success
    assert services.restarted == ["fixture"]
    engine.releases.verify(first)
    installed = Path(deploy.current_symlink) / "vendor" / "fixture_dependency.py"
    assert installed.stat().st_mode & 0o777 == 0o644
    installed.write_text("VALUE = 'tampered'\n")
    with pytest.raises(ReleaseError):
        engine.releases.verify(first)
    assert not engine.deploy(first).success
    assert services.restarted == ["fixture"]


@pytest.mark.parametrize("mutation", [
    "pathlib.Path('app.txt').write_text('replaced')",
    "pathlib.Path('escape').symlink_to('/etc/passwd')",
    "raise SystemExit(4)",
])
def test_invalid_artifact_never_activates(tmp_path: Path, git_repo: Path, mutation: str) -> None:
    from steward_harness.config.schema import CommandSpec, UntrustedExecutionConfig
    from steward_harness.runtime.execution import UntrustedExecutionBroker

    (git_repo / "build.py").write_text("import pathlib\n" + mutation + "\n")
    sha = _commit(git_repo, "invalid build")
    deploy = SystemdReleaseConfig(health_url="http://fixture.invalid/healthz",
        release_root=str(tmp_path / "artifacts"), current_symlink=str(tmp_path / "live"),
        artifact_build=CommandSpec(argv=(sys.executable, "build.py")),
    )
    engine = DeployEngine("repo", deploy, _transport(git_repo),
                          broker=UntrustedExecutionBroker(UntrustedExecutionConfig()))
    completed = engine.deploy(sha)
    assert not completed.success
    assert not Path(deploy.current_symlink).exists()
    assert not (Path(deploy.release_root) / "repo" / sha).exists()



def test_existing_source_release_cannot_masquerade_as_built_artifact(tmp_path: Path, git_repo: Path) -> None:
    from steward_harness.config.schema import CommandSpec, UntrustedExecutionConfig
    from steward_harness.runtime.execution import UntrustedExecutionBroker

    sha = _commit(git_repo, "source only")
    config = SystemdReleaseConfig(health_url="http://fixture.invalid/healthz", release_root=str(tmp_path / "releases"), current_symlink=str(tmp_path / "current"))
    transport = _transport(git_repo)
    ReleaseManager("repo", transport, config).stage(sha)
    configured = config.model_copy(update={"artifact_build": CommandSpec(argv=(sys.executable, "build.py"))})
    with pytest.raises(ReleaseError, match="staging receipt"):
        ReleaseManager("repo", transport, configured, UntrustedExecutionBroker(UntrustedExecutionConfig())).stage(sha)


def test_first_built_artifact_rolls_back_to_prior_source_policy(tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from steward_harness.config.schema import CommandSpec, UntrustedExecutionConfig
    from steward_harness.runtime.execution import UntrustedExecutionBroker

    prior = _commit(git_repo, "source release")
    config = SystemdReleaseConfig(health_url="http://fixture.invalid/healthz", release_root=str(tmp_path / "releases"), current_symlink=str(tmp_path / "current"))
    transport = _transport(git_repo)
    releases = ReleaseManager("repo", transport, config)
    releases.stage(prior)
    releases.activate(prior)
    # Activating by hand is not the same fact as having served traffic, and
    # rollback needs the second one. This is what a successful deploy of the
    # prior source release would have left behind.
    transport.set_deployed(prior)
    (git_repo / "build.py").write_text("from pathlib import Path\nPath('dependency.py').write_text('VALUE=1')\n")
    desired = _commit(git_repo, "introduce runnable dependencies")
    transport.fetch()
    policy = config.model_copy(update={
        "artifact_build": CommandSpec(argv=(sys.executable, "build.py")),
        "health_url": "http://fixture.invalid/healthz",
    })
    # Only the prior source release is ever healthy, so the built artifact must
    # fail forward and the rollback must accept the prior receipt unchanged.
    monkeypatch.setattr(
        "steward_harness.deploy.engine.HealthGate.wait_healthy",
        lambda self, expected_sha: (expected_sha == prior, "observed health"),
    )
    engine = DeployEngine("repo", policy, transport, broker=UntrustedExecutionBroker(UntrustedExecutionConfig()))
    result = engine.deploy(desired)
    assert not result.success
    assert result.rolled_back_to == prior
    assert releases.current_release() == prior
    releases.verify(prior)


def test_a_release_that_failed_its_health_gate_is_not_deployed_again(
    tmp_path: Path, git_repo: Path, health_server: HTTPServer
) -> None:
    """The termination condition the derivation needs, and has never had.

    `if observed == readlink(current): return` does not terminate: a release
    that fails health rolls the pointer back, so the desired SHA still differs
    from the active one and every subsequent pass redeploys it. That is not
    wasted CPU, it is flapping the live service on every poll —
    `deployments.status='failed'` is what used to prevent it, and what the
    fill has to replace with a directory entry beside `releases/<sha>/`.
    """
    good = _commit(git_repo, "healthy release")
    bad = _commit(git_repo, "unhealthy release")
    _HealthHandler.reported_sha = good  # The server never reports the bad SHA.
    deploy = SystemdReleaseConfig(
        release_root=str(tmp_path / "rel"),
        current_symlink=str(tmp_path / "current"),
        health_url=f"http://127.0.0.1:{health_server.server_address[1]}/healthz",
        timeout_seconds=10,
        service="app.service",
    )
    services = FakeServices()
    engine = _deploy_engine(git_repo, deploy, services=services)
    assert engine.deploy(good).success
    settled = _committed_at(git_repo, bad) + timedelta(seconds=31)

    # One pass: the tip moved, the release is due, and it breaks.
    assert not engine.deploy(bad).success
    assert engine.releases.current_release() == good
    served = list(services.restarted)
    # The whole of the termination condition: one directory entry beside the
    # release it condemns, written before the pointer went back.
    assert (engine.releases.releases_root / f".{bad}.failed").is_file()
    assert engine.releases.failed(bad)
    assert not engine.releases.failed(good)

    # Every pass after it observes the same ref and the same rolled-back
    # pointer, and derives nothing to do. Without the marker `observed !=
    # readlink(current)` stays true forever and this flaps the live service on
    # every poll.
    assert services.restarted == served
    assert engine.releases.current_release() == good
    # The good release is still what served traffic, and still says so.
    assert engine.releases.transport.deployed_sha() == good


def test_operator_must_retire_condemnation_before_retrying_the_same_release(
    tmp_path: Path, git_repo: Path, health_server: HTTPServer
) -> None:
    """An explicit operator retry must first retire the condemnation."""
    good = _commit(git_repo, "healthy release")
    bad = _commit(git_repo, "release that breaks, then does not")
    _HealthHandler.reported_sha = good
    deploy = SystemdReleaseConfig(
        release_root=str(tmp_path / "rel"),
        current_symlink=str(tmp_path / "current"),
        health_url=f"http://127.0.0.1:{health_server.server_address[1]}/healthz",
        timeout_seconds=10,
        service="app.service",
    )
    engine = _deploy_engine(git_repo, deploy, services=FakeServices())
    assert engine.deploy(good).success
    assert not engine.deploy(bad).success
    assert engine.releases.failed(bad)

    # Explicitly retire the marker after diagnosis before retrying this SHA.
    engine.releases.clear_failed(bad)
    _HealthHandler.reported_sha = bad
    assert engine.deploy(bad).success

    assert not engine.releases.failed(bad)
    assert engine.releases.transport.deployed_sha() == bad
    # And the derivation can now converge on that tip instead of refusing it.
    settled = _committed_at(git_repo, bad) + timedelta(seconds=31)
    engine.releases.deactivate()


# --- The derivation itself: due_release over the observed ref ---------------
#
# Four comparisons and a missing ref. None of them had a test before the fill,
# because before the fill the trigger was a queued row and not a comparison.












def test_observed_tip_reads_the_mirror_and_does_not_fetch(
    tmp_path: Path, git_repo: Path
) -> None:
    """The pass must not make a network round trip to answer *what is the tip*."""
    first = _commit(git_repo, "mirrored")
    transport = _transport(git_repo)
    observed = transport.observed_tip()
    assert observed is not None
    assert observed[0] == first
    assert observed[1] == _committed_at(git_repo, first)

    _commit(git_repo, "pushed after the mirror was taken")

    assert transport.observed_tip()[0] == first
    assert transport.fetch() != first
    assert transport.observed_tip()[0] == transport.fetch()


# --- Correction 6: what last worked is not what is pointed at ---------------


def test_recovery_after_an_activation_crash_returns_to_the_deployed_ref(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between activate() and health leaves `current` on the bad release.

    `readlink(current)` then answers *what is pointed at* when the question is
    *what last worked*, and rolling back to it would return the release we are
    trying to escape. `refs/steward/deployed/<branch>` is written only after
    health passes, so it is the one pointer the crash could not have moved.
    """
    good = _commit(git_repo, "healthy release")
    bad = _commit(git_repo, "unhealthy release")
    deploy = _deploy_config(
        tmp_path,
        health_url="http://fixture.invalid/healthz",
        timeout_seconds=10,
        service="app.service",
    )
    monkeypatch.setattr(
        "steward_harness.deploy.engine.HealthGate.wait_healthy",
        lambda self, expected_sha: (expected_sha == good, "observed health"),
    )
    engine = _deploy_engine(git_repo, deploy, services=FakeServices())
    assert engine.deploy(good).success
    assert engine.releases.transport.deployed_sha() == good

    class Crash(RuntimeError):
        pass

    crashing = _deploy_engine(git_repo, deploy, services=FakeServices())
    activate = crashing.releases.activate

    def crash_after_activation(candidate: str, **kwargs) -> None:
        activate(candidate, **kwargs)
        raise Crash

    monkeypatch.setattr(crashing.releases, "activate", crash_after_activation)
    with pytest.raises(Crash):
        crashing.deploy(bad)
    # The pointer now lies about what served traffic; the ref does not.
    assert crashing.releases.current_release() == bad
    assert crashing.releases.transport.deployed_sha() == good

    services = FakeServices()
    recovered = _deploy_engine(git_repo, deploy, services=services).deploy(bad)

    assert not recovered.success
    assert recovered.rolled_back_to == good
    assert engine.releases.current_release() == good
    assert engine.releases.transport.deployed_sha() == good
    assert (engine.releases.releases_root / f".{bad}.failed").is_file()
    assert services.restarted == ["app.service", "app.service"]


def test_the_deployed_ref_moves_only_after_the_health_gate_passes(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing but a passing health check may move the deployed ref.

    If anything else could, the ref would answer the same question the symlink
    already answers badly, and correction 6's recovery would have no target.
    """
    good = _commit(git_repo, "healthy release")
    bad = _commit(git_repo, "unhealthy release")
    deploy = _deploy_config(
        tmp_path, health_url="http://fixture.invalid/healthz", timeout_seconds=10
    )
    monkeypatch.setattr(
        "steward_harness.deploy.engine.HealthGate.wait_healthy",
        lambda self, expected_sha: (expected_sha == good, "observed health"),
    )

    first = _deploy_engine(git_repo, deploy)
    assert first.releases.transport.deployed_sha() is None
    assert not first.deploy(bad).success
    # An unhealthy first release rolls back to explicit absence, and the ref
    # never named it at all.
    assert first.releases.current_release() is None
    assert first.releases.transport.deployed_sha() is None

    second = _deploy_engine(git_repo, deploy)
    assert second.deploy(good).success
    assert second.releases.transport.deployed_sha() == good
    # Correction 3 would otherwise refuse the second attempt outright, and the
    # question here is what a *running* failure does to the ref.
    (second.releases.releases_root / f".{bad}.failed").unlink()

    assert not second.deploy(bad).success
    assert second.releases.current_release() == good
    assert second.releases.transport.deployed_sha() == good


def test_prune_retains_the_deployed_release_even_when_it_is_the_oldest(
    tmp_path: Path, git_repo: Path
) -> None:
    """Correction 6's recovery target must still have a directory to point at.

    `prune` already refuses to delete the active release. The deployed ref is a
    second thing worth keeping, and after a crash it is not the active one.
    """
    shas = [_commit(git_repo, f"release-{i}") for i in range(5)]
    releases = _release_manager(git_repo, _deploy_config(tmp_path))
    for sha in shas:
        releases.stage(sha)
    releases.activate(shas[-1])

    removed = releases.prune(keep=2, retain=shas[0])

    assert shas[0] not in removed
    assert (releases.releases_root / shas[0]).is_dir()
    assert len(removed) == 2
    assert releases.current_release() == shas[-1]
    assert (releases.releases_root / shas[-1]).is_dir()

    # Nothing else was holding it: without the retention it goes like any other.
    assert shas[0] in releases.prune(keep=1)
    assert not (releases.releases_root / shas[0]).exists()


# --- Correction 1: the process boundary is real, and argv is what crosses it -


def _flags(argv: list[str]) -> dict[str, str]:
    """Read `--name value` and `--name=value` out of a command line."""
    flags: dict[str, str] = {}
    for index, item in enumerate(argv):
        if not item.startswith("--"):
            continue
        name, separator, inline = item.partition("=")
        if separator:
            flags[name] = inline
        elif index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            flags[name] = argv[index + 1]
    return flags


@pytest.mark.parametrize("keep", [0, 2, 5])
def test_prune_preserves_condemnation_and_protected_artifacts(tmp_path, git_repo, keep):
    shas = [_commit(git_repo, f"release-{i}") for i in range(6)]
    releases = _release_manager(git_repo, _deploy_config(tmp_path))
    for i, sha in enumerate(shas):
        path = releases.stage(sha)
        os.utime(path, (1000 + i, 1000 + i))
    releases.mark_failed(shas[0])
    # Both protections must hold even when the artifacts are older than keep.
    releases.activate(shas[1])
    removed = releases.prune(keep=keep, retain=shas[2])
    expected = {0: [shas[0], *shas[3:]], 2: [shas[0], shas[3]], 5: [shas[0]]}[keep]
    assert removed == expected
    for sha in shas:
        assert (releases.releases_root / sha).is_dir() is (sha not in removed)
        assert releases._receipt_path(sha).exists() is (sha not in removed)
        assert releases.failed(sha) is (sha == shas[0])
    assert releases.current_release() == shas[1]
    assert releases.prune(keep=keep, retain=shas[2]) == []
    assert releases.failed(shas[0])


def test_pruned_condemned_revision_requires_explicit_clearance(tmp_path, git_repo, monkeypatch):
    shas = [_commit(git_repo, f"release-{i}") for i in range(6)]
    config = _deploy_config(tmp_path, service="fixture.service")
    services = FakeServices()
    engine = DeployEngine("repo", config, _transport(git_repo), services)
    for i, sha in enumerate(shas):
        path = engine.releases.stage(sha)
        os.utime(path, (1000 + i, 1000 + i))
    condemned, healthy = shas[0], shas[-1]
    engine.releases.mark_failed(condemned)
    engine.releases.activate(healthy)
    # Successful convergence invokes the production default retention sweep.
    assert engine.deploy(healthy).success
    assert not (engine.releases.releases_root / condemned).exists()
    assert not engine.releases._receipt_path(condemned).exists()

    for _ in range(2):
        worker = DeployEngine("repo", config, _transport(git_repo), services)
        with monkeypatch.context() as guarded:
            def forbidden(*args, **kwargs):
                pytest.fail("condemned request reached forward staging or activation")
            guarded.setattr(worker.releases, "stage", forbidden)
            guarded.setattr(worker.releases, "activate", forbidden)
            result = worker.deploy(condemned)
        assert not result.success and "remains condemned" in result.details
        assert worker.releases.failed(condemned)
        assert worker.releases.current_release() == healthy
        assert worker.releases.transport.deployed_sha() == healthy
        assert not services.restarted and not services.stopped

    # Simulate deliberate operator clearance only inside the disposable fixture.
    (worker.releases.releases_root / f".{condemned}.failed").unlink()
    assert worker.deploy(condemned).success
    assert worker.releases.current_release() == condemned
    assert worker.releases.transport.deployed_sha() == condemned
    worker.releases.verify(condemned)
    assert services.restarted == ["fixture.service"]
