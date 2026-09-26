"""Configured procedures create ordinary accepted tasks over exact Git inputs.

A rhythm over `input: world` is the one exception: it is an ordinary world turn
in its own conversation, accepted like an operator's, not a task.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from steward_harness.git_transport import GitTransportError
from steward_harness.lease import Busy
from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeUnavailable
from steward_harness.state import ConversationBusy, ConversationId, TaskId, TaskSpec, TaskStatus
from steward_harness.task_store import ProcedureRun
from steward_harness.world.turn_checkpoint import WorldContentConflict, WorldUpdatePending

log = logging.getLogger(__name__)


def resolve_input(reference, transports, *, fetch=True):
    _, repository, branch = reference.split("/", 2)
    transport = transports[repository]
    if fetch:
        transport.fetch()
    candidate = transport._run("rev-parse", "--verify", f"refs/steward/remote/{branch}^{{commit}}")
    parents = transport._run("rev-list", "--parents", "-n", "1", candidate).split()
    return repository, candidate, parents[1] if len(parents) > 1 else candidate


def captured(runs):
    """Every commit a rhythm's finished runs captured: the input it has seen.

    Only a finished run covers its input; a blocked or cancelled one reported
    nothing about it.
    """
    return {sha for task in runs if task.status is TaskStatus.DONE
            for sha in (task.procedure.candidate, *(task.procedure.activity or {}).values())}


class Procedures:
    def __init__(self, config, state, transports):
        self.config, self.state, self.transports = config, state, transports
        # Per quiet rhythm: the commits seen so far, and when the newest arrived.
        self._quiet = {}

    def request(self, name, repository, candidate, base, *, event="", owner=None, activity=None, workdir=None):
        config = self.config.procedures[name]
        instructions = Path(config.instructions).read_text()
        payload = config.model_dump(mode="json") | {"text": instructions}
        if workdir is not None:
            payload["workdir"] = workdir
        identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        key = json.dumps([name, identity, repository, candidate, base, event, owner])
        source = "procedure:" + hashlib.sha256((event or key).encode()).hexdigest()
        import uuid
        task_id = TaskId("task-" + uuid.uuid5(uuid.NAMESPACE_URL, source).hex)
        if "refs/heads/tasks/" + str(task_id) in self.state.tasks.refs():
            return task_id
        transport = self.transports[repository]
        with self.state.tasks.lease:
            self.state.tasks.git("fetch", "--no-tags", "--no-write-fetch-head", "--",
                                 str(transport.git_dir), candidate)
            run = ProcedureRun(**(config.model_dump() | {"instructions": instructions}),
                               name=name, identity=identity, candidate=candidate, base=base,
                               event=event, activity=activity, workdir=workdir)
            title = f"{name}: {candidate[:12]}"
            brief = f"Execute {name} on candidate {candidate} against base {base}."
            if event.startswith("rhythm:"):
                title = f"{name}: organisation reflection ({event.rsplit(':', 1)[-1][:12]})"
                brief = (
                    f"Execute {name} across the organisation.\n\n"
                    "Follow the accepted procedure across the relevant repositories; "
                    "discover changes and provenance through Git and report unavailable evidence explicitly."
                )
            task_id, _ = self.state.tasks.create(
                TaskSpec(repository, title, brief),
                source=source, procedure=run, owner=owner)
        return task_id

    def require(self, names, repository, candidate, base):
        """None means pending; empty text passes; other text is accepted failure."""
        pending, failures = [], []
        for name in names:
            task_id = self.request(name, repository, candidate, base)
            task = self.state.tasks.get(task_id)
            if task.definition.hold or task.dispatchable or task.verdict is None:
                pending.append(str(task_id))
            elif task.verdict != "pass":
                failures.append(f"{name} ({task_id}): {(task.findings or '')[-4000:]}")
        return "\n\n".join(failures) if failures else (None if pending else "")

    def observe(self, rhythm, definitions=None, heads=None, *, fetch=True):
        """(repository, candidate, base, activity): the input a run would capture.

        A repository rhythm's input is its candidate. An organisation rhythm
        (`workdir`) reads across everything, so its input is also every observed
        remote head and every ordinary task's accepted work. Task documents,
        their acceptance commits and procedure runs' evidence are the steward's
        own bookkeeping and never appear: a run cannot supply its successor's input.
        """
        heads = {} if heads is None else heads

        def remote(name):
            if name not in heads:
                transport = self.transports[name]
                if fetch:
                    transport.fetch()
                heads[name] = dict(line.split()[::-1] for line in transport._run(
                    "for-each-ref", "--format=%(objectname) %(refname:strip=3)",
                    "refs/steward/remote/").splitlines())
            return heads[name]

        _, repository, _ = rhythm.input.split("/", 2)
        remote(repository)
        repository, candidate, base = resolve_input(rhythm.input, self.transports, fetch=False)
        if rhythm.workdir is None:
            return repository, candidate, base, None
        activity = {f"repositories/{name}/{ref}": sha
                    for name in self.transports for ref, sha in remote(name).items()}
        if definitions is None:
            definitions = {str(task.task_id): task.definition for task in self.state.tasks.all()}
        activity.update({f"tasks/{task_id}": d.work for task_id, d in definitions.items()
                         if d.work and not (d.procedure and (d.procedure.access == "read-only"
                                                             or d.procedure.event.startswith("rhythm:")))})
        return repository, candidate, base, activity

    def advance_rhythms(self, *, now=None):
        """Admit each rhythm whose input holds a commit none of its finished runs saw.

        An interval rhythm admits at most one run per interval and only then;
        a quiet rhythm admits once its new input has stopped moving for its
        quiet period.
        """
        observed_now = time.monotonic() if now is None else now
        now = time.time() if now is None else now
        tasks = self.state.tasks.all()
        definitions = {str(task.task_id): task.definition for task in tasks}
        heads = {}
        for name, rhythm in self.config.rhythms.items():
            if rhythm.input == "world":
                continue  # A world turn, not a task: see `due_world_rhythms`.
            prefix = f"rhythm:{name}:"
            runs = [task for task in tasks if task.procedure and task.procedure.event.startswith(prefix)]
            # Cancellation ends recurrence's obligation once native execution
            # has stopped. Holds and reopened idle slices still prevent overlap.
            # A blocked run is not running, and nothing retries it: the
            # rhythm's next admission supersedes it rather than waiting forever.
            open_runs = [task for task in runs if task.status not in {TaskStatus.DONE, TaskStatus.CANCELLED}]
            if any(task.status is not TaskStatus.BLOCKED for task in open_runs):
                continue
            interval = isinstance(rhythm.schedule, int)
            event = f"{prefix}{int(now // rhythm.schedule)}" if interval else None
            if interval and any(task.procedure.event == event for task in runs):
                continue
            try:
                repository, candidate, base, activity = self.observe(rhythm, definitions, heads)
            except GitTransportError as error:
                log.warning("Rhythm %s input unavailable this poll: %s", name, error)
                continue
            observed = {candidate, *(activity or {}).values()}
            if observed <= captured(runs):
                self._quiet.pop(name, None)
                log.debug("Rhythm %s has no new input since its last run", name)
                continue
            if not interval:
                known, since = self._quiet.get(name, (set(), observed_now))
                since = observed_now if observed - known else since
                self._quiet[name] = (known | observed, since)
                if observed_now - since < rhythm.schedule.quiet:
                    continue
                digest = json.dumps(activity if activity is not None else candidate, sort_keys=True)
                event = f"{prefix}quiet:{hashlib.sha256(digest.encode()).hexdigest()}"
                if any(task.procedure.event == event for task in runs):
                    continue  # This exact input's run blocked or was cancelled.
            for task in open_runs:
                self.state.tasks.cancel(task.task_id, f"superseded by {event}")
            try:
                self.request(rhythm.procedure, repository, candidate, base, event=event,
                             owner=rhythm.owner, activity=activity, workdir=rhythm.workdir)
            except (OSError, ValueError) as error:
                # A refused admission consumes no input; keep other rhythms and
                # operator ingress alive while the operator repairs its policy.
                log.error("Rhythm %s admission failed: %s", name, error)

    def due_world_rhythms(self, *, now=None):
        """Each world rhythm whose current interval has no settled run.

        The source key is the whole idempotency: one turn per rhythm and
        interval, replayed rather than repeated after a restart. A recorded
        receipt settles the interval; an interrupted turn consumes it.
        """
        now = time.time() if now is None else now
        for name, rhythm in self.config.rhythms.items():
            if rhythm.input != "world":
                continue
            key = f"rhythm:{name}:{int(now // rhythm.schedule)}"
            prior = self.state.turn_for_source(ConversationId(f"rhythm:{name}"), key)
            if self.state.result_receipt(key) or (prior is not None and prior.state == "interrupted"):
                continue
            yield name, key

    def run_world_rhythm(self, conversations, name, key):
        """Run one interval as a world turn, then hand any reply to its owner."""
        rhythm = self.config.rhythms[name]
        procedure = self.config.procedures[rhythm.procedure]
        owner = ConversationId(f"rhythm:{name}")
        self.state.open_conversation(owner, provider=procedure.provider,
                                     profile=self.state.tasks.default_profile)
        if self.state.turn_for_source(owner, key) is None:
            # Each interval starts a fresh session on the procedure's provider:
            # the world, not yesterday's session, carries what was consolidated.
            self.state.bind_conversation_provider(owner, procedure.provider, None)
        try:
            result = conversations.run_turn(
                transport="rhythm", transport_key=name, source_event_key=key,
                operator_id="harness:rhythm", text=Path(procedure.instructions).read_text(),
                episode_input=f"Scheduled {name} rhythm ({key}).",
                allow_empty_output=True, model=procedure.model,
            )
        except (Busy, ConversationBusy, WorldContentConflict, WorldUpdatePending) as error:
            log.info("world rhythm %s deferred: %s", key, error)
            return
        except (RuntimeExecutionError, RuntimeUnavailable, OSError, ValueError) as error:
            # The interval is consumed: at most one run, never a retry storm.
            log.error("world rhythm %s failed: %s", key, error)
            return
        reply = result.reply_text.strip()
        # The ordinary result lane delivers a pending receipt to its owner.
        self.state.save_result_receipt({
            "owner": rhythm.owner, "task_id": None, "source_key": key,
            "result_text": reply, "reply": reply, "done": not (reply and rhythm.owner),
        })
