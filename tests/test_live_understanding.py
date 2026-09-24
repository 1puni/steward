"""A running native parent makes its account durable without ending execution.

The offer is an immutable blob under one task-local ref in the untrusted
repository. The turn's offer watcher reads it through the broker; acceptance is
a body-only exact-base commit in accepted task Git, acknowledged through the
live input channel. These tests use real Git and real processes with an
in-process adapter; the native adapters' own wire paths are exercised in
``test_live_understanding_adapters``.
"""

from __future__ import annotations

import subprocess
import sys
import time
import zlib
from dataclasses import replace
from pathlib import Path

import pytest

from state_fixtures import admit_task
from steward_harness.cognition import Cognition
from steward_harness.config.schema import RepositoryConfig, StewardConfig, UntrustedExecutionConfig
from steward_harness.conversations import ConversationService
from steward_harness.daemon import KernelCommands
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.runtime.contracts import (
    SESSION_WORKSPACE_CAPABILITIES,
    RuntimeInputResult,
)
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import StateDatabase, TaskSpec, TaskStatus
from steward_harness.task_runner import OFFER_LIMIT, OFFER_REF, TaskRunner
from steward_harness.task_store import PREFIX, OfferRejected
from steward_harness.web.tasks import TaskBoard
from test_task_runner_kernel import EditingAdapter, _git, _repository, publish_task, task_status


def offer(cwd: Path, task_id, base: str, body: str) -> str:
    """What the native parent runs: hash an immutable offer, point the ref at it."""
    return point(cwd, task_id, f"Steward-Base: {base}\n\n{body}\n".encode())


def point(cwd: Path, task_id, content: bytes) -> str:
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=cwd, check=True,
                          input=content, capture_output=True).stdout.decode().strip()
    _git("update-ref", OFFER_REF.format(task_id=task_id), blob, cwd=cwd)
    return blob


def tip(state, task_id) -> str:
    return state.tasks.refs()[PREFIX + str(task_id)]


def eventually(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.02)


class _Statuses:
    def refresh(self):
        return self

    def status(self, task):
        return TaskStatus.RUNNING


# -- the store's acceptance decision ------------------------------------------------


def test_acceptance_changes_only_the_body_and_replays_its_first_decision(tmp_path):
    state = StateDatabase(tmp_path / "state.db")
    task = admit_task(state, TaskSpec("app", "Understand", "Find the consumer."))
    base, before, _ = state.tasks.read(task.task_id)
    blob = "b" * 40

    accepted, created = state.tasks.accept_understanding(task.task_id, blob, base, "Now I know X.")

    assert created
    sha, after, body = StateDatabase(state.path).tasks.read(task.task_id)
    assert sha == accepted and body == "Now I know X."
    assert after == before
    assert state.tasks.get(task.task_id).pending == ()
    assert state.tasks.git("rev-list", "--count", f"{base}..{accepted}") == "1"
    # Replay after a lost acknowledgement: the original decision, no new commit.
    assert state.tasks.accept_understanding(task.task_id, blob, base, "Now I know X.") == (accepted, False)
    assert tip(state, task.task_id) == accepted


def test_stale_offer_cannot_erase_operator_edits_or_cancellation(tmp_path):
    state = StateDatabase(tmp_path / "state.db")
    task = admit_task(state, TaskSpec("app", "Understand", "Find the consumer."))
    base = tip(state, task.task_id)
    state.tasks.note(task.task_id, "operator: the consumer moved")

    with pytest.raises(OfferRejected, match="changed since") as appended:
        state.tasks.accept_understanding(task.task_id, "c" * 40, base, "Stale account.")
    # The refusal carries what the offerer has not seen, not only an identity.
    assert appended.value.revision == tip(state, task.task_id)
    assert "operator: the consumer moved" in appended.value.changes
    assert any("operator: the consumer moved" in item[2] for item in state.tasks.get(task.task_id).pending)

    # An operator rewrite of the body (e.g. arriving through the task remote).
    noted = tip(state, task.task_id)
    with state.tasks.lease:
        rewritten = state.tasks._commit(task.task_id, noted, state.tasks.read(task.task_id)[1],
                                        "Operator rewrote the plan.", "operator edit")
    with pytest.raises(OfferRejected) as replaced:
        state.tasks.accept_understanding(task.task_id, "e" * 40, noted, "Stale again.")
    assert replaced.value.changes == "The current accepted account is:\n\nOperator rewrote the plan."
    assert tip(state, task.task_id) == rewritten

    state.tasks.cancel(task.task_id, "withdrawn")
    current = tip(state, task.task_id)
    with pytest.raises(OfferRejected, match="cancelled"):
        state.tasks.accept_understanding(task.task_id, "d" * 40, current, "After withdrawal.")
    assert tip(state, task.task_id) == current
    assert state.tasks.read(task.task_id)[1].hold == "cancelled"


