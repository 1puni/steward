"""Systemd service operations kept behind a narrow controller boundary."""

from __future__ import annotations

import logging
import subprocess
import hashlib
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class ServiceError(RuntimeError):
    """Raised when a systemd operation fails."""


class ServiceController(Protocol):
    """The service operations a deployment needs; swap in a fake for tests."""

    def restart(self, service: str) -> None: ...

    def stop(self, service: str) -> None: ...


class SystemdController:
    """Real systemctl invocations for deployment restarts."""

    def restart(self, service: str) -> None:
        self._run("restart", service)

    def stop(self, service: str) -> None:
        self._run("stop", service)

    @staticmethod
    def _run(operation: str, service: str) -> None:
        res = subprocess.run(
            ["systemctl", operation, service],
            check=False,
            capture_output=True,
            text=True,
            # systemd owns the service job and its configured stop/start bounds.
            # Timing out this client leaves that job running and can race a
            # rollback against a controller still draining accepted work.
            timeout=None,
        )
        if res.returncode != 0:
            raise ServiceError(f"systemctl {operation} {service} failed: {res.stderr.strip()}")


class NoopController:
    """Deployment without a service declaration restarts nothing."""

    def restart(self, service: str) -> None:
        return None

    def stop(self, service: str) -> None:
        return None


def transient_deploy_unit(config_path: str, repository: str) -> str:
    """Stable detached ownership for this configured repository across restart."""
    identity = f"{Path(config_path).resolve()}\0{repository}"
    return "steward-deploy-" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def service_stopped(service: str) -> bool:
    """Only an observed inactive unit proves rollback to stopped absence."""
    result = subprocess.run(
        ["systemctl", "show", "--property=ActiveState", "--value", service],
        check=True, capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip() == "inactive"


def transient_deploy_active(config_path: str, repository: str) -> bool:
    """Whether this repository's unit still owns detached mutation."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", f"{transient_deploy_unit(config_path, repository)}.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceError(
            f"could not determine transient deployment state: {exc}"
        ) from exc
    state = result.stdout.strip()
    if state in {
        "activating", "active", "reloading", "deactivating"
    }:
        return True
    if state in {"inactive", "failed", "unknown", "not-found", ""}:
        return False
    raise ServiceError(
        f"ambiguous transient deployment state {state!r}"
    )
