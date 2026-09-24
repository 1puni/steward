"""Installed-driver process protocol with a simulated external service/supervisor.

Real Linux identity and HTTP integration remains in test_linux_deployment_acceptance.
"""
import hashlib
import io
import json
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from steward_harness.deploy import cli
from steward_harness.deploy.health import HealthGate
from steward_harness.deploy.systemd import SystemdController
from steward_harness.deploy.transport import ReleaseTransport
from steward_harness.git_transport import controller_transport
from steward_harness.config.loader import load_config
from steward_harness.runtime.execution import BoundaryStatus, UntrustedExecutionBroker
from test_task_runner_kernel import _git, _repository


def installed(tmp_path, monkeypatch):
    remote, clone = _repository(tmp_path)
    revision = _git("rev-parse", "main", cwd=clone)
    config_path, settings_path = tmp_path / "controller.yaml", tmp_path / "target.yaml"
    config_path.write_text(yaml.safe_dump(dict(
        identity=dict(name="test", slug="test"),
        provider=dict(state_db=str(tmp_path / "state.db"), workdir=str(tmp_path / "work")),
        repositories={"app": dict(path=str(clone), remote_url=str(remote))},
        targets={"production": dict(ref="repositories/app/main", driver=str(tmp_path / "installed-driver"))},
    )))
    pointer = tmp_path / "current"
    settings_path.write_text(yaml.safe_dump(dict(service="fixture.service", health_url="http://fixture.invalid/healthz",
        release_root=str(tmp_path / "releases"), current_symlink=str(pointer))))
    monkeypatch.setattr(UntrustedExecutionBroker, "status", lambda self: BoundaryStatus(True, "simulated test boundary"))
    monkeypatch.setattr(cli, "_check_observation_boundary", lambda *args: None)
    external, jobs = {"revision": None, "stopped": False}, []
    monkeypatch.setattr(SystemdController, "restart", lambda self, service: external.update(revision=pointer.resolve().name))
    monkeypatch.setattr(SystemdController, "stop", lambda self, service: external.update(revision=None, stopped=True))
    monkeypatch.setattr(cli, "service_stopped", lambda service: external["stopped"])
    monkeypatch.setattr(HealthGate, "wait_healthy", lambda gate, expected_sha=None, **kw:
                        (external["revision"] == expected_sha, "external simulated process identity"))
    monkeypatch.setattr(cli.httpx, "get", lambda *a, **kw: httpx.Response(
        200 if external["revision"] else 503, json={"sha": external["revision"]}))
    monkeypatch.setattr(cli.DeployEngine, "busy", lambda self: False)
    original = subprocess.run
    def supervise(argv, *args, **kwargs):
        if argv[0] == "systemd-run":
            jobs.append(argv)
            return subprocess.CompletedProcess(argv, 0)
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(cli.subprocess, "run", supervise)
    argv = ["--config", str(config_path), "--settings", str(settings_path)]
    def invoke(operation):
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(dict(target="production", revision=revision))))
        return cli.main([*argv, operation])
    return config_path, settings_path, pointer, revision, external, jobs, invoke


def test_apply_handoff_retains_exact_revision_and_policy_without_controller_memory(tmp_path, monkeypatch, capsys):
    config_path, settings, pointer, revision, external, jobs, invoke = installed(tmp_path, monkeypatch)
    assert invoke("apply") == 0
    assert external["revision"] is None  # dispatch success is not readiness
    job = jobs[0]
    assert "-B" in job  # Supervised imports must preserve immutable artifacts.
    assert "--no-block" in job and "--collect" in job
    assert job[job.index("--revision") + 1] == revision
    policy = hashlib.sha256(config_path.read_bytes() + settings.read_bytes()).hexdigest()
    assert job[job.index("--policy") + 1] == policy
    # A fresh invocation needs only supervisor argv and durable Git/settings.
    assert cli.main(job[job.index("--config"):]) == 0
    assert external["revision"] == revision
    assert invoke("observe") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["ready"] is True
    source = controller_transport(load_config(config_path), "app")
    assert ReleaseTransport(source, "production", "main").deployed_sha() == revision
    assert ReleaseTransport(source, "preview", "main").deployed_sha() is None
    # A tampered file invalidates observation even when the external SHA matches.
    file = next(p for p in pointer.resolve().rglob("*") if p.is_file())
    file.write_text("tampered")
    assert invoke("observe") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["ready"] is False


