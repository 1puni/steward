"""Named targets converge through finite, installed observe/apply executables."""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
import os
import subprocess
import signal
import time

from pydantic import BaseModel, ConfigDict, Field

from steward_harness.procedures import resolve_input
from steward_harness.git_transport import GitTransportError
from steward_harness.git import redact_command_output
from steward_harness.lease import Lease, Busy


# Re-confirming a deployment that cannot have changed is not free. Observation
# is a process spawn by contract, and the included driver then imports pydantic
# and httpx, re-reads and cross-validates the whole controller configuration,
# and re-hashes the release tree against its staging receipt: ~2.2s of CPU per
# call, measured on a live instance. At a five-second poll that is most of a core spent
# learning a revision that has not moved. A target already observed satisfied
# at the revision its ref still names is therefore left alone for this long.
# Every other state — a moved ref, busy, blocked, pending, failed — observes on
# every pass, because those are the states that need the loop.
SATISFIED_REOBSERVE_SECONDS = 60.0


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str | None = Field(pattern=r"^[0-9a-f]{40}$")
    ready: bool
    busy: bool = False
    blocked: bool = False
    details: str = ""


class Targets:
    def __init__(self, config, state, transports, procedures):
        self.config, self.state = config, state
        self.transports, self.procedures = transports, procedures
        # name -> (revision, monotonic time observed satisfied). Deliberately in
        # memory: a controller restart re-observes once, which is the correct
        # answer to "did anything change while I was not running".
        self._satisfied: dict[str, tuple[str, float]] = {}

    def call(self, name, operation, repository, revision):
        target = self.config.targets[name]
        request = dict(target=name, repository=repository, revision=revision,
                       git_dir=str(self.transports[repository].git_dir))
        # This executable is installed authority, never resolved from candidate code.
        with subprocess.Popen([target.driver, operation], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True, cwd=self.state.path.parent,
            env={k: v for k, v in os.environ.items()
                if not k.startswith("GIT_") and k not in {"PYTHONPATH", "PYTHONHOME"}}) as process:
            try:
                stdout, _ = process.communicate(json.dumps(request), timeout=target.timeout_seconds)
            finally:
                # A driver may hand off to a supervisor, but may not leak
                # children into the controller's lifetime or timeout boundary.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            returncode = process.returncode
        if returncode:
            raise RuntimeError(f"target {name} {operation} failed (exit {returncode})")
        if operation == "observe":
            return Observation.model_validate_json(stdout)
        return None

    def advance(self, name, *, force=False):
        try:
            with Lease(self.state.path.parent, lock_name=f"target-{name}.lock"):
                if force:
                    self._satisfied.pop(name, None)
                result = self._advance(name)
                # A pass that reused the previous observation has no new outcome
                # to publish, and retaining one would report a state transition
                # that never happened.
                if result[4] != "unchanged":
                    self._retain_result(name, *result)
                return result[0]
        except Busy:
            return f"{name}: another controller operation holds its lock"

    def due(self, name):
        """Derive eligibility from the last observation and locally observed ref.

        No network work belongs on the pass thread. A moved mirrored ref is
        immediately eligible; otherwise the observation interval bounds both
        the next remote refresh and the next external health observation.
        The worker rechecks under its target lease before acting.
        """
        seen = self._satisfied.get(name)
        if seen is None or self._satisfied_age(name, seen[0]) is None:
            return True
        _, repository, branch = self.config.targets[name].ref.split("/", 2)
        try:
            revision = self.transports[repository]._run(
                "rev-parse", "--verify", f"refs/steward/remote/{branch}^{{commit}}")
        except GitTransportError:
            return True
        return revision != seen[0]

    def _retain_result(self, name, message, repository, revision, observed, status):
        # Only the exact published outcome supplies a task recipient. A push
        # without an owning task still owes an operator-visible transition.
        receipts = [json.loads(path.read_text()) for path in
                    self.state.result_receipt_path("").parent.glob("*.json")]
        owners = []
        for task in self.state.tasks.all():
            task_id, definition = str(task.task_id), task.definition
            if revision is None or definition.repository != repository or not definition.owner or task.landed != revision:
                continue
            owners.append((task_id, definition.owner))
        for task_id, owner in owners or [(None, None)]:
            previous = max((r for r in receipts if r.get("target") == name
                            and r.get("task_id") == task_id),
                           key=lambda r: r["sequence"], default={})
            # Driver prose may contain a timestamp on every poll. Delivery is
            # driven by state/revision transitions, not changes in that prose.
            identity = [revision, status, observed.model_dump(exclude={"details"}) if observed else None]
            if previous.get("observation") == identity:
                continue
            sequence = previous.get("sequence", 0) + 1
            source = "target_result:" + hashlib.sha256(json.dumps(
                [name, task_id, previous.get("source_key"), identity], sort_keys=True,
            ).encode()).hexdigest()
            at = datetime.now(timezone.utc).isoformat()
            self.state.save_result_receipt({
                "owner": owner, "task_id": task_id, "source_key": source,
                "target": name, "sequence": sequence, "observation": identity,
                "result_text": f"Target observation at {at}\nRepository: {repository}\n"
                               f"Desired revision: {revision}\n{message}\n"
                               + (f"Observed: {observed.model_dump_json()}" if observed else "No driver observation available."),
            } | ({"reply": f"{message}\nDesired revision: {revision or 'unresolved'}"}
                 if task_id is None else {}))

    def _satisfied_age(self, name, revision):
        """Seconds since this exact revision was observed satisfied, while that still stands in for an observation."""
        seen, at = self._satisfied.get(name, (None, 0.0))
        if seen != revision:
            return None
        age = time.monotonic() - at
        return age if age < SATISFIED_REOBSERVE_SECONDS else None

    def _advance(self, name):
        target = self.config.targets[name]
        _, repository, _ = target.ref.split("/", 2)
        revision = observed = None
        def report(status, message):
            if status == "satisfied":
                self._satisfied[name] = (revision, time.monotonic())
            elif status != "unchanged":
                self._satisfied.pop(name, None)
            return message, repository, revision, observed, status
        try:
            repository, revision, base = resolve_input(target.ref, self.transports)
            age = self._satisfied_age(name, revision)
            if age is not None:
                return report("unchanged", f"{name}: satisfied at {revision} {age:.0f}s ago; not re-observed")
            observed = self.call(name, "observe", repository, revision)
            if observed.busy:
                return report("busy", f"{name}: busy; {observed.details}")
            if observed.blocked:
                return report("blocked", f"{name}: blocked at {revision}; {observed.details}")
            # Target requirements review the complete tree, not merely
            # the last commit or a diff from a moving deployment baseline.
            base = revision
            evidence = self.procedures.require(target.requires, repository, revision, base)
            if evidence is None:
                return report("awaiting-evidence", f"{name}: awaiting required procedure evidence for {revision}")
            if evidence:
                return report("failed-evidence", f"{name}: required procedure failed: {evidence}")
            if observed.ready and observed.revision == revision:
                return report("satisfied", f"{name}: satisfied at {revision}")
            if resolve_input(target.ref, self.transports)[1] != revision:
                return report("ref-moved", f"{name}: desired ref moved; evidence must be re-evaluated")
            self.call(name, "apply", repository, revision)
            observed = self.call(name, "observe", repository, revision)
            if observed.blocked:
                return report("blocked", f"{name}: blocked at {revision}; {observed.details}")
            if observed.ready and observed.revision == revision:
                return report("satisfied", f"{name}: satisfied at {revision}")
            return report("pending", f"{name}: not yet observed at {revision}; {observed.details}")
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError, GitTransportError) as error:
            return report("failed", f"{name}: failed: {type(error).__name__}: {redact_command_output(str(error))}")
