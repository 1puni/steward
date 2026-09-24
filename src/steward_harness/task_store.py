"""Accepted task documents in a controller-private Git repository.

Only this repository's refs confer admission. Product worktrees cannot write it.
The document is the request; its commits are the decisions and inputs; second
parents retain work. A task's status is derived here, once, from that graph,
the slice commit it retains, the observed remote and the live task locks.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from steward_harness.git import hardened_git_argv, validate_git_remote_url, validate_object_id
from steward_harness.state import (
    CheckpointDisposition, ConversationId, TaskCheckpointSummary, TaskId, TaskOriginKind,
    TaskSpec, TaskStatus,
)
from steward_harness.task_lock import locked_tasks, task_lock
from steward_harness.config.schema import ProcedureConfig, _require_bounded_absolute
from steward_harness.lease import Busy, Lease

PREFIX = "refs/heads/tasks/"
ZERO = "0" * 40


class TaskWorkConflict(RuntimeError):
    """Local unaccepted work must be retained for explicit reconciliation."""


class OfferRejected(ValueError):
    """A native account offer the controller did not accept; nothing changed.

    A stale offer carries the current revision and the accepted text its owner
    has not seen, so reconciling it cannot silently drop an operator's edit.
    """

    def __init__(self, reason, *, revision=None, changes=""):
        super().__init__(reason)
        self.revision = revision
        self.changes = changes


class ProcedureRun(ProcedureConfig):
    name: str
    event: str = ""
    identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate: str = Field(pattern=r"^[0-9a-f]{40}$")
    base: str = Field(pattern=r"^[0-9a-f]{40}$")
    activity: dict[str, str] | None = None
    workdir: str | None = None

    @model_validator(mode="after")
    def validates_workdir(self):
        if self.workdir is not None:
            _require_bounded_absolute("rhythm workdir", self.workdir)
            if not self.event.startswith("rhythm:") or self.access != "read-only":
                raise ValueError("organisation workdir requires a read-only rhythm")
        return self


class Repair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base: str = Field(pattern=r"^[0-9a-f]{40}$")
    work: str = Field(pattern=r"^[0-9a-f]{40}$")
    attempts: int = Field(ge=1)
    reason: str


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repair: Repair | None = None
    procedure: ProcedureRun | None = None
    version: int = Field(default=1, ge=1, le=1)
    repository: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    origin: TaskOriginKind = TaskOriginKind.CONVERSATION
    source: str | None = None
    owner: str | None = None
    priority: int = Field(default=0, ge=-100, le=100)
    hold: str | None = Field(default=None, pattern=r"^(proposed|blocked|cancelled)$")
    reason: str | None = None
    work: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    resume: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")




def _format(*fields: str) -> str:
    """A log format ending every field in NUL, which no commit message can contain."""
    return "--format=" + "".join(f"{field}%x00" for field in fields)


def _records(output: str, width: int) -> list[list[str]]:
    values = output.split("\0")[:-1]
    return [[value.lstrip("\n") if offset == 0 else value
             for offset, value in enumerate(values[start:start + width])]
            for start in range(0, len(values), width)]

_CLOSING = ("checkpoint ", "block task", "cancel task", "reject proposal")
_INPUT = tuple[str, str, str, str]  # commit, kind, text, source


@dataclass(frozen=True, slots=True)
class Task:
    """One accepted task at one revision, with what was observed about it."""

    task_id: TaskId
    revision: str
    definition: Definition
    brief: str
    created_at: str
    updated_at: str
    #: Every accepted input, oldest first, and the ones no slice has consumed.
    inputs: tuple[_INPUT, ...]
    pending: tuple[_INPUT, ...]
    #: The commit that closed the task's latest outcome, for result identity.
    outcome: str
    #: The retained slice commit's trailers and message body.
    disposition: str | None = None
    slice_reason: str | None = None
    findings: str | None = None
    landed: str | None = None
    running: bool = False

    repository = property(lambda self: self.definition.repository)
    title = property(lambda self: self.definition.title)
    priority = property(lambda self: self.definition.priority)
    origin_kind = property(lambda self: self.definition.origin)
    owner = property(lambda self: self.definition.owner)
    work_sha = property(lambda self: self.definition.work)
    procedure = property(lambda self: self.definition.procedure)
    branch = property(lambda self: f"tasks/{self.task_id}")
    session_id = property(lambda self: ConversationId.for_task(self.task_id))

    @property
    def origin_ref(self) -> str | None:
        d = self.definition
        return None if d.origin is TaskOriginKind.CONVERSATION else (d.source or "").removeprefix(d.origin.value + ":")

    @property
    def read_only(self) -> bool:
        return bool(self.procedure and self.procedure.access == "read-only")

    @property
    def verdict(self) -> str | None:
        """A finished candidate review's pass or fail, read off its findings."""
        p = self.procedure
        if not p or p.access != "read-only" or p.workdir is not None or self.disposition != "idle":
            return None
        verdicts = [line.strip() for line in (self.findings or "").splitlines()
                    if line.startswith("VERDICT:")]
        return "pass" if verdicts == ["VERDICT: PASS"] else "fail"

    @property
    def dispatchable(self) -> bool:
        """Owes another execution slice."""
        d = self.definition
        return not d.hold and (
            d.work is None or d.resume == d.work or self.disposition == "continue"
            or (self.disposition == "idle" and bool(self.pending)))

    @property
    def status(self) -> TaskStatus:
        if self.landed:
            return TaskStatus.DONE
        if self.running:
            return TaskStatus.RUNNING
        if self.definition.hold:
            return TaskStatus(self.definition.hold)
        if self.dispatchable:
            return TaskStatus.QUEUED
        if self.read_only and self.disposition == "idle":
            return TaskStatus.DONE
        return {"ask": TaskStatus.WAITING, "blocked": TaskStatus.BLOCKED,
                "idle": TaskStatus.RUNNING}.get(self.disposition, TaskStatus.QUEUED)

    @property
    def reason(self) -> str | None:
        """A decision's written reason, else the last slice's `Reason` trailer."""
        return self.definition.reason or self.slice_reason

    @property
    def publishable(self) -> bool:
        """Finished work the default branch does not have yet."""
        return (not self.read_only and not self.landed and not self.definition.hold
                and not self.dispatchable and self.disposition == "idle")

    @property
    def tip(self) -> str | None:
        """The commit that landed this task's work, else the retained work."""
        return self.landed or self.definition.work

