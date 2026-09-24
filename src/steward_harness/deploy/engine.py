"""Deployment orchestration: stage, activate, restart, verify, roll back."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from steward_harness.deploy.config import SystemdReleaseConfig
from steward_harness.deploy.health import HealthGate
from steward_harness.deploy.release import ReleaseError, ReleaseManager
from steward_harness.deploy.systemd import NoopController, ServiceController, ServiceError, SystemdController
from steward_harness.git_transport import ControllerGitTransport, GitTransportError
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.lease import Busy, Lease

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeployInspection:
    """Release state derived from the release directory and current symlink."""

    current_sha: str | None
    already_active: bool
    verification_error: str | None = None


@dataclass(frozen=True, slots=True)
class DeployResult:
    success: bool
    rolled_back_to: str | None
    details: str

    def __post_init__(self) -> None:
        if self.success and self.rolled_back_to is not None:
            raise ValueError("successful deployment cannot have a rollback target")


@dataclass(frozen=True, slots=True)
class RollbackResult:
    """Verified rollback outcome, including successful restoration of absence."""

    success: bool
    active_sha: str | None
    details: str


class DeployEngine:
    """Lands a release behind the atomic symlink and proves it healthy, or rolls it back."""

    def __init__(
        self,
        repo_name: str,
        deploy_config: SystemdReleaseConfig,
        transport: ControllerGitTransport,
        services: ServiceController | None = None,
        broker: UntrustedExecutionBroker | None = None,
    ) -> None:
        self.repo_name = repo_name
        self.deploy_config = deploy_config
        self.releases = ReleaseManager(repo_name, transport, self.deploy_config, broker)
        self.services = services or (
            SystemdController() if self.deploy_config.service else NoopController()
        )
        pointer = self.releases.current_symlink
        # A second outlasts an observer's momentary probe, never a deployment.
        self._lease = Lease(
            pointer.parent,
            timeout_seconds=1,
            lock_name=f".{pointer.name}.steward-deploy.lock",
        )

    def busy(self) -> bool:
        """A worker holds this release pointer's deployment lease."""
        return self._lease.held()

    def _inspect(self, sha: str) -> DeployInspection:
        current = self.releases.current_release()
        try:
            self.releases.verify(sha)
        except ReleaseError as exc:
            verification_error = str(exc)
        else:
            verification_error = None
        return DeployInspection(
            current_sha=current,
            already_active=current == sha,
            verification_error=verification_error,
        )

    def deploy(self, sha: str) -> DeployResult:
        """Converge release state on ``sha`` using the current symlink as activation truth."""
        try:
            with self._lease:
                return self._reconcile(sha)
        except Busy:
            return DeployResult(
                False, None, "release deployment is already in progress"
            )
        except ReleaseError as exc:
            return DeployResult(False, None, f"invalid release boundary: {exc}")

    def _reconcile(self, sha: str) -> DeployResult:
        # Condemnation forbids forward deployment, not completion of rollback.
        # Read it under the pointer lease, including for already queued workers.
        if self.releases.failed(sha):
            previous = self.releases.transport.deployed_sha()
            if previous == sha:
                return DeployResult(False, None, "condemned release is also the last healthy revision; no safe rollback target")
            if previous is not None and self.releases.current_release() == previous:
                healthy, details = self._verify(previous, check_policy=False)
                if healthy:
                    return DeployResult(False, previous, f"release remains condemned; rollback already healthy; {details}")
            return self._failed_after_rollback(sha, previous, "release remains condemned")
        inspection = self._inspect(sha)
        # Not `inspection.current_sha`: a crash between activation and the
        # health check has already moved that pointer onto the release we
        # would be rolling away from.
        previous = self.releases.transport.deployed_sha()
        if previous is None and inspection.current_sha is not None:
            healthy, _ = self._verify(inspection.current_sha)
            if healthy:
                previous = inspection.current_sha
                self.releases.transport.set_deployed(previous)

        if inspection.already_active and inspection.verification_error is not None:
            details = f"active release failed verification: {inspection.verification_error}"
            return DeployResult(False, None, details)

        if inspection.already_active:
            healthy, details = self._verify(sha)
            if healthy:
                return self._deployed(sha, f"already active; {details}")
            if self.deploy_config.service is not None:
                restarted, restart_details = self._restart()
                if restarted:
                    healthy, details = self._verify(sha)
                else:
                    details = restart_details
            if healthy:
                return self._deployed(sha, details)
            return self._failed_after_rollback(sha, previous, details)

        try:
            self.releases.transport.resolve_remote_commit(sha)
            self.releases.stage(sha)
        except (GitTransportError, ReleaseError) as exc:
            # The other exit that leaves the pointer alone. Without the marker
            # a tree that cannot build is retried on every pass forever, and
            # silently; explicit operator clearance is the deliberate retry.
            self.releases.mark_failed(sha)
            details = f"staging failed: {exc}"
            return DeployResult(False, None, details)

        try:
            self.releases.activate(sha)
        except ReleaseError as exc:
            details = f"activation failed: {exc}"
            return DeployResult(False, None, details)

        restarted, restart_details = self._restart()
        if not restarted:
            return self._failed_after_rollback(sha, previous, restart_details)

        healthy, details = self._verify(sha)
        if healthy:
            return self._deployed(sha, details)
        return self._failed_after_rollback(sha, previous, details)

    def _deployed(self, sha: str, details: str) -> DeployResult:
        """Health passed, so this release has served traffic; record and prune.

        The ref moves here and nowhere else, which is what lets a later pass
        read it as *the last release that worked* rather than *the last one we
        pointed at*.

        Serving traffic also retires any earlier condemnation of this SHA. The
        marker means "this release broke"; once the same release has passed its
        health gate, keeping it would be a second, stale answer to a question
        the ref now answers — and two representations of one fact that can
        disagree is the shape this whole refactor exists to remove.
        """
        self.releases.clear_failed(sha)
        self.releases.transport.set_deployed(sha)
        pruned = self.releases.prune(retain=sha)
        if pruned:
            log.info("Pruned %d old release(s) for %s", len(pruned), self.repo_name)
        return DeployResult(True, None, details)

    def _failed_after_rollback(
        self,
        desired_sha: str,
        previous: str | None,
        failure: str,
    ) -> DeployResult:
        rollback = self._rollback(desired_sha, previous)
        return DeployResult(
            False, rollback.active_sha if rollback.success else None,
            f"{failure}; {rollback.details}",
        )

    def _restart(
        self,
        *,
        rollback: bool = False,
    ) -> tuple[bool, str]:
        service = self.deploy_config.service
        if service is None:
            return True, "no service configured"
        context = "rollback restart" if rollback else "restart"
        try:
            self.services.restart(service)
        except (ServiceError, OSError, subprocess.SubprocessError) as exc:
            details = f"service {context} failed: {exc}"
            log.error("Service %s failed for %s: %s", context, service, exc)
            return False, details
        return True, f"service {context} succeeded"

    def _verify(self, sha: str, *, check_policy: bool = True) -> tuple[bool, str]:
        health_url = self.deploy_config.health_url
        if health_url is None:
            result = (False, "no external exact-revision health observation configured")
        else:
            gate = HealthGate(health_url, self.deploy_config.timeout_seconds)
            result = gate.wait_healthy(
                expected_sha=sha
            )
        if result[0]:
            try:
                self.releases.verify(sha, check_policy=check_policy)
            except ReleaseError as exc:
                result = (False, f"running release failed verification: {exc}")
        return result

    def rollback(self, desired_sha: str, previous: str | None) -> RollbackResult:
        """Converge on the recorded previous release without reactivating desired."""
        try:
            with self._lease:
                return self._rollback(desired_sha, previous)
        except Busy:
            details = "release deployment is already in progress"
            return RollbackResult(False, None, details)
        except ReleaseError as exc:
            return RollbackResult(False, None, f"invalid release boundary: {exc}")

    def _rollback(self, desired_sha: str, previous: str | None) -> RollbackResult:
        current = self.releases.current_release()
        if current not in {desired_sha, previous}:
            details = (
                f"active release changed from deployment {desired_sha} to {current}; "
                f"refusing rollback to {previous}"
            )
            return RollbackResult(False, current, details)

        # Persist intent before the pointer moves, even when a prior process
        # already restored it: restart/stop and health are still unproved and
        # may mutate live service. The marker forbids forward deployment while
        # subsequent workers can still finish restoration and verify health.
        self.releases.mark_failed(desired_sha)
        try:
            if current != previous:
                if previous is None:
                    self.releases.deactivate()
                else:
                    self.releases.activate(previous, check_policy=False)
            elif previous is not None:
                # An already-restored pointer is not proof its artifact stayed
                # intact. Never restart model-modified release content.
                self.releases.verify(previous, check_policy=False)
        except ReleaseError as exc:
            details = f"rollback release verification or activation failed: {exc}"
            current = self.releases.current_release()
            return RollbackResult(False, current, details)

        if previous is None:
            service = self.deploy_config.service
            try:
                if service is not None:
                    self.services.stop(service)
            except (ServiceError, OSError, subprocess.SubprocessError) as exc:
                details = f"rollback service stop failed: {exc}"
                return RollbackResult(False, None, details)
        else:
            restarted, details = self._restart(rollback=True)
            if restarted:
                restarted, details = self._verify(previous, check_policy=False)
            if not restarted:
                details = f"rollback failed: {details}"
                return RollbackResult(False, previous, details)

        target = "no release" if previous is None else previous
        details = f"rollback converged to {target}"
        # `_deployed` was the only caller of prune, so "at most five release
        # directories" held only on hosts where a deployment eventually
        # succeeded; a run of tips that stage and then fail health grew the
        # disk without bound. A converged rollback has just proved `previous`
        # healthy by the same gate `_deployed` prunes on, so it is entitled to
        # the same sweep — and the failed release is the newest directory, so
        # this never deletes the thing it was reached by.
        self.releases.prune(retain=previous)
        return RollbackResult(True, previous, details)