def test_supervised_worker_rejects_changed_policy_before_mutation(tmp_path, monkeypatch):
    _, settings, pointer, _, external, jobs, invoke = installed(tmp_path, monkeypatch)
    invoke("apply")
    settings.write_text(settings.read_text() + "\n# changed before worker start\n")
    with pytest.raises(ValueError, match="settings changed"):
        cli.main(jobs[0][jobs[0].index("--config"):])
    assert not pointer.exists() and external["revision"] is None


def test_observation_does_not_require_build_identity_but_apply_does(tmp_path, monkeypatch, capsys):
    _, _, _, _, _, jobs, invoke = installed(tmp_path, monkeypatch)
    def unavailable(*args, **kwargs):
        raise PermissionError("build identity unavailable")
    monkeypatch.setattr(UntrustedExecutionBroker, "for_steward", unavailable)
    assert invoke("observe") == 0
    assert json.loads(capsys.readouterr().out)["ready"] is False
    with pytest.raises(PermissionError, match="build identity unavailable"):
        invoke("apply")
    assert jobs == []


def test_installed_health_adapter_survives_worker_handoff(tmp_path, monkeypatch):
    config, settings, _, revision, _, jobs, _ = installed(tmp_path, monkeypatch)
    entry = tmp_path / 'site-target.py'
    monkeypatch.setattr(cli.sys, 'stdin', io.StringIO(json.dumps(dict(target='production', revision=revision))))
    assert cli.main(['--config', str(config), '--settings', str(settings), 'apply'], worker_entry=entry) == 0
    job = jobs[0]
    assert "-B" in job  # Supervised imports must preserve immutable artifacts.
    assert str(entry.resolve()) in job
    assert 'steward_harness.deploy.cli' not in job
    assert job[job.index('--revision') + 1] == revision


@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.parametrize("interruption", ["pointer", "service"])
def test_apply_resumes_condemned_rollback(tmp_path, monkeypatch, capsys, prior, interruption):
    from steward_harness.deploy.config import SystemdReleaseConfig
    from steward_harness.deploy.engine import DeployEngine

    config, settings, pointer, initial, external, jobs, _ = installed(tmp_path, monkeypatch)
    clone = tmp_path / "repo"
    _git("commit", "--allow-empty", "-qm", "broken release", cwd=clone)
    desired = _git("rev-parse", "HEAD", cwd=clone)
    _git("push", "-q", "origin", "main", cwd=clone)
    transport = ReleaseTransport(controller_transport(load_config(config), "app"), "production", "main")
    transport.resolve_remote_commit(desired)
    engine = DeployEngine("production", SystemdReleaseConfig.model_validate(yaml.safe_load(settings.read_text())), transport)
    previous = initial if prior else None
    if previous:
        engine.releases.stage(previous)
        transport.set_deployed(previous)
    engine.releases.stage(desired)
    engine.releases.activate(desired)

    class Interrupted(BaseException):
        pass

    def interrupt(*args, **kwargs):
        raise Interrupted()

    with monkeypatch.context() as crash:
        if interruption == "pointer":
            crash.setattr(engine.releases, "activate" if prior else "deactivate", interrupt)
        else:
            crash.setattr(engine.services, "restart" if prior else "stop", interrupt)
        with pytest.raises(Interrupted):
            engine.rollback(desired, previous)
    assert engine.releases.failed(desired)
    assert engine.releases.current_release() == (desired if interruption == "pointer" else previous)
    assert external["revision"] is None
    # Even a condemned process that now reports healthy must finish recovery.
    external["revision"] = desired

    # No forward staging or activation of the condemned release is allowed.
    from steward_harness.deploy.release import ReleaseManager
    operations = []
    restart, stop = SystemdController.restart, SystemdController.stop
    def record_restart(self, service):
        operations.append("restart")
        restart(self, service)
    def record_stop(self, service):
        operations.append("stop")
        stop(self, service)
    monkeypatch.setattr(SystemdController, "restart", record_restart)
    monkeypatch.setattr(SystemdController, "stop", record_stop)
    activate = ReleaseManager.activate
    def guarded_activate(self, sha, **kwargs):
        assert sha != desired
        return activate(self, sha, **kwargs)
    monkeypatch.setattr(ReleaseManager, "activate", guarded_activate)
    monkeypatch.setattr(ReleaseManager, "stage", lambda *a, **kw: pytest.fail("recovery staged a release"))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(dict(target="production", revision=desired))))
    cli.main(["--config", str(config), "--settings", str(settings), "observe"])
    pending = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert pending["revision"] == desired and not pending["ready"] and not pending["blocked"]
    for _attempt in range(2):
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(dict(target="production", revision=desired))))
        assert cli.main(["--config", str(config), "--settings", str(settings), "apply"]) == 0
        if _attempt == 0:
            assert external["revision"] == desired  # only dispatch accepted
            # Recovery may succeed, but deployment of desired still failed.
            job = jobs[-1]
            assert cli.main(job[job.index("--config"):]) == 1
        assert len(jobs) == 1  # Recovered failure does not launch another worker.
        assert engine.releases.current_release() == previous
        assert external["revision"] == previous
        assert engine.releases.failed(desired)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(dict(target="production", revision=desired))))
    cli.main(["--config", str(config), "--settings", str(settings), "observe"])
    observation = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert observation["revision"] == previous
    assert observation["ready"] is prior
    assert observation["blocked"] is True
    assert operations == (["restart"] if prior else ["stop"])


