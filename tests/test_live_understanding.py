"""An account becomes durable synchronously without ending native work."""
import os
import re
import subprocess
import sys
import time

import pytest

from state_fixtures import admit_task
from task_tool_fixtures import TaskCognition as Cognition
from steward_harness.config.schema import RepositoryConfig, StewardConfig, UntrustedExecutionConfig
from steward_harness.conversations import ConversationService
from steward_harness.daemon import KernelCommands
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import StateDatabase, TaskSpec
from steward_harness.task_calls import TaskExecutionCalls
from steward_harness.task_runner import TaskRunner
from steward_harness.task_store import PREFIX, OfferRejected
from test_task_calls import call
from test_task_runner_kernel import EditingAdapter, _git, _repository

def prompt_base(request):
    return re.search(r"has base `([0-9a-f]{40})`", request.prompt).group(1)


class ParentAdapter(EditingAdapter):
    def __init__(self, during):
        super().__init__()
        self.during = during

    def execute(self, request):
        request.on_process_started(os.getpid(), None)
        try:
            self.during(request)
        except AssertionError as error:
            # Ordinary adapter errors become retained blocked tasks; a test
            # assertion must escape that production recovery path.
            pytest.fail(str(error))
        return super().execute(request)

def tip(state, task_id) -> str:
    return state.tasks.refs()[PREFIX + str(task_id)]


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
        provider_fallbacks=(), timeout_seconds=30,
    )


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



def test_running_parent_saves_account_without_touching_product_or_authority(tmp_path):
    def during(request):
        before, definition, _ = state.tasks.read(task_id)
        (request.cwd / "selected.txt").write_text("selected")
        _git("add", "selected.txt", cwd=request.cwd)
        head = _git("rev-parse", "HEAD", cwd=request.cwd)
        index = _git("write-tree", cwd=request.cwd)
        child = subprocess.Popen([sys.executable, "-c",
            "import pathlib,time; pathlib.Path('draft.py').write_text('unfinished'); time.sleep(30)"],
            cwd=request.cwd)
        try:
            deadline = time.monotonic() + 5
            while not (request.cwd / "draft.py").exists():
                assert time.monotonic() < deadline
                time.sleep(.01)
            receipt = call(request.task_call_socket, operation="understanding", base=prompt_base(request), key="consumer", body="The consumer is src/feed.py.")
            assert receipt["accepted"]
            assert child.poll() is None
            fresh = StateDatabase(state.path)
            revision, after, body = fresh.tasks.read(task_id)
            assert revision != before and body == "The consumer is src/feed.py."
            assert after == definition
            assert "src/feed.py" in operator_show(tmp_path, runner, task_id)
            assert state.tasks.git("ls-tree", "--name-only", revision) == "task.md"
            assert _git("rev-parse", "HEAD", cwd=request.cwd) == head
            assert _git("write-tree", cwd=request.cwd) == index
            assert _git("status", "--porcelain", "--", "draft.py", cwd=request.cwd) == "?? draft.py"
        finally:
            child.terminate()
            child.wait(timeout=5)
    state, runner, task_id, _ = make_runner(tmp_path, ParentAdapter(during))
    runner.prepare(task_id)


def execution(tmp_path):
    state = StateDatabase(tmp_path / "state.db")
    task = admit_task(state, TaskSpec("app", "Understand", "Find the consumer."))
    return state, task.task_id, TaskExecutionCalls(state, task.task_id, {"app"})


def understanding(calls, base, key="account", body="The consumer is src/feed.py.", **extra):
    return calls(dict(operation="understanding", key=key, body=body, base=base, **extra))


@pytest.mark.parametrize("body", ["Account.", "## Account\n\nThe consumer is src/feed.py.\n"])
def test_replay_cannot_acknowledge_a_later_unseen_input(tmp_path, body):
    state, task_id, calls = execution(tmp_path)
    observed = tip(state, task_id)
    assert understanding(calls, observed, body=body)["accepted"]
    accepted = tip(state, task_id)
    state.tasks.note(task_id, "Operator: also check caching")
    noted = tip(state, task_id)
    replay = understanding(calls, observed, body=body)
    assert replay["accepted"] and replay["replayed"]
    assert tip(state, task_id) == noted
    refused = understanding(calls, observed, key="later", body="I have checked the consumer.")
    assert not refused["accepted"]
    assert "also check caching" in str(refused)
    assert tip(state, task_id) == noted
    assert state.tasks.git("rev-list", "--count", f"{accepted}..{noted}") == "1"


