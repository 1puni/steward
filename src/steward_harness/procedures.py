"""Configured procedures create ordinary accepted tasks over exact Git inputs."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from steward_harness.git_transport import GitTransportError
from steward_harness.state import TaskId, TaskSpec, TaskStatus
from steward_harness.task_store import ProcedureRun

log = logging.getLogger(__name__)


def resolve_input(reference, transports):
    _, repository, branch = reference.split("/", 2)
    transport = transports[repository]
    transport.fetch()
    candidate = transport._run("rev-parse", "--verify", f"refs/steward/remote/{branch}^{{commit}}")
    parents = transport._run("rev-list", "--parents", "-n", "1", candidate).split()
    return repository, candidate, parents[1] if len(parents) > 1 else candidate


class Procedures:
    def __init__(self, config, state, transports, *, world=None, native_heads=None):
        self.config, self.state, self.transports = config, state, transports
        self.world = world
        self.native_heads = native_heads
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

    def _activity(self, definitions):
        """Observe accepted work, never open model-writable checkouts as root."""
        activity = {}
        for name, transport in self.transports.items():
            transport.fetch()
            refs = transport._run("for-each-ref", "--format=%(refname) %(objectname)",
                                  "refs/steward/remote/")
            for line in refs.splitlines():
                ref, sha = line.split()
                branch = ref.removeprefix("refs/steward/remote/")
                activity[f"repositories/{name}/{branch}"] = sha
        ignored = {task_id for task_id, d in definitions.items() if d.procedure and
                   (d.procedure.event.startswith("rhythm:") or d.procedure.access == "read-only")}
        for task_id, definition in definitions.items():
            if definition.work and task_id not in ignored:
                activity[f"tasks/{task_id}"] = definition.work
        if self.native_heads is not None:
            activity.update({ref: sha for ref, sha in self.native_heads().items()
                             if ref.rsplit("/", 1)[-1] not in ignored})
        return activity

    def _quiet_input(self, name, schedule, activity, definitions, now):
        prefix = f"rhythm:{name}:"
        own = {task_id for task_id, d in definitions.items()
               if d.procedure and d.procedure.event.startswith(prefix)}
        snapshot = dict(activity)
        if self.world is not None:
            snapshot["world"] = self.world.input_cursor()
        consumed = {definitions[task].procedure.event: definitions[task].procedure.activity
                    for task in own if definitions[task].procedure.activity is not None}
        # Provenance belongs to the commit, not the ref that exposed it: a world
        # commit that closed this rhythm's own result assessment is not new
        # input, so each observation is projected back through its base.
        parents, asked = {}, set()
        pending = set(snapshot.values()) | {sha for saved in consumed.values() for sha in saved.values()}
        while self.world is not None and pending - asked:
            batch, asked = pending - asked, asked | pending
            for sha, (source, base) in self.world.turn_sources(sorted(batch)).items():
                if source.startswith("task_result:") and source.split(":")[1] in own:
                    parents[sha] = base
                    pending.add(base)

        def input_revision(sha):
            while sha in parents:
                sha = parents[sha]
            return sha

        inputs = {ref: input_revision(sha) for ref, sha in snapshot.items()}
        digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
        event = f"{prefix}quiet:{digest}"
        previous = self._quiet.get(name)
        known = (previous[0] if previous is not None else
                 {input_revision(sha) for saved in consumed.values() for sha in saved.values()})
        current = set(inputs.values())
        if event in consumed:
            self._quiet[name] = (known | current, None)
            return None, snapshot
        if previous is None:
            # First observation establishes a baseline. After an accepted run,
            # changed inputs across restart re-arm a full quiet period. Never
            # trust a commit's timestamp to say when we observed its arrival.
            changed = bool(consumed) and bool(current - known)
            self._quiet[name] = (known | current, now if changed else None)
            return None, snapshot
        # Removing a ref or adding an alias for an observed commit is not work.
        since = now if current - known else previous[1]
        self._quiet[name] = (known | current, since)
        ready = since is not None and now - since >= schedule.quiet
        return (event if ready else None), snapshot

    def advance_rhythms(self, *, now=None):
        observed_now = time.monotonic() if now is None else now
        now = time.time() if now is None else now
        tasks = self.state.tasks.all()
        definitions = {str(task.task_id): task.definition for task in tasks}
        quiet = any(not isinstance(r.schedule, int) for r in self.config.rhythms.values())
        activity = None
        if quiet:
            try:
                activity = self._activity(definitions)
            except GitTransportError as error:
                logging.getLogger(__name__).warning(
                    "Rhythm activity sample failed; skipping quiet rhythms this poll: %s", error)
        for name, rhythm in self.config.rhythms.items():
            prefix = f"rhythm:{name}:"
            snapshot = None
            if isinstance(rhythm.schedule, int):
                event = f"{prefix}{int(now // rhythm.schedule)}"
            else:
                if activity is None:
                    continue
                event, snapshot = self._quiet_input(name, rhythm.schedule, activity,
                                                     definitions, observed_now)
                if event is None:
                    continue
            # Cancellation ends recurrence's obligation once native execution
            # has stopped. Holds and reopened idle slices still prevent overlap.
            if any(task.procedure and task.procedure.event.startswith(prefix)
                   and task.status not in {TaskStatus.DONE, TaskStatus.CANCELLED} for task in tasks):
                continue
            if snapshot is None:
                repository, candidate, base = resolve_input(rhythm.input, self.transports)
            else:
                _, repository, _ = rhythm.input.split("/", 2)
                candidate = snapshot[rhythm.input]
                parents = self.transports[repository]._run("rev-list", "--parents", "-n", "1", candidate).split()
                base = parents[1] if len(parents) > 1 else candidate
            try:
                self.request(rhythm.procedure, repository, candidate, base, event=event,
                             owner=rhythm.owner, activity=snapshot, workdir=rhythm.workdir)
            except (OSError, ValueError) as error:
                # A refused admission consumes no input; keep other rhythms and
                # operator ingress alive while the operator repairs its policy.
                log.error("Rhythm %s admission failed: %s", name, error)