# -- a running parent with a working child ------------------------------------------


class ParentAdapter(EditingAdapter):
    """A native parent that keeps working while it offers its understanding."""

    capabilities = replace(SESSION_WORKSPACE_CAPABILITIES, ongoing_input=True)

    def __init__(self, during, *, deliver=True):
        super().__init__()
        self.during = during
        self.deliver = deliver
        self.delivered = []

    def execute(self, request):
        def send(message):
            if not self.deliver:
                raise RuntimeError("native turn gone")
            self.delivered.append(message)
            request.on_input_result(RuntimeInputResult(message.source_id, "accepted"))
        request.on_input_ready(send)
        request.on_session_started("claude-session")
        self.during(request)
        return super().execute(request)


def make_runner(tmp_path, adapter):
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    task = admit_task(state, TaskSpec("app", "Understand", "Find the consumer."),
                      provider="claude")
    return state, runner_for(tmp_path, state, adapter, clone, bare), task.task_id, bare


def runner_for(tmp_path, state, adapter, clone, bare):
    return TaskRunner(
        state=state,
        repositories={"app": RepositoryConfig(path=str(clone), remote_url=str(bare))},
        transports={"app": ControllerGitTransport(
            tmp_path / "controller.db", "app", str(bare), "main", allow_local=True)},
        worktrees_root=tmp_path / "worktrees",
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
        cognition=Cognition({"claude": adapter}),
        provider_fallbacks=(), timeout_seconds=30, poll_seconds=0.05,
    )


def messages(adapter):
    return [m.text for m in adapter.delivered if m.origin == "controller"]


def replies(adapter, count):
    eventually(lambda: len(messages(adapter)) >= count)
    return messages(adapter)


def operator_show(tmp_path, runner, task_id):
    config = StewardConfig.model_validate({
        "identity": {"name": "Steward", "slug": "test"},
        "provider": {"default_family": "claude", "fallback_families": [],
                     "workdir": str(tmp_path), "state_db": str(tmp_path / "state.db")},
    })
    service = ConversationService(
        runner.state, runner.cognition, provider_order=("claude",), profile="balanced",
        workspace=tmp_path, timeout_seconds=15)
    commands = KernelCommands(config, runner.state, service, None, {}, runner.broker)
    return commands("task", f"show {task_id}", 1, 7, 1)


# The parent's own delegated worker: a separate process editing a product file.
_CHILD = """
import pathlib, sys, time
draft, heartbeat = pathlib.Path('draft.py'), pathlib.Path(sys.argv[1])
while True:
    draft.write_text(f'# half written {time.time()}\\n')
    heartbeat.write_text(str(time.time()))
    time.sleep(0.05)
"""