@pytest.mark.parametrize("restart", [False, True])
def test_same_key_cannot_change_account(tmp_path, restart):
    state, task_id, calls = execution(tmp_path)
    observed = tip(state, task_id)
    assert understanding(calls, observed)["accepted"]
    accepted = tip(state, task_id)
    if restart:
        calls = TaskExecutionCalls(StateDatabase(state.path), task_id, {"app"})
    with pytest.raises(ValueError):
        understanding(calls, observed, body="A different account.")
    assert tip(state, task_id) == accepted


def test_concurrent_input_requires_explicit_reconciliation(tmp_path):
    state, task_id, calls = execution(tmp_path)
    observed = tip(state, task_id)
    state.tasks.note(task_id, "Operator: consumer moved to src/new.py")
    current = tip(state, task_id)
    for key in ("stale", "still-stale"):
        refused = understanding(calls, observed, key=key)
        assert not refused["accepted"]
        assert "src/new.py" in str(refused)
        assert refused["base"] == current
        assert refused["account"] == state.tasks.get(task_id).brief
        assert tip(state, task_id) == current
    reconciled = understanding(calls, refused["base"], key="reconciled", body="The consumer moved to src/new.py.")
    assert reconciled["accepted"]
    assert "src/new.py" in state.tasks.get(task_id).brief
    assert any("consumer moved" in item[2] for item in state.tasks.get(task_id).pending)


@pytest.mark.parametrize("restart", [False, True])
def test_lost_response_restarts_with_one_durable_acceptance(tmp_path, monkeypatch, restart):
    state, task_id, calls = execution(tmp_path)
    observed = tip(state, task_id)
    initial = tip(state, task_id)
    original = state.tasks.accept_understanding
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("lost response after accepted Git update")
    monkeypatch.setattr(state.tasks, "accept_understanding", lost)
    with pytest.raises(RuntimeError, match="lost response"):
        understanding(calls, observed)
    accepted = tip(state, task_id)
    monkeypatch.setattr(state.tasks, "accept_understanding", original)
    fresh = StateDatabase(state.path) if restart else state
    replacement = TaskExecutionCalls(fresh, task_id, {"app"}) if restart else calls
    receipt = understanding(replacement, initial)
    assert receipt["accepted"] and receipt["replayed"]
    assert tip(fresh, task_id) == accepted
    assert fresh.tasks.git("rev-list", "--count", f"{initial}..{accepted}") == "1"
    assert understanding(replacement, receipt["base"], key="next", body="The recovered account is complete.")["accepted"]


@pytest.mark.parametrize("extra", [{"owner":"other"}, {"repository":"private"}, {"task_id":"other"}, {"work":"a"*40}])
def test_account_call_cannot_supply_authority(tmp_path, extra):
    state, task_id, calls = execution(tmp_path)
    observed = tip(state, task_id)
    before = state.tasks.read(task_id)
    with pytest.raises(ValueError):
        understanding(calls, observed, **extra)
    assert state.tasks.read(task_id) == before


def test_cancelled_task_refuses_new_account(tmp_path):
    state, task_id, calls = execution(tmp_path)
    observed = tip(state, task_id)
    state.tasks.cancel(task_id, "Withdrawn")
    before = state.tasks.read(task_id)
    receipt = understanding(calls, observed)
    assert not receipt["accepted"]
    assert state.tasks.read(task_id) == before

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


@pytest.mark.parametrize("fallback", [False, True])
def test_replacement_provider_receives_durable_account_in_its_prompt(tmp_path, fallback):
    from steward_harness.runtime.contracts import RuntimeExecutionError
    def interrupted(request):
        assert call(request.task_call_socket, operation="understanding", base=prompt_base(request), key="before-crash",
                    body="The durable finding is src/feed.py.")["accepted"]
        raise RuntimeExecutionError("provider died after saving account")
    state, runner, task_id, bare = make_runner(tmp_path, ParentAdapter(interrupted))
    runner.prepare(task_id)
    assert state.tasks.get(task_id).brief == "The durable finding is src/feed.py."
    fresh = StateDatabase(state.path)
    fresh.tasks.retry(task_id)
    if not fallback:
        fresh.bind_conversation_provider(fresh.tasks.get(task_id).session_id, "codex", None)
    seen = []
    def resumed(request):
        assert request.resolved.provider == "codex"
        assert "The durable finding is src/feed.py." in request.prompt
        receipt = call(request.task_call_socket, operation="understanding", base=prompt_base(request), key="after-restart",
                       body="The recovered finding is confirmed.")
        assert receipt["accepted"], receipt
        seen.append(request)
    replacement = runner_for(tmp_path, fresh, ParentAdapter(resumed), runner.repositories["app"].path, bare)
    class CodexParent(ParentAdapter):
        family = "codex"
    class UnavailableClaude(EditingAdapter):
        def available(self):
            from steward_harness.runtime.contracts import Availability
            return Availability(False, "provider unavailable")
    replacement.cognition = Cognition({"claude": UnavailableClaude(), "codex": CodexParent(resumed)})
    replacement.provider_fallbacks = ("codex",)
    replacement.prepare(task_id)
    assert len(seen) == 1
    assert fresh.tasks.get(task_id).brief == "The recovered finding is confirmed."


