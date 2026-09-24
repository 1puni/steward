"""Successive outcomes return to the same owner without replaying old assessments."""

from state_fixtures import prepare_turn, FakeRemote, close_task_slice
from test_conversations import FakeCognition, _reply, _service, _turn
import pytest
from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeUnavailable


def admitted(tmp_path):
    facts = FakeRemote()
    cognition = FakeCognition([_reply('On it.\nTASK_PROPOSAL: {"repository":"app","title":"Fix the receipt","brief":"Report each outcome."}')])
    service = _service(tmp_path, cognition)
    first = _turn(service, 'admit')
    return service, facts, cognition, first.conversation_id, first.task_admission.task_id


def test_two_questions_then_publication_each_return_once(tmp_path):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    state = service._state
    for question in ('Which source?', 'Which destination?'):
        close_task_slice(state, task_id, 'ask', detail=question)
        cognition.replies.append(_reply(''))
        assert state.pending_task_result_conversations() == (owner,)
        assert question in service.deliver_task_result(owner, send=lambda *_: None)
        assert service.deliver_task_result(owner, send=lambda *_: None) is None
        state.tasks.set_priority(task_id, 10)
        assert service.deliver_task_result(owner, send=lambda *_: None) is None
        state.tasks.answer(task_id, 'Use the configured one.')
    close_task_slice(state, task_id, 'idle')
    # Closing an idle slice is not publication. Do not consume its result early.
    assert service.deliver_task_result(owner, send=lambda *_: None) is None
    facts.land(state, task_id)
    cognition.replies.append(_reply(''))
    assert 'Task done:' in service.deliver_task_result(owner, send=lambda *_: None)
    restarted = _service(tmp_path, cognition)
    restarted._state.tasks.transports = {"app": facts}
    assert restarted.deliver_task_result(owner, send=lambda *_: None) is None
    assert state.pending_task_result_conversations() == ()
    assert len(cognition.requests) == 4


def test_identical_failure_after_retry_is_a_new_result(tmp_path):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    state = service._state
    for _ in range(2):
        state.tasks.hold(task_id, "blocked", 'Repository unavailable')
        cognition.replies.append(_reply(''))
        assert 'Repository unavailable' in service.deliver_task_result(owner, send=lambda *_: None)
        assert service.deliver_task_result(owner, send=lambda *_: None) is None
        state.tasks.retry(task_id)
    assert len(cognition.requests) == 3


def test_question_receipt_does_not_suppress_later_completion(tmp_path):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    state = service._state
    close_task_slice(state, task_id, 'ask', detail='Which source?')
    cognition.replies.append(_reply('The question is retained.'))
    _turn(service, f"task_result:{task_id}:{state.tasks.get(task_id).outcome}:waiting", 'Task waiting: Which source?', operator_id='harness:task-result')
    assert service.deliver_task_result(owner, send=lambda *_: None) is None
    state.tasks.answer(task_id, 'The configured source.')
    close_task_slice(state, task_id, 'idle')
    facts.land(state, task_id)
    cognition.replies.append(_reply(''))
    assert 'Task done:' in service.deliver_task_result(owner, send=lambda *_: None)


@pytest.mark.parametrize('assessment_error', [None, RuntimeExecutionError('provider stopped'), RuntimeUnavailable('no capacity')])
def test_result_survives_assessment_and_send_failure_then_restart(tmp_path, assessment_error):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    close_task_slice(service._state, task_id, 'ask', detail='Which source?')
    cognition.replies.append(assessment_error or _reply('Use the configured source.'))
    attempts = []
    def fail(text, key):
        attempts.append((text, key))
        raise OSError('transport unavailable')
    with pytest.raises(OSError):
        service.deliver_task_result(owner, send=fail)
    assert service._state.pending_task_result_conversations() == (owner,)
    # An operator answer may already advance the task while its old result is
    # waiting on transport. Delivery still owes the selected outcome.
    service._state.tasks.answer(task_id, 'Configured source.')
    restarted = _service(tmp_path, cognition)
    restarted._state.tasks.transports = {"app": facts}
    sent = []
    reply = restarted.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert 'Which source?' in reply
    assert ('assessment interrupted' in reply) == (assessment_error is not None)
    assert sent == attempts
    assert len(cognition.requests) == 2
    assert restarted.deliver_task_result(owner, send=lambda *args: sent.append(args)) is None
    assert len(sent) == 1


