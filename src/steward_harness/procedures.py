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
from datetime import datetime

from steward_harness.git_transport import GitTransportError
from steward_harness.lease import Busy, Lease
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


def interval(rhythms, name, now):
    """The interval a rhythm is in; a dependent rhythm is in its predecessor's."""
    rhythm = rhythms[name]
    if rhythm.after is not None:
        return interval(rhythms, rhythm.after, now)
    return int((now - rhythm.offset) // rhythm.schedule)


def rhythm_attempts(runs):
    """Group immutable execution sources by their captured interval."""
    grouped = {}
    for run in runs:
        parts = run.source_event_key.split(":")
        if len(parts) >= 3 and parts[0] == "rhythm":
            grouped.setdefault(int(parts[2]), []).append(run)
    return grouped


def superseded(runs, name, receipt):
    """Interrupted intervals that a later accepted interval has settled.

    A rhythm procedure reads from its own cursor, so a later accepted run has
    already covered what an older interrupted one missed. Clocks still never
    supersede unfinished work: only an accepted completion does.
    """
    latest = next((index for index in sorted(runs, reverse=True)
                   if runs[index][-1].state == "completed" and receipt(f"rhythm:{name}:{index}")),
                  None)
    return {index for index, attempts in runs.items()
            if latest is not None and index < latest and attempts[-1].state == "interrupted"}


def world_rhythm_interval(rhythms, name, now, runs, predecessors, receipt):
    """Oldest captured obligation first; clocks never supersede unfinished work.

    A predecessor's captured interval binds its dependent's obligation even
    if no poll ran before rollover. Unobserved clock intervals are not backfilled.
    Offline readers pass a receipt lookup returning None (unknown).
    """
    current = interval(rhythms, name, now)
    settled = superseded(runs, name, receipt)
    pending = {index for index, attempts in runs.items() if index not in settled
               and (attempts[-1].state != "completed"
                    or receipt(f"rhythm:{name}:{index}") is False)}
    after = rhythms[name].after
    skipped = superseded(predecessors, after, receipt) if after else set()
    pending.update(index for index in predecessors if index not in runs and index not in skipped)
    return min(pending) if pending else current


def world_rhythm_observation(rhythms, name, now, *, run=None, latest=None,
                             receipt=None, before=None, changed=None, index=None,
                             recoverable=False, queued=False, writer_active=None):
    """Describe the captured obligation admission uses; unknown input is not due.

    Callers supply canonical turns/receipts and the world's change guard. An
    offline reader that cannot check that guard passes None, never guesses.
    """
    rhythm = rhythms[name]
    root = rhythm
    while root.after is not None:
        root = rhythms[root.after]
    index = interval(rhythms, name, now) if index is None else index
    start = index * root.schedule + root.offset
    key = f"rhythm:{name}:{index}"
    last = run or latest
    stamp = (last.completed_at or last.started_at) if last else None
    updated = datetime.fromisoformat(stamp).timestamp() if stamp else None
    value = dict(event=key, last_event=last.source_event_key if last else None,
                 status=last.state if last else "never_started",
                 started_at=last.started_at if last else None,
                 completed_at=last.completed_at if last else None,
                 updated=updated, evidence_age_seconds=max(0, now - updated) if updated else None,
                 observed_at=now, due_at=start, interval_end=start + root.schedule,
                 schedule=({'after': rhythm.after} if rhythm.after else
                           {'interval': rhythm.schedule, 'offset': rhythm.offset}),
                 paths=list(rhythm.paths or []), eligible=False,
                 obligation=("accepted" if run and run.state == "completed"
                             else "open" if run or before else "uncaptured"),
                 turn_id=str(run.turn_id) if run else None)
    if queued:
        progress = "continuation_queued"
        value["eligible"] = True
    elif recoverable:
        progress = "acceptance_pending"
        value["eligible"] = True
    elif run and run.state == "interrupted":
        progress = "held"
    elif receipt:
        progress = "scheduled"
        value['due_at'] = start + root.schedule
    elif run and run.state == "completed" and receipt is None:
        progress = "accepted_receipt_unobserved"
    elif run:
        progress = ("receipt_pending" if run.state == "completed" else
                    "recovery_held" if writer_active is False else "active")
        value['eligible'] = run.state == "completed"  # Never replay uncertain native work.
    elif rhythm.after and (before is None or before.state != "completed"):
        progress = "predecessor_held" if before and before.state == "interrupted" else "awaiting_predecessor"
    elif rhythm.paths and changed is None:
        progress = "input_unobserved"
    elif rhythm.paths and not changed:
        progress = "awaiting_input"
    else:
        progress = "overdue" if now > start else "due"
        value['eligible'] = True
    value['progress'] = progress
    value['overdue_seconds'] = max(0, now - start) if progress == "overdue" else 0
    return value


class Procedures:
    def __init__(self, config, state, transports, *, world=None):
        self.config, self.state, self.transports = config, state, transports
        self.world = world
        # Per quiet rhythm: the commits seen so far, and when the newest arrived.
        self._quiet = {}
        # Per procedure rhythm: the bucket whose fetch it has already made.
        self._observed_bucket = {}

    def request(self, name, repository, candidate, base, *, event="", owner=None, activity=None, workdir=None):
        config = self.config.procedures[name]
        instructions = Path(config.instructions).read_text()
        # Defaults left unsaid keep the identity every earlier run was accepted under.
        unsaid = ({"fallback"} if config.fallback else set()) | ({"models"} if config.models is None else set())
        payload = config.model_dump(mode="json", exclude=unsaid)
        payload["text"] = instructions
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

    def observe(self, rhythm, definitions=None, heads=None, *, fetch=True, fetched=None):
        """(repository, candidate, base, activity): the input a run would capture.

        A repository rhythm's input is its candidate. An organisation rhythm
        (`workdir`) reads across everything, so its input is also every observed
        remote head and every ordinary task's accepted work. Task documents,
        their acceptance commits and procedure runs' evidence are the steward's
        own bookkeeping and never appear: a run cannot supply its successor's input.
        With `paths`, only the keys under them count, and only the repositories
        they name are fetched: a repository that cannot wake the rhythm is not
        worth a network round trip on every poll.

        `fetch` is True (fetch what it reads), False (read the refs as the last
        fetch left them) or the repository names to fetch. `heads` and
        `fetched` let one pass share what it has already read and fetched.
        """
        heads = {} if heads is None else heads
        fetched = set() if fetched is None else fetched
        prefixes = tuple(path.rstrip("/") for path in rhythm.paths)

        def remote(name):
            wanted = fetch is True or (bool(fetch) and name in fetch)
            if name not in heads or (wanted and name not in fetched):
                transport = self.transports[name]
                if wanted:
                    transport.fetch()
                    fetched.add(name)
                heads[name] = dict(line.split()[::-1] for line in transport._run(
                    "for-each-ref", "--format=%(objectname) %(refname:strip=3)",
                    "refs/steward/remote/").splitlines())
            return heads[name]

        _, repository, _ = rhythm.input.split("/", 2)
        remote(repository)
        repository, candidate, base = resolve_input(rhythm.input, self.transports, fetch=False)
        if rhythm.workdir is None:
            return repository, candidate, base, None
        watched = [name for name in self.transports if not prefixes or any(
            prefix == f"repositories/{name}" or prefix.startswith(f"repositories/{name}/")
            for prefix in prefixes)]
        activity = {f"repositories/{name}/{ref}": sha
                    for name in watched for ref, sha in remote(name).items()}
        if definitions is None:
            definitions = {str(task.task_id): task.definition for task in self.state.tasks.all()}
        activity.update({f"tasks/{task_id}": d.work for task_id, d in definitions.items()
                         if d.work and not (d.procedure and (d.procedure.access == "read-only"
                                                             or d.procedure.event.startswith("rhythm:")))})
        if prefixes:
            # What the rhythm is for, not everything it can read: its runs
            # capture only these keys, so nothing outside them is ever new.
            activity = {key: sha for key, sha in activity.items()
                        if any(key == prefix or key.startswith(prefix + "/") for prefix in prefixes)}
        return repository, candidate, base, activity

    def advance_rhythms(self, *, now=None):
        """Admit each rhythm whose input holds a commit none of its finished runs saw.

        An interval rhythm admits at most one run per interval and only then;
        a quiet rhythm admits once its new input has stopped moving for its
        quiet period.

        Rhythms are not a second fetcher. A repository some target follows is
        fetched by that target's own observation, at least once a minute, and
        a rhythm reads its refs as that fetch left them. The rest, and the
        rhythm's own input repository, are fetched on the rhythm's first
        observation in each bucket: its interval, or its quiet period. An
        idle bucket therefore costs local ref reads and no network, and no
        repository goes unfetched for longer than a bucket.
        """
        observed_now = time.monotonic() if now is None else now
        now = time.time() if now is None else now
        tasks = self.state.tasks.all()
        definitions = {str(task.task_id): task.definition for task in tasks}
        heads, fetched = {}, set()
        refreshed = {target.ref.split("/", 2)[1] for target in self.config.targets.values()}
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
            periodic = isinstance(rhythm.schedule, int)
            event = f"{prefix}{interval(self.config.rhythms, name, now)}" if periodic else None
            if periodic and any(task.procedure.event == event for task in runs):
                continue
            bucket = (interval(self.config.rhythms, name, now) if periodic
                      else int(now // rhythm.schedule.quiet))
            fetch = set()
            if self._observed_bucket.get(name) != bucket:
                fetch = {rhythm.input.split("/", 2)[1], *(set(self.transports) - refreshed)}
            try:
                repository, candidate, base, activity = self.observe(
                    rhythm, definitions, heads, fetch=fetch, fetched=fetched)
            except GitTransportError as error:
                log.warning("Rhythm %s input unavailable this poll: %s", name, error)
                continue
            self._observed_bucket[name] = bucket
            # With `paths`, only what they name is input, the candidate included:
            # a writing rhythm's own landing moves its input repository.
            observed = ({*(activity or {}).values()} if rhythm.paths and activity is not None
                        else {candidate, *(activity or {}).values()})
            if observed <= captured(runs):
                self._quiet.pop(name, None)
                log.debug("Rhythm %s has no new input since its last run", name)
                continue
            if not periodic:
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

    def world_rhythm_observations(self, *, now=None, writer_active=None):
        """Fresh admission facts; no persisted cursor besides turns and receipts."""
        now = time.time() if now is None else now
        observations = {}
        if writer_active is None:
            writer_active = self._world_rhythm_lease().held()
        histories = {name: rhythm_attempts(self.state.rhythm_turns(ConversationId(f"rhythm:{name}")))
                     for name, rhythm in self.config.rhythms.items() if rhythm.input == "world"}
        claims = {row["turn_id"]: row for row in self.state.claimed_turns()}
        for name, runs in histories.items():
            rhythm = self.config.rhythms[name]
            predecessors = histories.get(rhythm.after, {})
            index = world_rhythm_interval(self.config.rhythms, name, now, runs, predecessors,
                                         lambda key: bool(self.state.result_receipt(key)))
            key = f"rhythm:{name}:{index}"
            attempts = runs.get(index, [])
            prior = attempts[-1] if attempts else None
            before = predecessors.get(index, [None])[-1]
            receipt = bool(self.state.result_receipt(key))
            changed = None
            if (rhythm.paths and prior is None and not receipt
                    and (not rhythm.after or (before and before.state == "completed"))):
                changed = self._world_changed(name, rhythm.paths)
            claim = claims.get(str(prior.turn_id)) if prior else None
            observations[name] = world_rhythm_observation(
                self.config.rhythms, name, now, run=prior,
                latest=self.state.latest_rhythm_turn(ConversationId(f"rhythm:{name}")),
                receipt=receipt, before=before, changed=changed, index=index,
                recoverable=bool(claim and claim["output"] is not None),
                queued=bool(prior and prior.state == "running" and prior.rhythm_continuation
                            and claim is None), writer_active=writer_active)
        return observations

    def due_world_rhythms(self, *, now=None):
        # Oldest captured interval first; configured order breaks ties (including
        # a night chain). A short-period rhythm cannot continually jump ahead
        # of an hourly/daily rhythm while both are due. No catch-up buckets.
        observations = self.world_rhythm_observations(now=now)
        for name, value in sorted(observations.items(), key=lambda item: item[1]['due_at']):
            if value['eligible']:
                yield name, value['event']

    def advance_world_rhythm(self, conversations):
        # Resolve at execution, not enqueue: backpressure may span intervals.
        due = next(iter(self.due_world_rhythms()), None)
        if due is not None and not self.state.paused():
            self.run_world_rhythm(conversations, *due)

    def _world_changed(self, name, paths):
        """Did the world change under `paths` since this rhythm's last accepted run?

        The cursor is that run's own candidate, not its base: what it wrote is
        in both sides of the comparison, so its writes never retrigger it,
        while anything another turn wrote since its base still counts.
        """
        last = self.state.last_world_candidate(ConversationId(f"rhythm:{name}"))
        return last is None or self.world.changed(last, paths)

    def _world_rhythm_lease(self):
        # Commands and the automatic dispatcher share this exclusion boundary.
        return Lease(self.state.path.parent, lock_name=self.state.path.name + ".rhythms.lock",
                     timeout_seconds=0)

    def continue_world_rhythm(self, name, *, now=None):
        """Persist one continuation source for the ordinary budgeted rhythm worker."""
        try:
            with self._world_rhythm_lease():
                if self.state.paused():
                    return "World rhythm admission is paused."
                observed = self.world_rhythm_observations(now=now, writer_active=False)[name]
                key = observed["event"]
                if observed["progress"] in {"continuation_queued", "receipt_pending", "acceptance_pending"}:
                    return f"{key}: {observed['progress']}; awaiting the rhythm worker."
                if observed["progress"] == "recovery_held":
                    return (f"{key}: recovery held; inspect retained native evidence. "
                            "No provider completion is available to resume safely.")
                if observed["progress"] != "held":
                    return f"{key}: {observed['progress']}; no interrupted obligation to continue."
                owner = ConversationId(f"rhythm:{name}")
                attempts = rhythm_attempts(self.state.rhythm_turns(owner))[int(key.split(":")[2])]
                prior = attempts[-1]
                original = next((attempt.input_text for attempt in attempts
                                 if attempt.input_text != f"Scheduled {name} rhythm ({key})."), None)
                if original is None:
                    procedure = self.config.procedures[self.config.rhythms[name].procedure]
                    original = Path(procedure.instructions).read_text()
                text = (original + "\n\nContinue the open obligation " + key +
                        f". Previous attempt: {prior.turn_id}. "
                        "Inspect retained workspace and native records before acting. "
                        "External actions may already have happened; reconcile their evidence "
                        "instead of repeating them blindly. Complete only what remains.")
                self.state.start_turn(owner, f"{key}:continue:{prior.turn_id}", "harness:rhythm", text)
                return f"{key}: continuation queued."
        except (Busy, ConversationBusy, OSError, ValueError) as error:
            return f"{name}: held; {error}"

    def run_world_rhythm(self, conversations, name, key):
        """Run/recover one obligation without overlapping a manual continuation."""
        try:
            with self._world_rhythm_lease():
                return self._run_world_rhythm(conversations, name, key)
        except Busy as error:
            log.info("world rhythm %s deferred: %s", key, error)

    def _run_world_rhythm(self, conversations, name, key):
        rhythm = self.config.rhythms[name]
        procedure = self.config.procedures[rhythm.procedure]
        owner = ConversationId(f"rhythm:{name}")
        runs = rhythm_attempts(self.state.rhythm_turns(owner))
        index = int(key.split(":")[2])
        attempts = runs.get(index, [])
        prior = attempts[-1] if attempts else None
        if self.state.result_receipt(key):
            return
        # Even a stale dispatched/manual request cannot skip an older hold.
        settled = superseded(runs, name, lambda key: bool(self.state.result_receipt(key)))
        if any(i < index and i not in settled and rows[-1].state != "completed"
               for i, rows in runs.items()):
            return
        if prior and prior.state == "interrupted":
            return
        source = prior.source_event_key if prior else key
        self.state.open_conversation(owner, provider=procedure.lead_provider,
                                     profile=self.state.tasks.default_profile)
        if not attempts:
            self.state.bind_conversation_provider(owner, procedure.lead_provider, None)
        try:
            text = prior.input_text if prior else Path(procedure.instructions).read_text()
            result = conversations.run_turn(
                transport="rhythm", transport_key=name, source_event_key=source,
                operator_id="harness:rhythm", text=text,
                episode_input=f"Scheduled {name} rhythm ({key}).",
                allow_empty_output=True, procedure=procedure, notify_owner=rhythm.owner,
                reserved_rhythm=bool(prior and prior.rhythm_continuation),
            )
        except (Busy, ConversationBusy, WorldContentConflict, WorldUpdatePending) as error:
            log.info("world rhythm %s deferred: %s", key, error)
            return
        except (RuntimeExecutionError, RuntimeUnavailable, OSError, ValueError) as error:
            # Keep a visible hold even when preparation failed before a source
            # could be created. Other rhythms can still use the world owner.
            if self.state.turn_for_source(owner, source) is None:
                failed, _ = self.state.start_turn(
                    owner, source, "harness:rhythm", f"Scheduled {name} rhythm ({key}).")
                self.state.interrupt_turn(failed.turn_id, str(error))
            log.error("world rhythm %s held: %s", key, error)
            return
        recorded = result.reply_text.strip()
        if recorded:
            log.info("world rhythm %s: reply recorded, not delivered", key)
        # Record completion independently of any notification operation.
        self.state.save_result_receipt({
            "owner": rhythm.owner, "task_id": None, "source_key": key,
            "result_text": recorded, "reply": "", "done": True,
            "recorded_only": bool(recorded
                                  and str(result.turn_id) not in self.state.notification_sources()),
            "recorded_at": time.time(),
        })
