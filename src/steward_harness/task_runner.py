"""Task cognition runs as a session turn; accepted work enters publication."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from steward_harness.cognition import Cognition, CognitionRequest
from steward_harness.config.schema import RepositoryConfig
from steward_harness.git import run_agent_git, hardened_git_argv
from steward_harness.git_transport import ControllerGitTransport, GitTransportError
from steward_harness.landing.checkpoint import (
    TickClosure,
    WorktreeCheckpointer,
    WorktreeCheckpointError,
    commit_subject,
    parse_tick_closure,
)
from steward_harness.landing.worktree import WorktreeError, WorktreeManager
from steward_harness.prompts import build_task_prompt, build_procedure_scope
from steward_harness.provider_types import ProviderFamily
from steward_harness.runtime.contracts import (
    RuntimeExecutionError, RuntimeInput, RuntimeInputResult, RuntimeUnavailable,
)
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.process import ProcessTimeout
from steward_harness.state import (
    CheckpointDisposition,
    ConversationBusy,
    StateDatabase,
    TaskId,
)
from steward_harness.task_query import is_task_query, ownership_answer
from steward_harness.task_lock import task_lock
from steward_harness.lease import Busy
from steward_harness.task_store import Task
from steward_harness.task_store import PREFIX, OfferRejected

log = logging.getLogger(__name__)

#: The one task-local ref a native parent points at its current account offer.
OFFER_REF = "refs/steward/understanding/{task_id}"
OFFER_LIMIT = 256 * 1024
_OFFER = re.compile(r"\ASteward-Base: ([0-9a-f]{40})\n\n(.*)\Z", re.S)
_REPLY_LIMIT = 100_000
# Runs as the execution identity: read at most limit + 1 bytes of one object,
# so an oversized or swapped object cannot make the controller read without bound.
_READ_OFFER = """
import subprocess, sys
limit = int(sys.argv[1])
with subprocess.Popen(sys.argv[2:], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as git:
    data = git.stdout.read(limit + 1)
    if len(data) > limit:
        git.kill()
    code = git.wait()
sys.stdout.buffer.write(data)
sys.exit(0 if len(data) > limit else code)
"""


@dataclass(eq=False)
class _LiveTask:
    """One running native turn: its input channel and accepted account baseline.

    ``baseline`` is the accepted revision whose body the native account was
    last reconciled with: the slice's opening, then each account it offered
    and had accepted. Closure reconciles against it, so an accepted offer is
    the owner's own earlier word rather than a concurrent edit.
    """

    worktree: Path
    baseline: str
    consumed: set[str]
    attempted: set[str]
    send: Callable[[RuntimeInput], None] | None = None
    # Offer replies, by offer: delivered (answered, or reported while its decision
    # is still pending), waiting to be (re)sent, or sent and awaiting the provider.
    answered: set[str] = field(default_factory=set)
    reported: set[str] = field(default_factory=set)
    outbox: dict[str, tuple[str, bool]] = field(default_factory=dict)
    sent: dict[str, tuple[str, str, bool]] = field(default_factory=dict)
    acknowledgements: set[str] = field(default_factory=set)
    ended: threading.Event = field(default_factory=threading.Event)


def _task_input(commit: str, kind: str, text: str, source: str) -> RuntimeInput:
    """Derive producer from the accepted input commit, never from its prose."""
    if source == "operator":
        origin, author = "operator", "operator"
    elif source.startswith("turn_"):
        origin, author = "controller", f"assistant:{source}"
    elif source.startswith("controller:"):
        origin, author = "controller", source
    else:
        origin, author = "controller", f"unverified:{source or 'unknown'}"
    return RuntimeInput(commit, f"{kind}: {text}", origin=origin, author=author)
def session_state_prefixes(family: ProviderFamily) -> tuple[str, ...]:
    """Worktree paths the running provider writes its own session state into.

    Each adapter maps its native session directory *inside* the worktree — see
    the `mappings=` argument where the providers open a `native_workspace` — and
    that is deliberate: it is what makes a session durable, resumable and
    reviewable across invocation loss and later recovery.

    The consequence is that these paths change on every turn, a read-only one
    included. A reviewer reading a repository necessarily appends its own
    transcript to that repository, so treating the change as evidence of
    tampering makes a read-only procedure unsatisfiable by retry rather than
    catching anything. Only these paths are excused, and only for the family
    that actually ran.
    """
    return (f"artefacts/{family}/", f"memories/{family}/")


def _without_session_state(status: str, prefixes: tuple[str, ...]) -> str:
    """Drop `git status --porcelain=v1` rows for paths the provider owns."""
    kept = []
    for row in status.splitlines():
        path = row[3:] if len(row) > 3 else ""
        # A rename reports `orig -> path`; the destination is what was written.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if not path.strip().strip('"').startswith(prefixes):
            kept.append(row)
    return "\n".join(kept)


class TaskRunner:
    """Run isolated task cognition; the commit it makes is the whole slice."""

    def __init__(
        self,
        *,
        state: StateDatabase,
        repositories: Mapping[str, RepositoryConfig],
        transports: Mapping[str, ControllerGitTransport],
        worktrees_root: Path,
        broker: UntrustedExecutionBroker,
        cognition: Cognition,
        provider_fallbacks: tuple[ProviderFamily, ...],
        timeout_seconds: int,
        actor_name: str = "Steward",
        actor_email: str = "steward@localhost",
        poll_seconds: float = 5,
    ) -> None:
        self.state = state
        self.repositories = dict(repositories)
        self.transports = dict(transports)
        self.worktrees_root = worktrees_root.resolve()
        state.tasks.transports = self.transports
        self.broker = broker
        self.cognition = cognition
        self.provider_fallbacks = provider_fallbacks
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.actor_name = actor_name
        self.actor_email = actor_email
        self._input_lock = threading.RLock()
        self._native_inputs: dict[TaskId, _LiveTask] = {}
        self._stopping = threading.Event()
        self._executions: set[str] = set()

    def interrupt_running(self) -> None:
        """Stop every running task turn cooperatively, for controller shutdown.

        Task turns have no routine deadline, so shutdown must ask them to end.
        Each is interrupted natively, then contained after the bounded grace,
        and its work is retained as a continuation rather than a blocked task.
        """
        self._stopping.set()
        with self._input_lock:
            running = tuple(self._executions)
        for execution_id in running:
            self.cognition.cancel(execution_id)

    def flush_inputs(self) -> None:
        """Offer newly persisted notes to each running native task session."""
        with self._input_lock:
            active = tuple(self._native_inputs.items())
        for task_id, live in active:
            for source_id, kind, text, source in self.state.tasks.get(task_id).pending:
                with self._input_lock:
                    if self._native_inputs.get(task_id) is not live:
                        break
                    if source_id in live.attempted:
                        continue
                    live.attempted.add(source_id)
                try:
                    live.send(_task_input(source_id, kind, text, source))
                except Exception:
                    # A disappearing native turn is not proof of consumption.
                    # Leave its persisted message for the next task slice.
                    log.exception("live task input failed for %s", task_id)

    def _watch_offers(self, task_id: TaskId, live: _LiveTask) -> None:
        """Observe this turn's offer ref until the turn ends.

        It lives exactly as long as the turn, off the daemon's loop, so a slow
        or hostile untrusted repository can delay only its own task's offers.
        """
        while not live.ended.wait(self.poll_seconds):
            if live.send is None:
                continue
            try:
                self._accept_offer(task_id, live)
            except Exception:
                log.exception("understanding offer check failed for %s", task_id)

    def _accept_offer(self, task_id: TaskId, live: _LiveTask) -> None:
        """Accept the account a running native parent offered, and say so.

        Acceptance is the Git decision; the reply only names it. Every offer
        gets a reply. An undelivered reply is sent again without deciding
        again. An offer that could not be evaluated is reported once and then
        evaluated again each poll until it has a decision.
        """
        found = run_agent_git(self.broker, "rev-parse", "--verify", "--quiet",
                              OFFER_REF.format(task_id=task_id), cwd=live.worktree, timeout=30)
        offer = found.stdout.strip()
        with self._input_lock:
            if found.returncode or offer in live.answered or any(
                    offer == sent for sent, _, _ in live.sent.values()):
                return
            pending = live.outbox.pop(offer, None)
        reply, decided = pending or self._decide_offer(task_id, live, offer)
        with self._input_lock:
            if not decided and offer in live.reported:
                return
            # Each reply is its own source: adapters refuse a repeated source ID.
            source_id = f"understanding-{offer}-{len(live.acknowledgements)}"
            live.acknowledgements.add(source_id)
            live.sent[source_id] = (offer, reply, decided)
        try:
            live.send(RuntimeInput(source_id, reply, origin="controller", author="steward"))
        except Exception:
            self._offer_reply_result(live, RuntimeInputResult(source_id, "rejected"))
            raise

    def _offer_reply_result(self, live: _LiveTask, result: RuntimeInputResult) -> None:
        """Only the provider's acceptance delivers a reply; otherwise send it again."""
        with self._input_lock:
            sent = live.sent.pop(result.source_id, None)
            if sent is None:
                return
            offer, reply, decided = sent
            if result.disposition == "accepted":
                (live.answered if decided else live.reported).add(offer)
            else:
                live.outbox[offer] = (reply, decided)

    def _decide_offer(self, task_id: TaskId, live: _LiveTask, offer: str) -> tuple[str, bool]:
        """Return the reply to one offer and whether it records a decision."""
        tasks = self.state.tasks
        try:
            if not re.fullmatch(r"[0-9a-f]{40}", offer):
                raise OfferRejected("an offer must name one Git object")
            base, body = self._read_offer(live.worktree, offer)
            accepted, created = tasks.accept_understanding(task_id, offer, base, body)
        except OfferRejected as error:
            log.warning("task %s understanding offer %s rejected: %s", task_id, offer, error)
            reply = f"Steward did not accept understanding offer {offer}: {error}. Nothing changed."
            if error.revision:
                if error.changes:
                    reply += "\n\n" + error.changes[:_REPLY_LIMIT]
                    if len(error.changes) > _REPLY_LIMIT:
                        reply += "\n\n(truncated)"
                reply += ("\n\nReconcile this, then offer again with "
                          f"Steward-Base: {error.revision}.")
            return reply, True
        except Exception as error:
            log.exception("task %s understanding offer %s could not be evaluated", task_id, offer)
            return (f"Steward could not evaluate understanding offer {offer}: "
                    f"{type(error).__name__}. Nothing was accepted yet; Steward will keep "
                    "evaluating this offer and reply when it has a decision."), False
        current = tasks.refs().get(PREFIX + str(task_id))
        if created:
            live.baseline = accepted
        return (f"Steward accepted understanding offer {offer} as accepted revision {accepted}. "
                + ("" if current == accepted else f"The current accepted revision is {current}. ")
                + f"Use Steward-Base: {current} for your next offer."), True

    def _read_offer(self, worktree: Path, offer: str) -> tuple[str, str]:
        """Read one bounded offer and prove it is the blob its ID names."""
        read = self.broker.run(
            [self.broker.python_executable, "-I", "-c", _READ_OFFER, str(OFFER_LIMIT),
             *hardened_git_argv("cat-file", "blob", offer)],
            cwd=worktree, timeout=30, text=False)
        raw = read.stdout
        if read.returncode:
            raise OfferRejected("an offer must be a blob in this repository")
        if len(raw) > OFFER_LIMIT:
            raise OfferRejected(f"an offer must be at most {OFFER_LIMIT} bytes")
        if hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest() != offer:
            raise OfferRejected("the offer's content does not match its object ID")
        try:
            match = _OFFER.match(raw.decode("utf-8"))
        except UnicodeDecodeError:
            match = None
        if match is None or not match.group(2).strip():
            raise OfferRejected("an offer is UTF-8 'Steward-Base: <accepted revision>', a blank "
                                "line, then the nonblank account body")
        if re.match(r"---\n.*?\n---\n", match.group(2).lstrip(), re.S):
            raise OfferRejected("task authority is not offerable; offer the account body "
                                "without frontmatter")
        return match.group(1), match.group(2).strip()

    def prepare(self, task_id: TaskId) -> TaskId:
        """Hold this task's lock and run one execution slice.

        The lock *is* the task's `running` state, so it is taken before any
        work begins and released only when the slice is over. It also folds
        away recovery: there is no half-open slice to find, because a slice
        that died left no record at all — its branch simply has no new commit,
        and the task never left the queue.
        """
        task = self.state.tasks.get(task_id)
        repository = self.repositories.get(task.repository)
        transport = self.transports.get(task.repository)
        if repository is None or transport is None:
            if task.dispatchable:
                self.state.tasks.hold(task_id, "blocked", "managed repository is unavailable")
            return task_id
        lock = task_lock(self.state.tasks.locks_root, task_id)
        try:
            lock.acquire()
        except Busy:
            return task_id
        try:
            self._answer_query(task_id)
            task = self.state.tasks.get(task_id)
            if not task.dispatchable:
                return task_id
            return self._run_owned(task_id, task.revision)
        finally:
            lock.release()

    def _answer_query(self, task_id):
        # The caller holds the task lock, so the task reads as running here.
        task = self.state.tasks._record(task_id)
        if (task.disposition != "ask" or task.definition.hold or task.dispatchable
                or not is_task_query("waiting", task.reason)):
            return
        answer = ownership_answer(self.state.tasks, task_id, task.reason, self.repositories)
        try:
            self.state.tasks.answer(task_id, answer, source=f"controller:task-query:{task.revision}")
        except RuntimeError:
            # An operator answer or cancellation may win while the read runs.
            # It owns the task; a stale query must not reopen it.
            current = self.state.tasks.get(task_id)
            if is_task_query(current.status.value, current.reason):
                raise

    def reconcile_worktrees(self) -> None:
        """Retain task environments; prune only missing Git registrations."""
        for repository in self.repositories.values():
            try:
                run_agent_git(self.broker, "worktree", "prune", cwd=repository.path, timeout=60)
            except OSError as error:
                log.warning("repository workspace unavailable: %s", error)

    def _run_owned(self, task_id: TaskId, opened_at: str) -> TaskId:
        """Execute cognition for this slice; close it with its disposition."""
        task = self.state.tasks.get(task_id)
        repository = self.repositories[task.repository]
        try:
            if self.state.tasks.cancelled(task_id):
                self._autosave_and_interrupt(task, opened_at, "operator cancelled task")
                return task_id

            _manager, worktree, checkpointer = self._open_worktree(task, repository)
            procedure = task.procedure
            live = _LiveTask(worktree, opened_at, set(), set())
            disposition, detail, findings = self._work(
                task, worktree, checkpointer, live
            )
            if self.state.tasks.cancelled(task_id):
                # Withdrawn work is committed; its environment remains available
                # for inspection, including ignored files and native records.
                self._autosave_and_interrupt(task, opened_at, "operator cancelled task")
                return task_id

            # The slice already ended, durably, in the commit `_work` made —
            # `Disposition:` and all. Nothing is submitted anywhere: whether
            # this task now owes a publication is read back off that commit,
            # by whoever holds the repository next.
            work_sha = self._retain_work(task)
            self.state.tasks.finish_slice(
                task_id,
                work_sha=work_sha,
                disposition=disposition,
                opened_at=live.baseline,
                detail=detail or (
                    "ask" if disposition is CheckpointDisposition.ASK else None
                ),
                consumed_input_ids=frozenset(live.consumed),
            )
            self._answer_query(task_id)

        except (RuntimeExecutionError, RuntimeUnavailable) as error:
            # The adapter binds the native session mid-execution, so read the
            # lineage the execution ended on.
            session = self.state.get_conversation(task.session_id).provider_session_id
            if self.state.tasks.cancelled(task_id):
                # Commit what the cancelled invocation wrote and retain its
                # environment, as for every other task disposition.
                self._autosave_and_interrupt(task, opened_at, "operator cancelled task")
            elif self._stopping.is_set() or (
                isinstance(error, ProcessTimeout)
                and error.session_id is not None
                and error.session_id == session
            ):
                # The execution slice ended but the task did not: a procedure
                # deadline with a bound session, or controller shutdown. Its
                # owner continues on a later tick. Whatever it already wrote is
                # still committed here — a live session is not a reason to
                # leave work uncheckpointed.
                self._autosave(task)
                saved = self._retain_work(task)
                # A turn stopped before it wrote anything changed nothing: the
                # task is as dispatchable as it was, publication included.
                if saved and saved != task.work_sha:
                    self.state.tasks.change(task_id, lambda d: d.model_copy(update={
                        "work": saved, "resume": saved}),
                        message="retain interrupted native continuation\n\nThe native execution was interrupted before closure "
                                "with a resumable session; retained work needs another execution.",
                        parents=(saved,))
                return task_id
            else:
                self._autosave_and_interrupt(
                    task, opened_at, str(error) or type(error).__name__
                )
        except (WorktreeError, WorktreeCheckpointError, GitTransportError, OSError) as error:
            self._autosave_and_interrupt(task, opened_at, str(error))

        return task_id

    def _autosave(self, task: Task, reason: str | None = None) -> None:
        """Commit whatever an interrupted execution left in the task worktree."""
        repository = self.repositories.get(task.repository)
        if repository is None:
            return
        try:
            _manager, _worktree, checkpointer = self._open_worktree(task, repository)
            checkpointer.stage(expected_branch=task.branch)
            # Even an empty interrupted slice needs its own checkpoint: the
            # inherited main tip is not evidence that this task was published.
            checkpointer.commit(
                "steward: autosave interrupted task work",
                disposition=CheckpointDisposition.BLOCKED.value,
                reason=reason,
            )
        except Exception:
            pass

    def _autosave_and_interrupt(
        self, task: Task, opened_at: str, reason: str
    ) -> None:
        """Commit any uncommitted work, then close the slice as blocked."""
        self._autosave(task, reason)
        work_sha = self._retain_work(task)
        self.state.tasks.finish_slice(
            task.task_id,
            work_sha=work_sha,
            disposition=CheckpointDisposition.BLOCKED,
            opened_at=opened_at,
            detail=reason[:500] or "blocked",
        )

    def _work(
        self,
        task: Task,
        worktree: Path,
        checkpointer: WorktreeCheckpointer,
        live: _LiveTask,
    ) -> tuple[CheckpointDisposition, str | None, str]:
        """Run cognition and commit any changes; return (disposition, detail, findings)."""
        task = self.state.tasks.get(task.task_id)
        lineage = self.state.get_conversation(task.session_id)
        procedure = task.procedure
        brief = task.brief
        if procedure and procedure.instructions not in brief:
            brief += "\n\n## Accepted procedure\n" + procedure.instructions
        order = self._provider_order(lineage.provider)
        inputs = tuple(_task_input(*item) for item in task.pending)
        live.consumed.update(item.source_id for item in inputs)
        # Every accepted input, not only the pending ones: a saved session may
        # turn out missing, or cognition may fall back to another provider,
        # and either starts fresh from this prompt.
        operator_context = tuple(_task_input(*item).attributed_text for item in task.inputs)
        live.attempted.update(live.consumed)

        def input_ready(send: Callable[[RuntimeInput], None]) -> None:
            with self._input_lock:
                live.send = send
                self._native_inputs[task.task_id] = live

        def input_result(result: RuntimeInputResult) -> None:
            with self._input_lock:
                # Acknowledgements are the controller's words, not operator
                # input the checkpoint could consume.
                if result.source_id in live.acknowledgements:
                    self._offer_reply_result(live, result)
                elif result.disposition == "accepted":
                    live.consumed.add(result.source_id)
        execution_generation = lineage.generation
        execution_provider = lineage.provider

        def session_started(provider: str, session: str | None) -> None:
            nonlocal execution_generation, execution_provider
            try:
                lineage = self.state.bind_conversation_provider(
                    task.session_id, provider, session,
                    expected_generation=execution_generation,
                )
            except ConversationBusy:
                return
            execution_generation = lineage.generation
            execution_provider = lineage.provider

        def session_invalidated(provider: str) -> None:
            # Nothing left to resume under this provider: the same bind with
            # no session, which is what makes the generation move.
            nonlocal execution_generation
            try:
                lineage = self.state.bind_conversation_provider(
                    task.session_id, provider, None,
                    expected_generation=execution_generation,
                )
            except ConversationBusy:
                return
            else:
                execution_generation = lineage.generation

        # The slice has no identity of its own until it commits, and needs
        # none: this names the task, and the lock already refuses a second
        # slice of it, which is the only collision `execution_id` excludes.
        execution_id = str(task.session_id)
        before = None
        if procedure:
            from steward_harness.landing.gates import GateRunner
            if procedure.access == "read-only":
                changed = run_agent_git(self.broker, "diff", "--name-only", procedure.candidate,
                                        cwd=worktree, timeout=30)
                added = run_agent_git(self.broker, "ls-files", "--others", "--exclude-standard",
                                      cwd=worktree, timeout=30)
                if changed.returncode or added.returncode or (changed.stdout + added.stdout).strip():
                    raise RuntimeExecutionError("read-only review workspace differs from its exact candidate")
            before = GateRunner._snapshot(worktree, self.broker)
        procedure_prompt = build_procedure_scope(
            candidate=procedure.candidate,
            base=procedure.base,
            read_only=procedure.access == "read-only",
            full_tree=not procedure.event,
            verdict=not procedure.event,
        ) if procedure else ""
        execution_cwd = Path(procedure.workdir) if procedure and procedure.workdir else worktree
        if procedure and procedure.workdir:
            # Organisation evidence is read from existing sibling repositories.
            # Refresh their observed refs through the credential-free transfer;
            # leave every working HEAD, index and local change in place.
            for name, transport in self.transports.items():
                transport.sync_remote_to_agent(self.repositories[name].path, self.broker)
            procedure_prompt = (
                "Reflect across the organisation from this root directory, with repositories beneath it as siblings. "
                "Discover relevant evidence through their files and Git history. "
                "Return findings and missing evidence, not a gate verdict."
            )
        request = CognitionRequest(
            execution_id=execution_id,
            profile=lineage.profile,  # type: ignore[arg-type]
            prompt=build_task_prompt(
                task.title,
                brief,
                procedure_scope=procedure_prompt,
                repository=task.repository,
                event_id=execution_id,
                operator_context=operator_context,
                read_only=bool(procedure and procedure.access == "read-only"),
                understanding=None if procedure else (
                    OFFER_REF.format(task_id=task.task_id), live.baseline),
            ),
            cwd=execution_cwd,
            # Task cognition may work for days. A procedure run is a
            # finite review and keeps the provider deadline.
            timeout_seconds=self.timeout_seconds if procedure else None,
            provider_order=(procedure.provider,) if procedure else order,
            model=procedure.model if procedure else None,
            provider_session_id=lineage.provider_session_id,
            session_provider=(
                lineage.provider if lineage.provider_session_id is not None else None
            ),
            sandbox_mode=procedure.access if procedure else "workspace-write",
            on_session_started=session_started,
            on_session_invalidated=session_invalidated,
            on_input_ready=input_ready,
            on_input_result=input_result,
        )

        def prepared() -> CognitionRequest:
            # Registered with cognition before this runs, so a shutdown either
            # stops the turn through cancellation or is seen here first.
            if self._stopping.is_set():
                raise RuntimeExecutionError("controller is stopping")
            return request

        watcher = None if procedure else threading.Thread(
            target=self._watch_offers, args=(task.task_id, live),
            name=f"offers-{task.task_id}", daemon=True)
        with self._input_lock:
            self._executions.add(execution_id)
        try:
            if watcher:
                watcher.start()
            result = self.cognition.run(prepared, execution_id=execution_id)
        finally:
            with self._input_lock:
                self._executions.discard(execution_id)
                self._native_inputs.pop(task.task_id, None)
            # Wait out an acceptance in flight; none can follow, so closure
            # reconciles against the last accepted account.
            live.ended.set()
            if watcher:
                watcher.join()
        if procedure and procedure.access == "read-only":
            after = GateRunner._snapshot(worktree, self.broker)
            if not isinstance(before, tuple) or not isinstance(after, tuple):
                raise RuntimeExecutionError("read-only procedure changed its input; evidence rejected")
            owned = session_state_prefixes(result.resolved.provider)
            if before[0] != after[0] or (
                _without_session_state(before[1], owned) != _without_session_state(after[1], owned)
            ):
                raise RuntimeExecutionError("read-only procedure changed its input; evidence rejected")
        session_started(result.resolved.provider, result.provider_session_id)

        closure_error = None
        try:
            closure = parse_tick_closure(result.output, task.title)
        except ValueError as error:
            closure_error = str(error)
            closure = TickClosure(commit_subject(None, task.title), "continue")

        # Staging rejects an unfinished Git operation or a switched branch.
        checkpointer.stage(expected_branch=task.branch)
        # Every slice commits, a findings-only one included: the tip of a task
        # branch is always the harness's own, so its trailers and message are
        # always the ones this slice ended on.
        checkpointer.commit(
            closure.subject,
            disposition=closure.disposition,
            reason=closure.blocking_question,
            findings=closure.findings,
        )
        log.info("Task %s checkpoint: %s", task.task_id, closure.subject)

        if closure_error is not None:
            raise RuntimeExecutionError(f"Invalid task closure; work retained: {closure_error}")

        disposition = CheckpointDisposition(closure.disposition)
        return disposition, closure.blocking_question, closure.findings

    def _open_worktree(
        self,
        task: Task,
        repository: RepositoryConfig,
    ) -> tuple[WorktreeManager, Path, WorktreeCheckpointer]:
        """Check out the task's branch: retained work, else a new one at its base."""
        manager = WorktreeManager(
            repository.path,
            self.worktrees_root,
            execution_broker=self.broker,
        )
        base = None
        if not manager.has_branch(task.branch):
            transport = self.transports[task.repository]
            base = transport.sync_remote_to_agent(repository.path, self.broker)
            procedure = task.procedure
            if procedure:
                base = procedure.candidate
                self.state.tasks.push_to_agent(repository, self.broker,
                    f"{base}:refs/steward/procedure-inputs/{task.task_id}")
            if task.work_sha:
                self.state.tasks.restore_work(task.task_id, repository, self.broker)
                base = None
        worktree = manager.create_worktree(
            str(task.task_id), branch_name=task.branch, base_sha=base
        )
        checkpointer = WorktreeCheckpointer(
            worktree,
            actor_name=self.actor_name,
            actor_email=self.actor_email,
            execution_broker=self.broker,
        )
        return manager, worktree, checkpointer

    def _provider_order(self, primary: str) -> tuple[ProviderFamily, ...]:
        return (
            primary,  # type: ignore[return-value]
            *(provider for provider in self.provider_fallbacks if provider != primary),
        )

    def _retain_work(self, task):
        repository = self.repositories[task.repository]
        try:
            result = run_agent_git(self.broker, "rev-parse", "--verify", f"refs/heads/{task.branch}",
                                   cwd=repository.path, timeout=60)
        except OSError:
            return task.work_sha
        if result.returncode:
            return task.work_sha
        sha = result.stdout.strip()
        self.state.tasks.retain_work(repository, self.broker, sha)
        return sha