def test_crash_after_accepted_assessment_replays_without_repeating_cognition(tmp_path, monkeypatch):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    close_task_slice(service._state, task_id, 'ask', detail='Which source?')
    cognition.replies.append(_reply('Use the configured source.'))
    original = service._state.save_result_receipt
    def crash(receipt):
        if 'reply' in receipt:
            raise OSError('crash before transport')
        original(receipt)
    monkeypatch.setattr(service._state, 'save_result_receipt', crash)
    with pytest.raises(OSError):
        service.deliver_task_result(owner, send=lambda *_: pytest.fail('must not send'))
    restarted = _service(tmp_path, cognition)
    restarted._state.tasks.transports = {"app": facts}
    sent = []
    restarted.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert 'Use the configured source.' in sent[0][0]
    assert len(cognition.requests) == 2
    assert not restarted._state.pending_task_result_conversations()


@pytest.mark.parametrize("old_reply", ["SILENT", "A material finding."])
def test_old_assessment_replays_recorded_prompt_after_upgrade(tmp_path, monkeypatch, old_reply):
    import steward_harness.conversations as module
    service, facts, cognition, owner, task_id = completed_review(tmp_path)
    builder = module.build_result_assessment_request
    def old_builder(*args, **kwargs):
        return builder(*args, **kwargs) + "\nOld completion protocol: reply exactly SILENT."
    monkeypatch.setattr(module, "build_result_assessment_request", old_builder)
    cognition.replies.append(_reply(old_reply))
    save = service._state.save_result_receipt
    def crash(receipt):
        if "reply" in receipt:
            raise OSError("before receipt save")
        save(receipt)
    monkeypatch.setattr(service._state, "save_result_receipt", crash)
    with pytest.raises(OSError):
        service.deliver_task_result(owner, send=lambda *_: pytest.fail("premature send"))
    monkeypatch.setattr(module, "build_result_assessment_request", builder)
    # Both task account and configured prompt can change after acceptance.
    service._state.tasks.change(task_id, lambda definition: definition,
                               message="later context\n\nAdditional retained evidence.")
    restarted = _service(tmp_path, cognition)
    restarted._state.tasks.transports = {"app": facts}
    sent = []
    reply = restarted.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert reply == ("" if old_reply == "SILENT" else old_reply)
    assert len(sent) == int(bool(reply))
    assert len(cognition.requests) == 2
    assert restarted.deliver_task_result(owner, send=lambda *_: pytest.fail("duplicate")) is None


def test_waiting_task_keeps_notes_without_treating_them_as_answers(tmp_path):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    state = service._state
    close_task_slice(state, task_id, 'ask', detail='Which source?')
    state.tasks.note(task_id, 'Operator context.')
    cognition.replies.append(_reply(
        f'Context recorded.\nTASK_ACTION: {{"task_id":"{task_id}","action":"note","text":"Additional evidence."}}'
    ))
    result = _turn(service, 'note-waiting')
    assert result.task_rejection is None
    assert state.tasks.get(task_id).status.value == 'waiting'
    assert [text for _, text in tuple((k, t) for _, k, t, _ in state.tasks.get(task_id).pending)] == [
        'Operator context.', 'Additional evidence.',
    ]
    state.tasks.answer(task_id, 'Use the configured source.')
    assert state.tasks.get(task_id).status.value == 'queued'
    assert len(tuple((k, t) for _, k, t, _ in state.tasks.get(task_id).pending)) == 3