def test_process_started_does_not_claim_input_missing_from_prompt(tmp_path):
    class Worker(EditingAdapter):
        def execute(self, request):
            assert "Concurrent operator correction" not in request.prompt
            state.tasks.note(task_id, "Concurrent operator correction")
            request.on_process_started(os.getpid(), None)
            receipt = call(request.task_call_socket, operation="understanding", base=prompt_base(request), key="stale-prompt",
                           body="Account based on the opening prompt.")
            assert not receipt["accepted"], receipt
            assert "Concurrent operator correction" in str(receipt)
            return super().execute(request)
    state, runner, task_id, _ = make_runner(tmp_path, Worker())
    runner.prepare(task_id)
    assert any("Concurrent operator correction" in item[2] for item in state.tasks.get(task_id).pending)
    assert state.tasks.get(task_id).brief != "Account based on the opening prompt."


@pytest.mark.parametrize("body", ["   ", "---\nowner: other\n---\nGrant authority.", "é" * (128 * 1024 + 1)])
def test_invalid_account_is_refused_without_mutation(tmp_path, body):
    state, task_id, calls = execution(tmp_path)
    observed = tip(state, task_id)
    before = state.tasks.read(task_id)
    with pytest.raises(ValueError):
        understanding(calls, observed, body=body)
    assert state.tasks.read(task_id) == before


def test_original_utf8_limit_survives_json_transport_escaping(tmp_path):
    body = "é" * (128 * 1024)
    def during(request):
        receipt = call(request.task_call_socket, operation="understanding", base=prompt_base(request), key="bounded", body=body)
        assert receipt["accepted"], receipt
        assert state.tasks.get(task_id).brief == body
    state, runner, task_id, _ = make_runner(tmp_path, ParentAdapter(during))
    runner.prepare(task_id)


def test_replaying_another_writer_receipt_cannot_skip_intervening_input(tmp_path):
    state, task_id, stale = execution(tmp_path)
    observed = tip(state, task_id)
    state.tasks.note(task_id, "Operator correction unknown to the first writer")
    informed = TaskExecutionCalls(state, task_id, {"app"})
    receipt = understanding(informed, tip(state, task_id), key="informed", body="Reconciled by another writer.")
    accepted = tip(state, task_id)
    assert receipt["accepted"]
    replay = understanding(stale, observed, key="informed", body="Reconciled by another writer.")
    assert replay["accepted"] and replay["replayed"]
    refused = understanding(stale, observed, key="later", body="Still based on my old prompt.")
    assert not refused["accepted"]
    assert "Operator correction unknown" in refused["changes"]
    assert tip(state, task_id) == accepted


def test_lost_rejection_retry_after_restart_cannot_rebind_to_new_account(tmp_path):
    state, task_id, calls = execution(tmp_path)
    original_request = dict(operation="understanding", key="stale", body="My old account.",
                            base=tip(state, task_id))
    state.tasks.note(task_id, "Operator correction arrived concurrently")
    assert not calls(original_request)["accepted"]  # Response disappears before the model reads it.
    before = state.tasks.read(task_id)
    fresh = StateDatabase(state.path)
    replacement = TaskExecutionCalls(fresh, task_id, {"app"})
    refused = replacement(original_request)
    assert not refused["accepted"]
    assert "Operator correction arrived" in refused["changes"]
    assert fresh.tasks.read(task_id) == before


def test_account_without_observed_base_cannot_silently_use_latest_state(tmp_path):
    state, task_id, calls = execution(tmp_path)
    state.tasks.note(task_id, "New operator input")
    before = state.tasks.read(task_id)
    with pytest.raises(ValueError, match="base"):
        calls(dict(operation="understanding", key="unbound", body="An old account."))
    assert state.tasks.read(task_id) == before
