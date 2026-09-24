"""One controller dispatching task and repository owners onto the shared budget."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor

from .config.schema import ControllerConfig
from .repository_reconciler import RepositoryReconciler
from .state import StateDatabase
from .task_runner import TaskRunner
from .task_query import is_task_query
from .lease import Busy, Lease

# What a lane yields: who the work belongs to, and the work.
Owner = tuple[Hashable, Callable[[], object]]


def repository_lease(state: StateDatabase, repository: str) -> Lease:
    """The one exclusion over a repository: whoever holds it is its driver.

    Every caller that acts on a repository takes this, and it is the only
    thing keeping two of them apart. `promotions.status='running'` was a copy
    of it, and so was the dispatch set that used to stand beside it; both
    excluded a second *owner* while leaving a second *driver* of the same
    owner perfectly possible.
    """
    return Lease(
        state.path.parent,
        lock_name=f".{state.path.name}.{repository}.repository.lock",
        timeout_seconds=0,
    )


class Dispatch:
    """One budget of concurrent work, and the owners currently holding it.

    `controller.workers` is the whole scheduling policy. Everything the
    steward schedules for itself — task slices, repository publication,
    rhythms, desk messages, probes, result delivery — competes for the same
    slots, and the executor's own queue is the assignment: first asked, first
    served. There is no per-lane reservation and no fairness ordering, because
    both are a scheduler, and a scheduler is the thing this row deletes.

    A live conversation never comes here. The operator talking to their
    steward runs on the ingress thread that received them, so the budget can
    be full and they still get an answer.

    The dict is deliberately not an exclusion. `task_lock` and
    `repository_lease` are, and unlike a dict they are durable, so they hold
    across the crash that would strand an in-memory claim. It exists only so
    a pass does not enqueue a second copy of a job already queued, which would
    grow the backlog by one entry per poll. With it the backlog holds at most
    one entry per owner, so nobody waits behind a duplicate of themselves.
    """

    def __init__(self, workers: int) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="steward"
        )
        self._inflight: dict[Hashable, Future] = {}

    def reap(self) -> None:
        """Free finished slots, re-raising a worker's exception in the caller.

        This is the whole of fault propagation. A fault leaves the pass, then
        `run_forever`, then the process, and the service manager restarts it.
        """
        for key in [key for key, job in self._inflight.items() if job.done()]:
            self._inflight.pop(key).result()

    def submit(self, key: Hashable, work: Callable[[], object]) -> None:
        if key not in self._inflight:
            self._inflight[key] = self._pool.submit(work)

    def stop(self) -> None:
        # Queued owners remain derivable from durable state after restart.
        # Drain writers that started; shutdown must not start another one.
        self._pool.shutdown(wait=True, cancel_futures=True)


class StewardKernel:
    """Dispatch existing durable work; exclusivity follows the affected owner."""

    def __init__(
        self,
        state: StateDatabase,
        reconciler: RepositoryReconciler,
        tasks: TaskRunner,
        *,
        dispatch: Dispatch | None = None,
    ) -> None:
        self.state = state
        self.reconciler = reconciler
        self.tasks = tasks
        # Shared with every other thing the steward schedules for itself.
        self.dispatch = dispatch or Dispatch(ControllerConfig().workers)

    def owners(self) -> Iterator[Owner]:
        """Every owner this kernel would advance right now, as work to run.

        A derivation, not a schedule. Pause is a filter on it rather than a
        branch around it: pausing stops the steward taking on work, and
        `/pause` says so — *accepted publication remains active* — so the
        repository half is not filtered.

        There is no recovery pass either. A task interrupted by a crash never
        left the queue and its lock died with its worker, so it is yielded
        again here like any other, and `TaskRunner.prepare` resumes the turn
        it finds open.

        Repositories are yielded unfiltered. The filter that used to stand
        here refreshed the whole task-status index to decide which ones were
        worth locking, while a second loop did the Git work for every
        repository on every poll regardless; `_advance_repository` already
        returns having done nothing when nothing is owed.
        """
        if not self.state.paused():
            owed = (task.task_id for task in self.state.tasks.all()
                    if task.dispatchable or is_task_query(task.status.value, task.reason))
            for task_id in owed:
                yield ("task", task_id), lambda task_id=task_id: self.tasks.prepare(
                    task_id
                )

        for repository in self.reconciler.repositories:
            yield (
                ("repository", repository),
                lambda repository=repository: self._advance_repository(repository),
            )

    def _advance_repository(self, repository: str) -> None:
        """Own the repository, then publish whatever it owes, then release it.

        The lock is held across the gate run, for the minutes that takes,
        because running the gates is what owning a repository is *for*, and the
        kernel releases it when its holder dies.

        Deployment follows publication under the same hold, and nothing is
        handed off between them: it is a derivation over the tip publication
        may just have moved, so a pass that publishes and then dies loses
        nothing the next one cannot observe for itself.
        """
        try:
            with repository_lease(self.state, repository):
                self.reconciler.publish_one(repository)
        except Busy:
            # Someone else has it. Nothing to queue: the work stays durable in
            # the branches and the next pass asks again.
            return

    def stop(self) -> None:
        self.dispatch.stop()