@pytest.mark.parametrize("allowed", [("app",), ()])
def test_pending_action_replays_with_repository_authority(tmp_path, allowed):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    state = service._state
    # Prepare an accepted model output without applying its action, as a crash
    # before world acceptance leaves it for startup replay.
    turn, _ = state.start_turn(owner, source_event_key="pending-note", operator_id="operator",
                            input_text="Retain this note.")
    prepare_turn(state, str(turn.turn_id), world_root=None, base_sha=None, candidate_sha=None,
        output=f'Noted.\nTASK_ACTION: {{"task_id":"{task_id}","action":"note","text":"Keep the evidence."}}',
        provider="codex", model="gpt", provider_session_id="session-1", profile="balanced")
    restarted = _service(tmp_path, cognition, allowed=allowed)
    restarted._state.tasks.transports = {"app": facts}
    result = restarted.accept_prepared(str(turn.turn_id))
    assert restarted._state.prepared_turn(str(turn.turn_id))["state"] == "completed"
    assert (result.task_rejection is None) == bool(allowed)
    notes = tuple((k, t) for _, k, t, _ in state.tasks.get(task_id).pending)
    assert bool(notes) == bool(allowed)
    restarted.accept_prepared(str(turn.turn_id))
    assert tuple((k, t) for _, k, t, _ in state.tasks.get(task_id).pending) == notes


def completed_review(tmp_path, *, event="rhythm:light:1", disposition="idle"):
    from steward_harness.task_store import ProcedureRun
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    close_task_slice(service._state, task_id, disposition,
                     detail="Which source?" if disposition == "ask" else None,
                     findings="Existing finding remains owned; full retained evidence.")
    work = service._state.tasks.get(task_id).work_sha
    procedure = ProcedureRun(name="review", event=event, instructions="Review changes.",
                             provider="codex", model={"model": "gpt"}, access="read-only",
                             identity="a" * 64, candidate=work, base=work)
    service._state.tasks.change(task_id, lambda definition: definition.model_copy(
        update={"procedure": procedure}), message="retain review verdict")
    return service, facts, cognition, owner, task_id


def test_silent_scheduled_review_keeps_evidence_and_stays_quiet_after_restart(tmp_path):
    service, facts, cognition, owner, task_id = completed_review(tmp_path)
    cognition.replies.append(_reply(""))
    pending = service._state.pending_task_result_for(owner)
    assert service.deliver_task_result(owner, send=lambda *_: pytest.fail("silent review sent")) == ""
    receipt = service._state.result_receipt(pending[2])
    assert receipt["done"] and receipt["reply"] == ""
    assert "full retained evidence" in receipt["result_text"]
    assert service._state.tasks.get(task_id).verdict == "fail"
    restarted = _service(tmp_path, cognition)
    restarted._state.tasks.transports = {"app": facts}
    assert restarted.deliver_task_result(owner, send=lambda *_: pytest.fail("replayed silence")) is None
    assert len(cognition.requests) == 2


def test_material_scheduled_review_sends_owner_summary_and_retries_exactly(tmp_path):
    service, facts, cognition, owner, task_id = completed_review(tmp_path)
    cognition.replies.append(_reply("A new failure needs repair."))
    attempted = []
    def unavailable(text, key):
        attempted.append((text, key))
        raise OSError("transport unavailable")
    with pytest.raises(OSError):
        service.deliver_task_result(owner, send=unavailable)
    assert attempted[0][0] == "A new failure needs repair."
    restarted = _service(tmp_path, cognition)
    restarted._state.tasks.transports = {"app": facts}
    sent = []
    restarted.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert sent == attempted and len(cognition.requests) == 2


@pytest.mark.parametrize("event,disposition", [("manual:review", "idle"), ("rhythm:light:1", "ask")])
def test_explicit_review_and_scheduled_question_still_deliver_evidence(tmp_path, event, disposition):
    service, facts, cognition, owner, task_id = completed_review(tmp_path, event=event, disposition=disposition)
    cognition.replies.append(_reply(""))
    sent = []
    service.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert len(sent) == 1 and "full retained evidence" in sent[0][0]


