"""Seed SQL state through accepted outputs; these helpers do not prove native execution."""

import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from itertools import count

from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.git import agent_git
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import (
    CheckpointDisposition,
    TaskId,
    TaskStatus,
)


def prepare_turn(state, event_id, *, world_root, base_sha, candidate_sha, output,
                 provider, model, provider_session_id, profile):
    """Claim, retain and (for a world turn) prepare, as one finished execution would."""
    from steward_harness.state import TurnId
    turn = state.get_turn(TurnId(event_id))
    state.claim_turn(turn.turn_id, episode_input=turn.input_text, world_root=world_root,
                     base_sha=base_sha)
    state.retain_output(turn.turn_id, output=output, provider=provider, model=model,
                        provider_session_id=provider_session_id, profile=profile,
                        generation=state.get_conversation(turn.conversation_id).generation)
    if world_root is not None:
        state.record_candidate(event_id, candidate_sha)


def accept_conversation_turn(
    state,
    turn_id,
    *,
    reply_text="Done.",
    provider="codex",
    model="gpt",
    provider_session_id=None,
    profile="balanced",
    spec=None,
):
    event_id = str(turn_id)
    prepare_turn(
        state,
        event_id,
        world_root=None,
        base_sha=None,
        candidate_sha=None,
        output=reply_text,
        provider=provider,
        model=model,
        provider_session_id=provider_session_id,
        profile=profile,
    )
    return state.accept_turn(
        event_id,
        visible_reply=reply_text,
        spec=spec,
        rejection=None,
    )


_ADMISSIONS = count()


def admit_task(state, spec, *, provider="codex", profile="balanced"):
    """Accept a Git task directly; no scheduled or conversational SQL turn."""
    from steward_harness.state import ConversationId, TaskOriginKind
    task_id, _ = state.tasks.create(spec, kind=TaskOriginKind.RHYTHM)
    state.open_conversation(ConversationId.for_task(task_id), provider=provider, profile=profile)
    return state.tasks.get(task_id)


def close_task_slice(
    state,
    task_id,
    disposition,
    *,
    opened_at=None,
    detail=None,
    findings=None,
):
    """End one execution slice: its commit, then what the store still owes."""
    task = state.tasks.get(task_id)
    tree = state.tasks.git("mktree", input_text="")
    parents = ("-p", task.work_sha) if task.work_sha else ()
    work_sha = state.tasks.git("commit-tree", tree, *parents,
                              input_text=f"fixture checkpoint\n\n{findings or ''}\n\nDisposition: {disposition}\nReason: {detail or ''}\n")
    return state.tasks.finish_slice(
        task_id,
        work_sha=work_sha,
        consumed_input_ids=frozenset(source for source, _, _, _ in task.pending),
        disposition=CheckpointDisposition(disposition),
        opened_at=opened_at or datetime.now(UTC).isoformat(),
        detail=detail,
    )


def advance(kernel):
    """Dispatch one kernel's owners once, waiting for none of them.

    This lived on `StewardKernel` as `tick()` and had no production caller: the
    daemon builds its own pass because it owns five lanes, not two. Five tests
    kept it alive, which the mandate forbids by name, so the driver moved here
    where it is honestly labelled as test scaffolding.
    """
    kernel.dispatch.reap()
    for key, work in kernel.owners():
        kernel.dispatch.submit(key, work)


@contextmanager
def running(daemon, poll_seconds=0.01):
    """Run one daemon the way `steward run` does, on a thread.

    Tests used to construct the services and let the daemon's own background
    loops drive them. There is one pass now and `run_forever` is what turns
    it, so a test wanting a live daemon runs the real entry point — including
    the lease, which `run_forever` takes for itself.
    """
    errors: list[BaseException] = []

    def run() -> None:
        try:
            daemon.run_forever(poll_seconds=poll_seconds)
        except BaseException as error:
            errors.append(error)

    owner = threading.Thread(target=run, name="steward-test-daemon")
    owner.start()
    try:
        yield errors
    finally:
        daemon._stop.set()
        owner.join(10)
    assert not owner.is_alive(), "the daemon did not stop"
    assert errors == [], errors


class FakeRemote:
    """A repository remote whose default branch contains exactly what it is told."""

    def __init__(self):
        self.landed: set[str] = set()

    def observed_tip(self):
        return ("f" * 40, None)

    def landing(self, work, tip):
        return "c" * 40 if work in self.landed else None

    def land(self, state, task_id, repository="app"):
        self.landed.add(state.tasks.get(task_id).work_sha)
        state.tasks.transports = {repository: self}