class GitTaskStore:
    def __init__(self, path: Path, *, create: bool = True):
        self.path = path
        # One record per task, replaced when its accepted tip moves.
        self._records: dict[str, Task] = {}
        self.remote: str | None = None
        self.repositories: set[str] | None = None
        self.transports = {}
        # Each task's execution lock, beside the store it belongs to.
        self.locks_root = path.with_name(path.name + ".locks")
        self.default_provider = "codex"
        self.default_profile = "balanced"
        self.lease = Lease(
            path.parent, lock_name=f".{path.name}.lock", require_existing=not create,
        )
        if path.is_symlink():
            raise ValueError("task store must not be a symlink")
        if create:
            with self.lease:
                if not path.exists():
                    path.mkdir(mode=0o700)
                    self.git("init", "--bare")
        metadata = path.stat()
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise PermissionError("task store must be controller-private")

    def git(self, *args: str, input_text: str | None = None) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_AUTHOR_NAME="Steward", GIT_AUTHOR_EMAIL="steward@localhost",
                   GIT_COMMITTER_NAME="Steward", GIT_COMMITTER_EMAIL="steward@localhost",
                   GIT_NO_LAZY_FETCH="1")
        result = subprocess.run(
            hardened_git_argv("-c", "core.fsync=committed", "-c", "commit.gpgsign=false", f"--git-dir={self.path}", *args),
            env=env, input=input_text, text=True, capture_output=True,
            timeout=120, umask=0o077,
        )
        if result.returncode:
            raise RuntimeError(f"task Git {args[0]} failed (exit {result.returncode})")
        return result.stdout.strip()

    def refs(self, prefix: str = PREFIX) -> dict[str, str]:
        return dict(line.split() for line in self.git(
            "for-each-ref", "--format=%(refname) %(objectname)", prefix,
        ).splitlines())

    def read(self, task_id: TaskId, sha=None) -> tuple[str, Definition, str]:
        task = self._record(task_id, sha)
        return task.revision, task.definition.model_copy(deep=True), task.brief

    def _record(self, task_id: TaskId, sha=None) -> Task:
        sha = sha or self.refs().get(PREFIX + str(task_id))
        if sha is None:
            raise LookupError(f"unknown task {task_id}")
        validate_object_id(sha)
        task = self._records.get(str(task_id))
        if task is None or task.revision != sha:
            task = self._records[str(task_id)] = self._load(task_id, sha)
        # Authority may change even when the immutable Git document did not.
        if self.repositories is not None and task.repository not in self.repositories:
            raise PermissionError(f"task {task_id} names an unauthorized repository")
        return task

    def _load(self, task_id: TaskId, sha: str) -> Task:
        """Everything an accepted revision determines; immutable for its SHA."""
        text = self.git("show", f"{sha}:task.md")
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            raise ValueError(f"task {task_id} needs a frontmatter document")
        header, body = text[4:].split("\n---\n", 1)
        definition = Definition.model_validate(yaml.safe_load(header))
        if not body.strip():
            raise ValueError(f"task {task_id} has no durable request")
        if definition.owner is not None and ConversationId(definition.owner).owner_kind != "conversation":
            raise ValueError("task reply owner must be a conversation")
        if definition.work:
            self.git("cat-file", "-e", f"{definition.work}^{{commit}}")
            if not self.contains(definition.work, sha):
                raise ValueError("task work must be retained in its graph")
        history = self.git("log", "--first-parent", "--reverse", _format(
            "%H", "%cI", "%s", "%(trailers:key=Steward-Input,valueonly)",
            "%(trailers:key=Steward-Consumed,valueonly)", "%(trailers:key=Steward-Source,valueonly)", "%B",
        ), *self._history(sha, definition))
        inputs, pending, stamps, outcome = [], {}, [], sha
        for commit, stamp, subject, kind, consumed, source, message in _records(history, 7):
            stamps.append(stamp)
            if subject.startswith(_CLOSING):
                outcome = commit
            for key in consumed.split():
                pending.pop(key, None)
            if kind.strip():
                text = message.partition("\n\n")[2].rpartition("\n\nSteward-Input:")[0]
                pending[commit] = (commit, kind.strip(), text, source.strip())
                inputs.append(pending[commit])
        task = Task(task_id, sha, definition, body.strip(), stamps[0], stamps[-1],
                    tuple(inputs), tuple(pending.values()), outcome)
        if not definition.work:
            return task
        [(disposition, reason, message)] = _records(self.git("show", "-s", _format(
            "%(trailers:key=Disposition,valueonly)", "%(trailers:key=Reason,valueonly)", "%B",
        ), definition.work), 3)
        # Subject, findings, trailers: drop the first paragraph, and the last
        # one only if it is ours. A slice that concluded nothing has two.
        paragraphs = message.partition("\n")[2].strip().split("\n\n")
        if paragraphs and all(line.startswith(("Disposition:", "Reason:"))
                              for line in paragraphs[-1].strip().splitlines() if line.strip()):
            paragraphs.pop()
        return replace(task, disposition=disposition.strip() or None,
                       slice_reason=reason.strip() or None,
                       findings="\n\n".join(paragraphs).strip() or None)

    def get(self, task_id: TaskId) -> Task:
        return self._observe([self._record(task_id)])[0]

    def all(self) -> list[Task]:
        tasks = self._observe([self._record(TaskId(ref.removeprefix(PREFIX)), sha)
                               for ref, sha in self.refs().items()])
        return sorted(tasks, key=lambda t: (-t.priority, t.created_at, str(t.task_id)))

    def queued(self) -> tuple[TaskId, ...]:
        return tuple(task.task_id for task in self.all() if task.dispatchable)

    def _observe(self, tasks: list[Task]) -> list[Task]:
        """Overlay the facts the accepted graph cannot hold: locks and landing."""
        running = locked_tasks(self.locks_root)
        tips: dict[str, tuple | None] = {}
        observed = []
        for task in tasks:
            transport, landed = self.transports.get(task.repository), None
            if transport is not None and task.work_sha:
                if task.repository not in tips:
                    tips[task.repository] = transport.observed_tip()
                if tips[task.repository]:
                    landed = transport.landing(task.work_sha, tips[task.repository][0])
            observed.append(replace(task, landed=landed, running=str(task.task_id) in running))
        return observed

    def checkpoints(self, task: Task, limit: int) -> tuple[TaskCheckpointSummary, ...]:
        """The task's slices, newest first; a commit without the trailer is skipped."""
        if not task.work_sha:
            return ()
        records = self.git("log", f"--max-count={limit}", _format(
            "%cI", "%(trailers:key=Disposition,valueonly,separator=%x20)",
            "%(trailers:key=Reason,valueonly,separator=%x20)"), task.work_sha, "--")
        summaries = []
        for ended_at, disposition, reason in _records(records, 3):
            if disposition.strip() in set(CheckpointDisposition):
                summaries.append(TaskCheckpointSummary(
                    CheckpointDisposition(disposition.strip()), reason.strip() or None, ended_at))
        return tuple(summaries)

    @staticmethod
    def _history(sha: str, definition: Definition) -> tuple[str, ...]:
        """The accepted decision line ending at ``sha``, as `git log` arguments.

        A procedure task's first commit takes its product input as first parent,
        so an unbounded first-parent walk would read product commits' trailers
        and subjects as task decisions. Product history is retained, never
        accepted.
        """
        return (sha, "--not", definition.procedure.candidate) if definition.procedure else (sha,)

    def contains(self, ancestor: str, descendant: str) -> bool:
        validate_object_id(ancestor)
        validate_object_id(descendant)
        try:
            return self.git("merge-base", ancestor, descendant) == ancestor
        except RuntimeError:
            return False

    def _commit(self, task_id, old, definition, body, message, parents=()):
        definition = Definition.model_validate(definition.model_dump())
        # Git refuses a message containing NUL; nothing else needs care.
        message = message.replace("\0", "\ufffd")
        content = "---\n" + yaml.safe_dump(
            definition.model_dump(mode="json", exclude_none=True), sort_keys=False,
        ) + "---\n\n" + body.strip() + "\n"
        blob = self.git("hash-object", "-w", "--stdin", input_text=content)
        tree = self.git("mktree", input_text=f"100644 blob {blob}\ttask.md\n")
        ancestry = tuple(dict.fromkeys(p for p in (old, *parents) if p))
        sha = self.git("commit-tree", tree,
                       *(arg for p in ancestry for arg in ("-p", p)),
                       input_text=message + "\n")
        self.git("update-ref", PREFIX + str(task_id), sha, old or ZERO)
        return sha

    def create(self, spec: TaskSpec, *, kind=TaskOriginKind.CONVERSATION,
               source=None, owner=None, hold=None, reason=None, task_id=None, procedure=None):
        # Accepted source IDs make SQL/world/incident replay idempotent. They
        # are never chosen by product work. A direct Git author uses a UUID.
        identity = uuid.uuid5(uuid.NAMESPACE_URL, source).hex if source else uuid.uuid4().hex
        task_id = task_id or TaskId("task-" + identity)
        with self.lease:
            if PREFIX + str(task_id) in self.refs():
                _, existing, _ = self.read(task_id)
                if existing.source != source or existing.repository != spec.repository:
                    raise ValueError("task identity already belongs to another request")
                return task_id, False
            definition = Definition(repository=spec.repository, title=spec.title,
                                    origin=kind, source=source, owner=owner,
                                    priority=spec.priority, hold=hold, reason=reason, procedure=procedure)
            self._commit(task_id, None, definition, spec.brief, "steward: accept task",
                         (procedure.candidate,) if procedure else ())
        return task_id, True

    def change(self, task_id: TaskId, decide: Callable, *, message: str,
               parents=(), source=None) -> str:
        with self.lease:
            old, definition, body = self.read(task_id)
            if source:
                message += f"\nSteward-Source: {source}"
            return self._commit(task_id, old, decide(definition), body, message, parents)

    def accept_understanding(self, task_id, offer, base, body) -> tuple[str, bool]:
        """Accept a native account body by exact-base compare-and-swap.

        Only the body changes. The Definition, pending inputs, work and
        publication obligation are carried unchanged, so acceptance grants
        nothing. The offer's blob identity is recorded, and replay returns the
        revision that first accepted it rather than deciding again.
        """
        validate_object_id(offer)
        validate_object_id(base)
        with self.lease:
            old, definition, current = self.read(task_id)
            for line in self.git("log", "--first-parent", "--format=%H %(trailers:key=Steward-Offer,valueonly)",
                                 *self._history(old, definition)).splitlines():
                revision, _, offered = line.partition(" ")
                if offered.strip() == offer:
                    return revision, False
            if definition.hold:
                raise OfferRejected(f"task is {definition.hold}")
            if base != old:
                try:
                    # A base on the accepted line; a retained work parent is not an account.
                    opening = self.read(task_id, base)[2] if self.contains(base, old) else None
                except (RuntimeError, ValueError):
                    opening = None
                changes = (f"Accepted since your base:\n\n{current[len(opening):].strip()}"
                           if opening is not None and current.startswith(opening)
                           else f"The current accepted account is:\n\n{current}")
                unseen = [f"{kind} (source: {source}): {text}"
                          for commit, kind, text, source in self._record(task_id, old).inputs
                          if not self.contains(commit, base)]
                if unseen:
                    changes += "\n\nAccepted inputs since your base:\n" + "\n".join(unseen)
                raise OfferRejected("the accepted task changed since the offered base",
                                    revision=old, changes=changes)
            return self._commit(task_id, old, definition, body,
                                f"accept understanding\n\nSteward-Offer: {offer}"), True

    def input(self, task_id, kind, text, *, decide=lambda d: d, source=None):
        label = f"assistant {source}" if source and source.startswith("turn_") else source or "unrecorded"
        return self.change(task_id, decide, message=f"{kind} (source: {label})\n\n{text}\n\nSteward-Input: {kind}", source=source)

    # Decisions. Each is one guarded compare-and-swap commit on the task ref.

    @staticmethod
    def _text(text, what):
        text = text.strip()
        if not 1 <= len(text) <= 8000:
            raise ValueError(f"{what} must contain 1 through 8000 characters")
        return text

    def answer(self, task_id, text, *, source="operator") -> Task:
        def answer(d):
            if d.hold or self._record(task_id).disposition != "ask" or d.resume == d.work:
                raise RuntimeError("only a waiting task may be answered")
            return d.model_copy(update={"resume": d.work, "reason": None})
        self.input(task_id, "answer", self._text(text, "task answer"), decide=answer, source=source)
        return self.get(task_id)

    def note(self, task_id, text, *, kind="note", source="operator") -> Task:
        """Queue context for the next slice without changing the task's status."""
        def note(d):
            if d.hold == "cancelled":
                raise RuntimeError("cancelled task cannot receive a note")
            if self.get(task_id).status is TaskStatus.DONE:
                raise RuntimeError("completed task cannot receive a note")
            return d
        self.input(task_id, kind, self._text(text, "task note"), decide=note, source=source)
        return self.get(task_id)

    def confirm(self, task_id) -> Task:
        def confirm(d):
            if d.hold != "proposed":
                raise RuntimeError("only a proposed task may be confirmed")
            return d.model_copy(update={"hold": None, "reason": None})
        self.change(task_id, confirm, message="admit proposal")
        return self.get(task_id)

    def reject(self, task_id, reason="operator rejected proposal") -> Task:
        if not reason.strip():
            raise ValueError("task rejection reason must be nonblank")
        def reject(d):
            if d.hold != "proposed":
                raise RuntimeError("only a proposed task may be rejected")
            return d.model_copy(update={"hold": "cancelled", "reason": reason.strip()})
        self.change(task_id, reject, message=f"reject proposal\n\n{reason.strip()}")
        return self.get(task_id)

    def retry(self, task_id, note=None, *, source="operator") -> Task:
        if source == "operator" and self._record(task_id).origin_kind in {
                TaskOriginKind.INCIDENT_REPAIR, TaskOriginKind.INCIDENT_ESCALATION}:
            raise PermissionError("incident-owned tasks may only be retried by incidents")
        def retry(d):
            if d.hold not in {"blocked", "cancelled"} and self._record(task_id).disposition != "blocked":
                raise RuntimeError("only a blocked or cancelled task may be retried")
            return d.model_copy(update={"hold": None, "reason": None, "resume": d.work, "repair": None})
        if note is None:
            self.change(task_id, retry, message="retry task")
        else:
            self.input(task_id, "retry", self._text(note, "task retry note"), decide=retry, source=source)
        return self.get(task_id)

    def cancel(self, task_id, reason=None) -> Task:
        reason = "operator requested cancellation" if reason is None else reason.strip()
        if not reason:
            raise ValueError("task cancellation reason must be nonblank")
        if self.get(task_id).status is TaskStatus.DONE:
            raise RuntimeError("completed task cannot be cancelled")
        self.hold(task_id, "cancelled", reason)
        return self.get(task_id)

    def cancelled(self, task_id) -> bool:
        return self._record(task_id).definition.hold == "cancelled"

    def set_priority(self, task_id, priority) -> Task:
        if not -100 <= priority <= 100:
            raise ValueError("task priority is outside the supported range")
        if self.get(task_id).status in {TaskStatus.RUNNING, TaskStatus.DONE, TaskStatus.CANCELLED}:
            raise RuntimeError("only inactive unfinished work may change priority")
        self.change(task_id, lambda d: d.model_copy(update={"priority": priority}), message="set priority")
        return self.get(task_id)

    def retarget(self, task_id, repository) -> Task:
        if self.repositories is not None and repository not in self.repositories:
            raise PermissionError("unmanaged repository")
        def retarget(d):
            if d.work or d.hold not in {None, "proposed"}:
                raise RuntimeError("only unstarted proposed or queued work may be retargeted")
            return d.model_copy(update={"repository": repository})
        # Holding the task lock is what keeps a first slice from starting in
        # the old repository between this check and its commit. The wait
        # outlasts a status probe's momentary hold, never a running slice.
        lock = task_lock(self.locks_root, task_id, timeout=1)
        try:
            lock.acquire()
        except Busy:
            raise RuntimeError("only unstarted proposed or queued work may be retargeted") from None
        try:
            self.change(task_id, retarget, message=f"retarget task to {repository}")
        finally:
            lock.release()
        return self.get(task_id)

    def hold(self, task_id, hold, reason) -> bool:
        """Hold the task with a reason; blocking never overrides an operator's hold."""
        if not reason.strip():
            raise ValueError("a held task requires a reason")
        changed = False
        def decide(d):
            nonlocal changed
            if hold == "blocked" and d.hold in {"cancelled", "proposed"}:
                return d
            changed = True
            return d.model_copy(update={"hold": hold, "reason": reason.strip()})
        self.change(task_id, decide, message=f"{'block' if hold == 'blocked' else 'cancel'} task\n\n{reason.strip()}")
        return changed

    def finish_slice(self, task_id, *, disposition, opened_at, detail=None,
                     consumed_input_ids=None, work_sha=None):
        if disposition is CheckpointDisposition.ASK and not (detail and detail.strip()):
            raise ValueError("ask requires a non-empty detail")
        if consumed_input_ids is None and disposition is not CheckpointDisposition.BLOCKED:
            consumed_input_ids = frozenset(source for source, _, _, _ in self._record(task_id).pending
                                           if len(opened_at) == 40 and self.contains(source, opened_at))
        consumed = " ".join(sorted(consumed_input_ids or ()))
        def finish(d):
            # A concurrent operator decision owns hold/resume; accepting work
            # cannot erase cancellation or a new note.
            updates = {"work": work_sha} if work_sha and work_sha != d.work else {}
            if d.hold is None and disposition is CheckpointDisposition.BLOCKED:
                updates.update(hold="blocked", reason=detail or "interrupted")
            return d.model_copy(update=updates)
        self.change(task_id, finish,
            message=f"checkpoint {disposition.value}\n\nSteward-Consumed: {consumed}",
            parents=(work_sha,) if work_sha else ())

    def sync(self):
        """Fast-forward only; deletion retains custody, divergence stops intake.

        This remote is trusted admission configuration, never a task field.
        A non-force push publishes accepted decisions. Failed/ambiguous pushes
        leave local refs intact and repeat safely on the next sync.
        """
        if self.remote is None:
            return
        validate_git_remote_url(self.remote, allow_local=True)
        with self.lease:
            self.git("fetch", "--no-tags", "--no-write-fetch-head", "--atomic", "--",
                     self.remote, "+refs/heads/tasks/*:refs/steward/incoming/*")
            local = self.refs()
            for ref, remote_sha in self.refs("refs/steward/incoming/").items():
                target = PREFIX + ref.removeprefix("refs/steward/incoming/")
                previous = local.get(target)
                if previous == remote_sha:
                    continue
                if previous and self.contains(remote_sha, previous):
                    continue
                if previous and not self.contains(previous, remote_sha):
                    raise RuntimeError(f"divergent task decisions: {target}")
                self.read(TaskId(target.removeprefix(PREFIX)), remote_sha)
                self.git("update-ref", target, remote_sha, previous or ZERO)
            # Validate all accepted records before publishing any local decision.
            self.all()
            refs = self.refs()
            if refs:
                self.git("push", "--atomic", "--", self.remote,
                         *(f"{sha}:{ref}" for ref, sha in refs.items()))

    def retain_work(self, repository, broker, sha):
        """Fetch one exact agent commit's closure into this store, checking every object."""
        self.git("-c", "transfer.fsckObjects=true", "fetch", "--quiet", "--no-tags",
                 "--no-write-fetch-head", f"--upload-pack={broker.git_service('upload-pack')}",
                 "--", str(repository.path), sha)

    def push_to_agent(self, repository, broker, *refspecs):
        self.git("push", "--quiet", f"--receive-pack={broker.git_service('receive-pack')}",
                 "--", str(repository.path), *refspecs)

    def restore_work(self, task_id, repository, broker):
        """Restore accepted work into a fresh agent clone, without credentials."""
        _, definition, _ = self.read(task_id)
        if not definition.work:
            return
        sha = definition.work
        from steward_harness.git import run_agent_git
        held = run_agent_git(broker, "show-ref", "--verify", "--hash", f"refs/heads/tasks/{task_id}",
                             cwd=repository.path, timeout=60)
        if held.returncode == 0:
            if held.stdout.strip() != sha:
                raise TaskWorkConflict("unaccepted task work must be reconciled before publication")
            return
        self.push_to_agent(repository, broker, f"{sha}:refs/heads/tasks/{task_id}")

    def request_repair(self, task_id, transport, work, base, failure):
        """Return exact failing inputs to the owner; stop repeated no-progress."""
        parents = ()
        if failure.candidate:
            self.git("fetch", "--no-tags", "--no-write-fetch-head", "--",
                     str(transport.git_dir), failure.candidate)
            parents = (failure.candidate,)
        with self.lease:
            _, definition, _ = self.read(task_id)
            if definition.hold or definition.work != work:
                return "decision changed"
            prior = definition.repair
            attempts = prior.attempts + 1 if prior and prior.base == base else 1
            unchanged = bool(prior and prior.base == base
                and not self.git("diff", "--name-only", prior.work, work))
            waiting = unchanged or not failure.actionable
            reason = ("Automatic reconciliation made no progress: the product tree remains "
                      "unchanged on the same base and validation is still red. An operator decision is required.\n"
                      if unchanged else "") + failure.reason
            text = (f"Work: {work}\nBase: {base}\n"
                    f"Candidate: {failure.candidate or 'integration conflicted before gates'}\n"
                    f"Repair attempt: {attempts}\n\n{reason}\n\n"
                    "Reconcile the retained task branch with this exact base, preserving both intents. "
                    "Fix actionable gate findings and commit the result. Do not change gates to pass. "
                    "If intent or capability is missing, ask a concrete question and use Disposition: ask. "
                    "A new integrated candidate must pass all gates again.")
            self.change(task_id, lambda d: d.model_copy(update={
                "repair": Repair(base=base, work=work, attempts=attempts, reason=failure.reason),
                "resume": work,
                "hold": "blocked" if waiting else None,
                "reason": reason if waiting else None,
            }), message=f"block task reconciliation\n\n{text}" if waiting else f"reconcile task\n\n{text}\n\nSteward-Input: repair",
                parents=parents)
            return "waiting for decision" if waiting else "repair queued"

    def has_source(self, task_id, source):
        sha, definition, _ = self.read(task_id)
        return source in self.git("log", "--first-parent", "--format=%(trailers:key=Steward-Source,valueonly)",
                                  *self._history(sha, definition)).splitlines()