def test_active_integrity_failure_blocks_repeated_dispatch_until_repaired(tmp_path, monkeypatch, capsys):
    _, _, pointer, revision, external, jobs, invoke = installed(tmp_path, monkeypatch)
    invoke("apply")
    job = jobs[-1]
    assert cli.main(job[job.index("--config"):]) == 0
    # Reproduce the overnight incident: Python cache added to a healthy release.
    cache = pointer.resolve() / "__pycache__"
    cache.mkdir()
    bytecode = cache / "unexpected.pyc"
    bytecode.write_bytes(b"unreceipted bytecode")
    invoke("observe")
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["ready"] and not result["blocked"]
    invoke("apply")
    assert len(jobs) == 1
    # Real content drift still blocks dispatch, even beside interpreter caches.
    unexpected = pointer.resolve() / "unexpected.py"
    unexpected.write_text("raise RuntimeError('drift')")
    for _ in range(3):
        invoke("observe")
        result = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert result["revision"] == revision and not result["ready"]
        assert result["blocked"] and "integrity" in result["details"]
        invoke("apply")
    assert len(jobs) == 1 and external["revision"] == revision
    unexpected.unlink()
    invoke("observe")
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["ready"] and not result["blocked"]
    invoke("apply")
    assert len(jobs) == 1  # A delayed apply also skips a now-satisfied revision.


def test_failed_staging_stops_dispatch_but_new_revision_can_deploy(tmp_path, monkeypatch, capsys):
    config, settings, pointer, initial, external, jobs, invoke = installed(tmp_path, monkeypatch)
    invoke("apply")
    job = jobs[-1]
    assert cli.main(job[job.index("--config"):]) == 0
    clone = tmp_path / "repo"
    _git("commit", "--allow-empty", "-qm", "broken candidate", cwd=clone)
    desired = _git("rev-parse", "HEAD", cwd=clone)
    _git("push", "-q", "origin", "main", cwd=clone)
    request = lambda sha: monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(dict(target="production", revision=sha))))
    argv = ["--config", str(config), "--settings", str(settings)]
    from steward_harness.deploy.release import ReleaseManager, ReleaseError
    def fail_build(*args):
        raise ReleaseError("build failed")
    with monkeypatch.context() as broken:
        broken.setattr(ReleaseManager, "stage", fail_build)
        request(desired)
        cli.main([*argv, "apply"])
        job = jobs[-1]
        assert cli.main(job[job.index("--config"):]) == 1
    assert len(jobs) == 2
    for _ in range(3):
        request(desired)
        cli.main([*argv, "observe"])
        result = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert result["blocked"] and result["revision"] == initial
        request(desired)
        cli.main([*argv, "apply"])
    assert len(jobs) == 2
    _git("commit", "--allow-empty", "-qm", "corrected candidate", cwd=clone)
    corrected = _git("rev-parse", "HEAD", cwd=clone)
    _git("push", "-q", "origin", "main", cwd=clone)
    request(corrected)
    cli.main([*argv, "apply"])
    assert len(jobs) == 3
    job = jobs[-1]
    assert cli.main(job[job.index("--config"):]) == 0
    assert external["revision"] == corrected and pointer.resolve().name == corrected