def test_running_parent_offers_understanding_while_child_works(tmp_path):
    seen = {}

    def during(request):
        cwd = request.cwd
        task_id = seen["task"]
        opened = tip(state, task_id)
        assert f"it is {opened}" in request.prompt
        assert OFFER_REF.format(task_id=task_id) in request.prompt
        # The parent delegates: a child keeps editing product files. One change
        # is staged by the parent, one is left dirty by the child.
        heartbeat = tmp_path / "heartbeat"
        child = subprocess.Popen([sys.executable, "-c", _CHILD, str(heartbeat)], cwd=cwd)
        try:
            eventually(heartbeat.exists)
            (cwd / "staged.txt").write_text("staged, not committed\n")
            _git("add", "staged.txt", cwd=cwd)
            head, index = _git("rev-parse", "HEAD", cwd=cwd), _git("write-tree", cwd=cwd)

            first = offer(cwd, task_id, opened, "## Understanding\n\nThe consumer is src/feed.py.")
            [ack] = replies(adapter, 1)
            accepted = tip(state, task_id)
            assert accepted != opened
            assert ack == (f"Steward accepted understanding offer {first} as accepted revision "
                           f"{accepted}. Use Steward-Base: {accepted} for your next offer.")
            # The operator inspects the accepted account while work runs.
            assert "src/feed.py" in StateDatabase(state.path).tasks.get(task_id).brief
            shown = operator_show(tmp_path, runner, task_id)
            assert f"{task_id} — running" in shown and "The consumer is src/feed.py." in shown
            detail = TaskBoard(state).detail(task_id)
            assert detail["status"] == "running" and "src/feed.py" in detail["brief"]
            assert child.poll() is None
            beat = float(heartbeat.read_text() or 0)
            eventually(lambda: float(heartbeat.read_text() or 0) > beat)
            assert _git("rev-parse", "HEAD", cwd=cwd) == head
            assert _git("write-tree", cwd=cwd) == index
            assert _git("status", "--porcelain", "--", "draft.py", cwd=cwd) == "?? draft.py"
            # Nothing reached publication, and the product tree was not swept in.
            definition = state.tasks.read(task_id)[1]
            assert definition.work is None and state.tasks.get(task_id).landed is None
            assert state.tasks.git("ls-tree", "--name-only", accepted) == "task.md"

            # The same blob again: no second decision and no second reply.
            _git("update-ref", "-d", OFFER_REF.format(task_id=task_id), cwd=cwd)
            _git("update-ref", OFFER_REF.format(task_id=task_id), first, cwd=cwd)
            time.sleep(0.3)
            assert tip(state, task_id) == accepted and len(messages(adapter)) == 1

            state.tasks.note(task_id, "check the archive consumer too")
            runner.flush_inputs()
            assert adapter.delivered[-1].text == "note: check the archive consumer too"
            stale = offer(cwd, task_id, accepted, "## Understanding\n\nStale second offer.")
            refused = replies(adapter, 2)[-1]
            noted = tip(state, task_id)
            assert refused.startswith(f"Steward did not accept understanding offer {stale}")
            assert "check the archive consumer too" in refused
            assert refused.endswith(f"Steward-Base: {noted}.")
            assert "Stale second offer" not in state.tasks.get(task_id).brief

            offer(cwd, task_id, noted, "## Understanding\n\nsrc/feed.py and the archive consumer.")
            replies(adapter, 3)
            assert "archive consumer" in state.tasks.get(task_id).brief
            assert child.poll() is None
            # The accepted account is canonical; no product-side prose copy exists.
            assert not (cwd / "tasks" / f"{task_id}.md").exists()
            state.tasks.note(task_id, "late note after the second offer")
            runner.flush_inputs()  # delivered live; the closing checkpoint consumes it
        finally:
            child.kill()
            child.wait()
        (cwd / "draft.py").unlink()

    adapter = ParentAdapter(during)
    state, runner, task_id, bare = make_runner(tmp_path, adapter)
    seen["task"] = task_id

    runner.prepare(task_id)

    history = state.tasks.git("log", "--first-parent", "--format=%s", tip(state, task_id)).splitlines()
    assert history.count("accept understanding") == 2
    assert not any("reconcil" in subject.lower() for subject in history)
    definition = state.tasks.read(task_id)[1]
    assert definition.resume is None and definition.hold is None
    body = state.tasks.get(task_id).brief
    # Closure keeps the owner's account, the note it never wrote down, and its checkpoint.
    assert body == "## Understanding\n\nsrc/feed.py and the archive consumer."
    assert any("late note after the second offer" in item[2] for item in state.tasks.get(task_id).inputs)
    assert "Concurrent task understanding" not in body
    # Acknowledgements are controller words, never consumed operator input.
    consumed = state.tasks.git("log", "-1", "--format=%(trailers:key=Steward-Consumed,valueonly)",
                               tip(state, task_id))
    assert "understanding-" not in consumed
    # Natural closure still goes through ordinary gates and publication.
    assert publish_task(runner) == task_id
    assert task_status(runner, task_id) is TaskStatus.DONE
    assert f"tasks/{task_id}.md" not in _git("ls-tree", "-r", "--name-only", "main", cwd=bare)
    assert "archive consumer" in state.tasks.get(task_id).brief


