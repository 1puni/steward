"""Publish accepted task outcomes; retain exploration and publication provenance."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Mapping
from pathlib import Path

from steward_harness.config.schema import RepositoryConfig
from steward_harness.git_transport import (
    ControllerGitTransport,
    GitTransportError,
    GitTransportInterrupted,
)
from steward_harness.landing.merger import (
    PromotionEngine,
    ValidationFailure,
    publish,
)
from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeUnavailable
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import StateDatabase, TaskId
from steward_harness.task_lock import task_lock
from steward_harness.lease import Busy
from steward_harness.task_store import Task, TaskWorkConflict

log = logging.getLogger(__name__)


class RepositoryReconciler:
    """Gate and push one candidate per repository, holding its lock throughout."""

    def __init__(
        self,
        *,
        state: StateDatabase,
        repositories: Mapping[str, RepositoryConfig],
        transports: Mapping[str, ControllerGitTransport],
        worktrees_root: Path,
        broker: UntrustedExecutionBroker,
        procedures=None,
    ) -> None:
        self.last_outcome: dict[str, str] = {}
        self.procedures = procedures
        self.state = state
        self.repositories = dict(repositories)
        self.transports = dict(transports)
        self.worktrees_root = worktrees_root.resolve()
        self.broker = broker

    def publish_repository(self, repository_name: str) -> TaskId | None:
        """Publish this repository's oldest unpublished candidate, if any.

        The caller holds the repository lock. Returns the task that landed, or
        ``None`` when nothing was owed, the candidate failed its gates, or the
        remote moved first — all three of which leave the branches saying what
        is still true, so the next tick asks again and needs nothing carried.
        """
        repository = self.repositories.get(repository_name)
        transport = self.transports.get(repository_name)
        if repository is None or transport is None:
            raise LookupError(f"unmanaged repository {repository_name!r}")

        self.last_outcome[repository_name] = "nothing owed"
        tasks = [task for task in self.state.tasks.all() if task.repository == repository_name]
        if any(task.disposition == "idle" and not task.landed and not task.read_only for task in tasks):
            # Observe before deriving: finished work may have landed since the
            # last observation, including a push whose own observation was lost.
            transport.fetch()
            tasks = [task for task in self.state.tasks.all() if task.repository == repository_name]
        # The oldest task whose finished work has not landed. A withdrawal
        # arriving later is read again at the push, the decision that writes.
        task = next((task for task in tasks if task.publishable), None)
        if task is not None:
            lock = task_lock(self.state.tasks.locks_root, task.task_id)
            try:
                lock.acquire()
            except Busy:
                return None
            try:
                # A retry owes a new slice even if its old tip says idle.
                # The shared task lock prevents that slice and publication
                # from changing the same branch/decision concurrently.
                task = self.state.tasks.get(task.task_id)
                if not task.publishable:
                    return None
                return self._publish_task(task, repository, transport, task.tip) if task.tip else None
            finally:
                lock.release()
        outstanding = [task for task in tasks if task.status.value != "done"]
        if outstanding:
            self.last_outcome[repository_name] = "; ".join(
                f"{task.task_id}: {task.status.value}" + (f" ({task.reason})" if task.reason else "")
                for task in outstanding)
        return None

    def publish_one(self, repository_name: str) -> TaskId | None:
        """A failed publication affects only its own repository."""
        try:
            return self.publish_repository(repository_name)
        except GitTransportInterrupted as error:
            # The controller is stopping and took its own Git children with it.
            # The work stays durable in the branches and the next pass asks
            # again, exactly as for a busy world — so this is news about the
            # shutdown, not a failure of this repository.
            log.info(
                "Publication of %s stopped with the controller: %s",
                repository_name, error,
            )
            return None
        except (
            GitTransportError,
            RuntimeExecutionError, RuntimeUnavailable,
            subprocess.TimeoutExpired,
        ) as error:
            log.error(
                "Could not publish repository %s: %s", repository_name, error
            )
            return None

    def _publish_task(
        self,
        task: Task,
        repository: RepositoryConfig,
        transport: ControllerGitTransport,
        head: str,
    ) -> TaskId | None:
        """Collapse, integrate, gate and push exact retained task work.

        The landing commit names the work it lands (`Steward-Work`), so a push
        whose observation was lost is recognized as landed on the next pass
        and never reaches here again.
        """
        base = transport.sync_remote_to_agent(repository.path, self.broker)
        # A fresh machine may have only the accepted task graph.
        try:
            self.state.tasks.restore_work(task.task_id, repository, self.broker)
        except TaskWorkConflict as error:
            outcome = self.state.tasks.request_repair(task.task_id, transport,
                task.work_sha, base, ValidationFailure(str(error)))
            self.last_outcome[task.repository] = f"{task.task_id}: {outcome}"
            return None
        prepared = PromotionEngine(
            repository,
            self.worktrees_root,
            transport=transport,
            execution_broker=self.broker,
        ).prepare(task.branch, head, base)
        # The candidate ref keeps imported objects alive only for this pass: a
        # repair has copied them into the task store, a push onto the remote,
        # and a later pass imports its own candidate again.
        candidate = getattr(prepared, "tested_sha", None) or getattr(prepared, "candidate", None)
        try:
            if isinstance(prepared, ValidationFailure):
                outcome = self.state.tasks.request_repair(task.task_id, transport, head, base, prepared)
                self.last_outcome[task.repository] = f"{task.task_id}: {outcome}: {prepared.reason}"
                return None

            if repository.requires:
                if self.procedures is None:
                    raise RuntimeError("publication procedure service is unavailable")
                evidence = self.procedures.require(repository.requires, task.repository,
                                                   prepared.tested_sha, prepared.base_sha)
                if evidence is None:
                    self.last_outcome[task.repository] = f"{task.task_id}: awaiting required procedure evidence"
                    return None
                if evidence:
                    failure = ValidationFailure(evidence, candidate=prepared.tested_sha)
                    outcome = self.state.tasks.request_repair(task.task_id, transport, head, base, failure)
                    self.last_outcome[task.repository] = f"{task.task_id}: {outcome}"
                    return None

            # The short decision lease orders a withdrawal against the push. Native
            # work never holds this lock. A push already in progress wins that order.
            with self.state.tasks.lease:
                current = self.state.tasks.get(task.task_id)
                if current.definition.hold or current.dispatchable:
                    return None
                landed = publish(transport, prepared)
            if not landed:
                self.last_outcome[task.repository] = f"{task.task_id}: remote moved or refused; candidate will be revalidated"
                # The remote moved first, or refused. Both leave the branch exactly
                # as it was, which still says there is work to publish.
                return None
            self.last_outcome[task.repository] = f"{task.task_id}: published {prepared.tested_sha}"
            return task.task_id
        finally:
            if candidate:
                transport.drop_candidate(candidate)