def test_apply_leaves_active_supervisor_in_charge(tmp_path, monkeypatch):
    *_, jobs, invoke = installed(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.DeployEngine, "busy", lambda self: True)
    assert invoke("apply") == 0
    assert jobs == []


@pytest.mark.parametrize("state", ["inactive", "active", "activating", "deactivating", "failed", "", "unknown"])
def test_stopped_absence_requires_observed_inactive_service(monkeypatch, state):
    from steward_harness.deploy.systemd import service_stopped
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=state + "\n"))
    assert service_stopped("fixture.service") is (state == "inactive")


@pytest.mark.parametrize("damage", ["active-last-healthy", "rollback-artifact"])
def test_condemned_release_without_safe_recovery_blocks_dispatch(tmp_path, monkeypatch, capsys, damage):
    from steward_harness.deploy.config import SystemdReleaseConfig
    from steward_harness.deploy.engine import DeployEngine
    config, settings, pointer, initial, external, jobs, invoke = installed(tmp_path, monkeypatch)
    invoke("apply")
    job = jobs[-1]
    assert cli.main(job[job.index("--config"):]) == 0
    transport = ReleaseTransport(controller_transport(load_config(config), "app"), "production", "main")
    engine = DeployEngine("production", SystemdReleaseConfig.model_validate(yaml.safe_load(settings.read_text())), transport)
    desired = initial
    if damage == "rollback-artifact":
        clone = tmp_path / "repo"
        _git("commit", "--allow-empty", "-qm", "unhealthy candidate", cwd=clone)
        desired = _git("rev-parse", "HEAD", cwd=clone)
        _git("push", "-q", "origin", "main", cwd=clone)
        transport.resolve_remote_commit(desired)
        engine.releases.stage(desired)
        engine.releases.activate(desired)
        (engine.releases.releases_root / initial / "unexpected.pyc").write_bytes(b"corrupt")
    engine.releases.mark_failed(desired)
    for _ in range(2):
        for operation in ("observe", "apply"):
            monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(dict(target="production", revision=desired))))
            assert cli.main(["--config", str(config), "--settings", str(settings), operation]) == 0
        observation = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert observation["blocked"]
    assert len(jobs) == 1 and pointer.resolve().name == desired


def test_observation_rejects_writable_evidence_without_probing_build_paths(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from steward_harness.config.schema import UntrustedExecutionConfig
    settings = SimpleNamespace(release_root=str(tmp_path / "releases"),
                               current_symlink=str(tmp_path / "current"))
    config = SimpleNamespace(requires_execution_boundary=True,
                             execution=UntrustedExecutionConfig(user="agent"))
    release_root = Path(settings.release_root)
    release_root.mkdir()
    config_path, settings_path = tmp_path / "config.yaml", tmp_path / "driver.yaml"
    config_path.touch()
    settings_path.touch()
    monkeypatch.setattr(UntrustedExecutionBroker, "_resolved_identity",
                        lambda self: ("agent", 999999, 999999, "/missing-build-home"))
    monkeypatch.setattr(UntrustedExecutionBroker, "_identity_kwargs", lambda self: {})
    monkeypatch.setattr(UntrustedExecutionBroker, "status",
                        lambda self: pytest.fail("observation probed build workspace"))
    probed = []
    def can_write(mode, path, *_):
        probed.append(path)
        return mode == "-w" and bool(path.stat().st_mode & 0o002)
    monkeypatch.setattr(UntrustedExecutionBroker, "_identity_can", staticmethod(can_write))
    # Limit namespace probing to this fixture; its macOS /tmp ancestors do not
    # model the controller-owned production installation.
    monkeypatch.setattr(UntrustedExecutionBroker, "_unstable_namespace", lambda *a, **kw: None)
    cli._check_observation_boundary(config, config_path, settings_path, settings)
    assert release_root in probed
    release_root.chmod(0o777)
    with pytest.raises(PermissionError, match="write controller-owned release root"):
        cli._check_observation_boundary(config, config_path, settings_path, settings)
