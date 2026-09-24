"""Installed systemd target driver: finite observe/apply, supervised worker."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

import httpx
import yaml

from steward_harness.config.loader import load_config
from steward_harness.deploy.config import SystemdReleaseConfig
from steward_harness.deploy.engine import DeployEngine
from steward_harness.deploy.health import HealthGate
from steward_harness.deploy.release import ReleaseError
from steward_harness.deploy.systemd import service_stopped
from steward_harness.deploy.transport import ReleaseTransport
from steward_harness.git import validate_object_id
from steward_harness.git_transport import controller_transport
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.targets import Observation


def observe(engine: DeployEngine, revision: str) -> Observation:
    """Derive readiness and useful recovery work from external and release truth."""
    settings = engine.deploy_config
    result = Observation(revision=None, ready=False, details="health not observed")
    try:
        response = httpx.get(settings.health_url, timeout=5)
        result.revision = HealthGate._reported_sha(response)
        if response.status_code == 200 and result.revision:
            engine.releases.verify(result.revision, check_policy=False)
            result.ready = True
            result.details = "external health and immutable release verified"
    except (httpx.HTTPError, RuntimeError, ValueError) as error:
        result.details = str(error)

    releases = engine.releases
    try:
        current = releases.current_release()
        if releases.failed(revision):
            previous = releases.transport.deployed_sha()
            reason = None
            if previous == revision:
                reason = "no safe rollback target"
            elif current not in {revision, previous}:
                reason = "active release changed; refusing to overwrite it"
            else:
                if previous is not None and not (result.ready and result.revision == previous):
                    # Recovery cannot execute a damaged previous artifact either.
                    releases.verify(previous, check_policy=False)
                if current == previous and previous is not None and result.ready and result.revision == previous:
                    reason = "rollback is already healthy"
                elif current is None and previous is None and (settings.service is None or service_stopped(settings.service)):
                    reason = "rollback to stopped absence is complete"
            if reason:
                result.blocked = True
                result.details = f"release remains condemned; {reason}; change the desired revision or explicitly clear its failure marker after diagnosis"
            else:
                result.details = "release remains condemned; rollback still requires verified recovery"
            if result.revision == revision:
                # A condemned process coming back up cannot cancel rollback.
                result.ready = False
        elif current == revision and not (result.ready and result.revision == revision):
            # Reapplying cannot repair an immutable active artifact. Reobserve
            # every pass so an operator repair or changed desired ref unblocks it.
            releases.verify(revision, check_policy=False)
    except ReleaseError as error:
        result.blocked = True
        result.details = f"release integrity requires operator repair: {error}"
    return result


def _check_observation_boundary(config, config_path, settings_path, settings):
    """Trust observation evidence without requiring a usable build workspace."""
    if not config.requires_execution_boundary:
        return
    protected = {"configuration": config_path, "driver settings": settings_path,
                 "release root": Path(settings.release_root),
                 "release pointer directory": Path(settings.current_symlink).parent}
    if config.execution.user is None:
        raise PermissionError("observation evidence requires an execution identity")
    broker = UntrustedExecutionBroker(config.execution, protected_paths=protected)
    _user, uid, gid, _home = broker._resolved_identity()
    if uid == 0 or gid == 0 or uid == os.geteuid():
        raise PermissionError("observation evidence is not isolated from execution identity")
    failure = broker._path_failure(
        uid, gid, probe_environment=broker._command_environment({}),
        identity_kwargs=broker._identity_kwargs(),
    )
    if failure:
        raise PermissionError(f"observation evidence is untrusted: {failure}")


def main(argv=None, *, worker_entry=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("operation", choices=("observe", "apply", "worker"))
    parser.add_argument("--target")
    parser.add_argument("--revision")
    parser.add_argument("--policy")
    args = parser.parse_args(argv)
    config_path, settings_path = Path(args.config).resolve(), Path(args.settings).resolve()
    config = load_config(config_path)
    settings = SystemdReleaseConfig.model_validate(yaml.safe_load(settings_path.read_text()))
    policy = hashlib.sha256(config_path.read_bytes() + settings_path.read_bytes()).hexdigest()
    request = dict(target=args.target, revision=args.revision) if args.operation == "worker" else json.load(sys.stdin)
    name, revision = request["target"], validate_object_id(request["revision"])
    if args.operation == "worker" and args.policy != policy:
        raise ValueError("accepted driver settings changed before worker started")
    _, repository, branch = config.targets[name].ref.split("/", 2)
    transport = ReleaseTransport(controller_transport(config, repository), name, branch)
    if not settings.health_url:
        raise ValueError("systemd target requires external exact-revision health URL")
    if args.operation == "observe":
        # Observation reads trusted release and external health truth; it runs
        # no candidate code. Re-probing every model-writable path and UID here
        # made a blocked deployment repeatedly pay for an unused build boundary.
        _check_observation_boundary(config, config_path, settings_path, settings)
        engine = DeployEngine(name, settings, transport)
        result = observe(engine, revision)
        result.busy = engine.busy()
        print(result.model_dump_json())
        return 0
    broker = UntrustedExecutionBroker.for_steward(config, config_path)
    broker._protected_paths.update({"driver settings": settings_path,
        "release root": Path(settings.release_root),
        "release pointer directory": Path(settings.current_symlink).parent})
    if config.requires_execution_boundary and not broker.status().enforced:
        raise PermissionError("systemd driver execution boundary is unavailable")
    engine = DeployEngine(name, settings, transport, broker=broker)
    if args.operation == "worker":
        result = engine.deploy(revision)
        logging.warning(result.details)
        return 0 if result.success else 1
    # The engine's pointer lease serializes workers across controller restarts:
    # one dispatched before the running worker took it finds it held and exits.
    # The worker's argv retains revision and accepted policy identity. A failed
    # revision may still owe rollback. The leased worker decides; successful
    # dispatch is not proof that either deployment or recovery ran.
    if not engine.busy():
        result = observe(engine, revision)
        if result.blocked:
            logging.warning(result.details)
            return 0
        if result.ready and result.revision == revision:
            return 0
        worker = [sys.executable, "-B", str(Path(worker_entry).resolve())] if worker_entry else [sys.executable, "-B", "-m", "steward_harness.deploy.cli"]
        subprocess.run(["systemd-run", "--no-block", "--collect", *worker,
            "--config", str(config_path), "--settings", str(settings_path), "worker",
            "--target", name, "--revision", revision, "--policy", policy], check=True, timeout=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
