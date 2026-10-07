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
from steward_harness.deployment_approval import DeploymentApprovals


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

# What a person is told about a target is its outcome, never its progress.
# Pending, busy, awaiting evidence and a moved ref are the loop doing its job;
# on a live instance they were two thirds of every deploy's messages, and each
# one handed to an owning task cost a full model turn to say "nothing to do".
# Only these statuses are outcomes: live at the desired revision, or not
# getting there.
FAILED = frozenset({"failed", "blocked", "failed-evidence"})
# A failure is an outcome only once it has stood this long. The controller's
# own shutdown drain makes every observe fail for a pass or two, and a target
# that recovers before anyone could act has nothing to report. The clock
# starts at the first failure since the target was last satisfied at this
# revision, so flapping between failure and busy still accumulates. Five
# minutes: long enough to span a restart, short against a real outage.
FAILURE_PERSISTS_SECONDS = 300.0


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
        # name -> (revision, monotonic time of the first failure since it was
        # last satisfied there). In memory for the same reason: a restart is a
        # fresh look, and a failure that outlives it is still told.
        self._failing: dict[str, tuple[str | None, float]] = {}

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

    def _outcome(self, name, status):
        """What this observation means to a person, or None when it is progress."""
        if status == "satisfied":
            return "satisfied"
        failing = self._failing.get(name)
        if status in FAILED and failing and time.monotonic() - failing[1] >= FAILURE_PERSISTS_SECONDS:
            return "failed"
        return None

    def _retain_result(self, name, message, repository, revision, observed, status):
        # Only the exact published outcome supplies a task recipient. A push
        # without an owning task still owes the operator its outcome.
        outcome = self._outcome(name, status)
        if outcome is None:
            return
        receipts =[json.loads(path.read_text()) for path in
                    self.state.result_receipt_path("").parent.glob("*.json")]
        owners = []
        for task in self.state.tasks.all():
            task_id, definition = str(task.task_id), task.definition
            if (revision is None or definition.repository != repository or not definition.owner
                    or task.landed != revision or task.landed_nothing):
                continue
            owners.append((task_id, definition.owner))
        for task_id, owner in owners or [(None, None)]:
            previous = max((r for r in receipts if r.get("target") == name
                            and r.get("task_id") == task_id),
                           key=lambda r: r["sequence"], default={})
            # One message per outcome per revision; driver prose and error text
            # vary by poll and are not a new outcome. Receipts retained before
            # outcomes existed name a status: read every failure as one.
            was_revision, was = (previous.get("observation") or [None, None])[:2]
            was = "failed" if was in FAILED else was
            if (was_revision, was) == (revision, outcome):
                continue
            identity = [revision, outcome]
            sequence = previous.get("sequence", 0) + 1
            source = "target_result:" + hashlib.sha256(json.dumps(
                [name, task_id, previous.get("source_key"), identity], sort_keys=True,
            ).encode()).hexdigest()
            at = datetime.now(timezone.utc).isoformat()
            short = (revision or "an unresolved revision")[:12]
            # Satisfied is whatever the driver says it is; its own words say
            # what that meant this time, so "reached" never claims more.
            details = f"\n{observed.details[:1000]}" if observed and observed.details else ""
            if outcome == "failed":
                reply = f"{name} is not reaching {short}.\n{message}"
            elif (was_revision, was) == (revision, "failed"):
                reply = f"{name} recovered and reached {short}.{details}"
            else:
                reply = f"{name} reached {short}.{details}"
            receipt = {
                "owner": owner, "task_id": task_id, "source_key": source,
                "target": name, "sequence": sequence, "observation": identity,
                "result_text": f"Target observation at {at}\nRepository: {repository}\n"
                               f"Desired revision: {revision}\n{message}\n"
                               + (f"Observed: {observed.model_dump_json()}" if observed else "No driver observation available."),
            }
            # Live is fully stated by the observation, so an owner's model could
            # only restate it. A failure is owed the owner's assessment.
            if task_id is None or outcome == "satisfied":
                receipt["reply"] = reply
            self.state.save_result_receipt(receipt)

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
                self._failing.pop(name, None)
            elif status != "unchanged":
                self._satisfied.pop(name, None)
            if status in FAILED and (name not in self._failing or self._failing[name][0] != revision):
                self._failing[name] = (revision, time.monotonic())
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
            if self.config.deployment_operators and not DeploymentApprovals(
                    self.state, self.config.deployment_operators).require(
                        "target", name, revision, target.model_dump(mode="json")):
                return report("awaiting-approval", f"{name}: awaiting operator approval for {revision}")
            self.call(name, "apply", repository, revision)
            observed = self.call(name, "observe", repository, revision)
            if observed.blocked:
                return report("blocked", f"{name}: blocked at {revision}; {observed.details}")
            if observed.ready and observed.revision == revision:
                return report("satisfied", f"{name}: satisfied at {revision}")
            return report("pending", f"{name}: not yet observed at {revision}; {observed.details}")
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError, GitTransportError) as error:
            return report("failed", f"{name}: failed: {type(error).__name__}: {redact_command_output(str(error))}")