def _tampered(cwd: Path, task_id, base: str) -> None:
    """Replace a loose object's file with other content under the same ID.

    Git does not re-hash on read; a larger substitute is already refused by
    the bounded read, so this forges an equally plausible small offer.
    """
    genuine = point(cwd, task_id, f"Steward-Base: {base}\n\ngenuine\n".encode())
    forged = f"Steward-Base: {base}\n\nforged!\n".encode()
    path = Path(_git("rev-parse", "--path-format=absolute", "--git-path",
                     f"objects/{genuine[:2]}/{genuine[2:]}", cwd=cwd))
    path.chmod(0o644)
    path.write_bytes(zlib.compress(b"blob %d\0" % len(forged) + forged))


@pytest.mark.parametrize("content, reason", [
    (b"not an offer", "Steward-Base"),
    (b"Steward-Base: {base}\n\n   ", "nonblank"),
    (b"Steward-Base: {base}\n\n\xff\xfe account", "UTF-8"),
    (b"Steward-Base: {base}\n\n" + b"y" * OFFER_LIMIT, "at most"),
    (b"Steward-Base: {base}\n\n---\nrepository: elsewhere\nhold: null\n---\nbody", "not offerable"),
    (None, "does not match its object ID"),
], ids=["shape", "blank", "encoding", "oversized", "authority", "tampered"])
def test_malformed_or_authority_offers_fail_visibly_and_work_continues(tmp_path, content, reason):
    def during(request):
        task_id = seen["task"]
        base = tip(state, task_id)
        if content is None:
            _tampered(request.cwd, task_id, base)
        else:
            point(request.cwd, task_id, content.replace(b"{base}", base.encode()))
        [refused] = replies(adapter, 1)
        assert "did not accept" in refused and reason in refused
        assert tip(state, task_id) == base

    adapter = ParentAdapter(during)
    state, runner, task_id, _ = make_runner(tmp_path, adapter)
    seen = {"task": task_id}
    before = state.tasks.read(task_id)[1]

    runner.prepare(task_id)

    after = state.tasks.read(task_id)[1]
    assert after.repository == before.repository == "app"
    assert after.hold is None  # the healthy execution closed normally
    assert "accept understanding" not in state.tasks.git("log", "--format=%s", tip(state, task_id))


def test_evaluation_failure_is_reported_once_then_the_same_offer_is_accepted(tmp_path):
    calls = []

    def during(request):
        task_id = seen["task"]
        blob = offer(request.cwd, task_id, tip(state, task_id), "## Understanding\n\nRetry me.")
        eventually(lambda: "Retry me." in state.tasks.get(task_id).brief)
        accepted = tip(state, task_id)
        failure, ack = replies(adapter, 2)
        assert failure == (f"Steward could not evaluate understanding offer {blob}: RuntimeError. "
                           "Nothing was accepted yet; Steward will keep evaluating this offer "
                           "and reply when it has a decision.")
        assert ack.startswith(f"Steward accepted understanding offer {blob} as accepted revision {accepted}.")
        time.sleep(0.2)
        assert len(messages(adapter)) == 2
        assert len({m.source_id for m in adapter.delivered}) == 2

    adapter = ParentAdapter(during)
    state, runner, task_id, _ = make_runner(tmp_path, adapter)
    seen = {"task": task_id}
    original = state.tasks.accept_understanding

    def flaky(*args):
        calls.append(args)
        if len(calls) <= 3:
            raise RuntimeError("task Git accept failed")
        return original(*args)

    state.tasks.accept_understanding = flaky
    runner.prepare(task_id)
    assert len(calls) == 4  # three transient failures, one decision, no re-decision
    assert state.tasks.git("log", "--format=%s", tip(state, task_id)).count("accept understanding") == 1


def test_cancellation_during_a_live_turn_refuses_later_offers(tmp_path):
    def during(request):
        task_id = seen["task"]
        base = tip(state, task_id)
        state.tasks.cancel(task_id, "operator withdrew")
        offer(request.cwd, task_id, base, "## Understanding\n\nToo late.")
        [refused] = replies(adapter, 1)
        assert "task is cancelled" in refused

    adapter = ParentAdapter(during)
    state, runner, task_id, _ = make_runner(tmp_path, adapter)
    seen = {"task": task_id}
    runner.prepare(task_id)
    definition = state.tasks.read(task_id)[1]
    assert definition.hold == "cancelled" and definition.reason == "operator withdrew"
    assert "Too late." not in state.tasks.get(task_id).brief