def test_scheduled_assessment_failure_remains_visible(tmp_path):
    service, facts, cognition, owner, task_id = completed_review(tmp_path)
    cognition.replies.append(RuntimeUnavailable("no capacity"))
    sent = []
    service.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert len(sent) == 1 and "assessment interrupted" in sent[0][0]
    assert "full retained evidence" in sent[0][0]


def test_crash_before_silent_receipt_commit_replays_accepted_assessment_once(tmp_path, monkeypatch):
    service, facts, cognition, owner, task_id = completed_review(tmp_path)
    cognition.replies.append(_reply(""))
    original = service._state.save_result_receipt
    def crash(receipt):
        if "reply" in receipt:
            raise OSError("crash before quiet receipt")
        original(receipt)
    monkeypatch.setattr(service._state, "save_result_receipt", crash)
    with pytest.raises(OSError):
        service.deliver_task_result(owner, send=lambda *_: pytest.fail("silent review sent"))
    restarted = _service(tmp_path, cognition)
    restarted._state.tasks.transports = {"app": facts}
    assert restarted.deliver_task_result(owner, send=lambda *_: pytest.fail("replayed silence")) == ""
    assert len(cognition.requests) == 2
    assert not restarted._state.pending_task_result_conversations()



def test_procedure_worker_uses_git_instead_of_injected_peer_state(tmp_path):
    from test_task_runner_kernel import _repository
    from test_git_tasks import harness
    from test_task_no_changes import InvestigationAdapter
    from test_rewrite_convergence import setup_procedures
    from steward_harness.state import TaskSpec
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, _, _ = harness(tmp_path / "state", bare, clone, adapter)
    config, procedures = setup_procedures(tmp_path, state, runner)
    candidate = runner.transports["app"].fetch()
    task = procedures.request("security-one", "app", candidate, candidate,
                              event="manual:first", activity={"world": "1" * 40})
    peer, _ = state.tasks.create(TaskSpec("app", "Unrelated obligation", "Unrelated peer context."))
    runner.prepare(task)
    prompt = adapter.requests[-1].prompt
    assert "git log" in prompt and "git show" in prompt
    assert "Git activity captured at admission" not in prompt
    assert str(peer) not in prompt and "Unrelated peer context" not in prompt
    assert "Provenance map" not in prompt
    assert not state.path.with_name(state.path.name + ".provenance").exists()


def test_a_settled_backlog_is_not_rewalked_on_every_pass(tmp_path):
    """Every pass tests every task ever accepted, and the walk is the task's whole history."""
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    state = service._state
    close_task_slice(state, task_id, 'idle')
    facts.land(state, task_id)
    cognition.replies.append(_reply('SILENT'))
    assert 'Task done:' in service.deliver_task_result(owner, send=lambda *_: None)
    assert state.pending_task_result_conversations() == ()

    store, walks = state.tasks, []
    original = store.git
    store.git = lambda *a, **k: (walks.append(a[0]), original(*a, **k))[1]
    for _ in range(5):
        state.pending_task_result_conversations()

    assert walks.count("log") == 0


def test_a_long_task_record_cannot_push_its_result_past_the_prompt_bound():
    """A 75-checkpoint task reached 184k characters; every result then failed unread."""
    from steward_harness.prompts import build_result_assessment_request

    admitted = "Build the phone gateway.\n"
    record = admitted + "".join(f"## {n} — continue · slice {n}\n" + "x" * 2400 + "\n" for n in range(75))
    request = build_result_assessment_request(record, "Target observation: satisfied", quiet=True)

    assert len(request) < 20_000
    assert admitted in request
    assert "Target observation: satisfied" in request
    assert "more characters on the task's branch" in request
    short = build_result_assessment_request(admitted, "done", quiet=True)
    assert "task's branch" not in short