def test_acceptance_before_lost_acknowledgement_recovers_in_fresh_controller(tmp_path):
    """The Git decision lands; its reply never arrives; the controller dies."""
    def during(request):
        task_id = seen["task"]
        seen["blob"] = offer(request.cwd, task_id, tip(state, task_id), "## Understanding\n\nRecovered.")
        eventually(lambda: "Recovered." in state.tasks.get(task_id).brief)
        seen["accepted"] = tip(state, task_id)
        time.sleep(0.3)  # undelivered replies are retried as replays, not new decisions
        raise KeyboardInterrupt  # the controller died here

    adapter = ParentAdapter(during, deliver=False)
    state, runner, task_id, _ = make_runner(tmp_path, adapter)
    seen = {"task": task_id}
    with pytest.raises(KeyboardInterrupt):
        runner.prepare(task_id)

    fresh = StateDatabase(state.path)
    assert "Recovered." in fresh.tasks.get(task_id).brief
    assert fresh.tasks.git("log", "--format=%s", tip(fresh, task_id)).count("accept understanding") == 1
    fresh.tasks.note(task_id, "operator note while the controller was down")

    # The next execution under a fresh controller is told the original identity
    # and the current base, without a second decision.
    def resumed(request):
        current = tip(fresh, task_id)
        assert f"it is {current}" in request.prompt
        [ack] = replies(successor, 1)
        assert ack == (f"Steward accepted understanding offer {seen['blob']} as accepted revision "
                       f"{seen['accepted']}. The current accepted revision is {current}. "
                       f"Use Steward-Base: {current} for your next offer.")

    successor = ParentAdapter(resumed)
    repository = runner.repositories["app"]
    runner_for(tmp_path, fresh, successor, Path(repository.path), repository.remote_url).prepare(task_id)
    assert fresh.tasks.git("log", "--format=%s", tip(fresh, task_id)).count("accept understanding") == 1


def test_reply_is_delivered_only_when_the_provider_accepts_it(tmp_path):
    results = iter(["rejected", "unresolved", "accepted"])

    class Refusing(ParentAdapter):
        def execute(self, request):
            def send(message):
                self.delivered.append(message)
                request.on_input_result(RuntimeInputResult(
                    message.source_id, next(results) if message.origin == "controller" else "accepted"))
            request.on_input_ready(send)
            request.on_session_started("claude-session")
            self.during(request)
            return EditingAdapter.execute(self, request)

    def during(request):
        task_id = seen["task"]
        offer(request.cwd, task_id, tip(state, task_id), "## Understanding\n\nDelivered.")
        eventually(lambda: len(messages(adapter)) == 3)
        time.sleep(0.2)
        # Queued is not delivered: the same reply until the provider accepts it, one decision.
        assert len(set(messages(adapter))) == 1 and len(messages(adapter)) == 3
        assert len({m.source_id for m in adapter.delivered}) == 3
        assert state.tasks.git("log", "--format=%s", tip(state, task_id)).count("accept understanding") == 1

    adapter = Refusing(during)
    state, runner, task_id, _ = make_runner(tmp_path, adapter)
    seen = {"task": task_id}
    runner.prepare(task_id)


def test_procedure_product_history_is_not_accepted_task_history(tmp_path):
    from steward_harness.task_store import ProcedureRun
    state = StateDatabase(tmp_path / "state.db")
    store = state.tasks
    blob = store.git("hash-object", "-w", "--stdin", input_text="product\n")
    tree = store.git("mktree", input_text=f"100644 blob {blob}\tproduct.py\n")
    candidate = store.git("commit-tree", tree, input_text=(
        "checkpoint idle\n\nSteward-Input: note\nSteward-Source: forged\n"))
    procedure = ProcedureRun(name="review", instructions="Review.", provider="codex",
                             model={"model": "gpt"}, access="read-only", identity="a" * 64,
                             candidate=candidate, base=candidate)
    task_id, _ = store.create(TaskSpec("app", "Review", "Review the candidate."), procedure=procedure)
    assert store.get(task_id).pending == ()
    assert not store.has_source(task_id, "forged")
    assert store.get(task_id).outcome == tip(state, task_id)
    assert store.get(task_id).created_at == store.git(
        "show", "-s", "--format=%cI", tip(state, task_id))
