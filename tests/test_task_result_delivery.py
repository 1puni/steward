"""Successive outcomes return to the same owner without replaying old assessments."""

from state_fixtures import prepare_turn, FakeRemote, close_task_slice
from test_conversations import FakeCognition, _reply, _service, _turn, _task_reply
import pytest
from steward_harness.state import TaskId
from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeUnavailable


def admitted(tmp_path):
    facts = FakeRemote()
    submit = _task_reply(dict(operation='submit', key='receipt', repository='app', title='Fix the receipt', brief='Report each outcome.'))
    cognition = FakeCognition([submit])
    service = _service(tmp_path, cognition)
    first = _turn(service, 'admit')
    return service, facts, cognition, first.conversation_id, TaskId(submit.receipts[0]['task_id'])


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
    assert len(cognition.requests) == 1


def test_identical_failure_after_retry_is_a_new_result(tmp_path):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    state = service._state
    for _ in range(2):
        state.tasks.hold(task_id, "blocked", 'Repository unavailable')
        cognition.replies.append(_reply(''))
        assert 'Repository unavailable' in service.deliver_task_result(owner, send=lambda *_: None)
        assert service.deliver_task_result(owner, send=lambda *_: None) is None
        state.tasks.retry(task_id)
    assert len(cognition.requests) == 1


def test_question_receipt_does_not_suppress_later_completion(tmp_path):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    state = service._state
    close_task_slice(state, task_id, 'ask', detail='Which source?')
    cognition.replies.append(_reply('The question is retained.'))
    _turn(service, f"task_result:{task_id}:{state.tasks.get(task_id).outcome}:waiting", 'Task waiting: Which source?', operator_id='harness:task-result')
    assert 'Which source?' in service.deliver_task_result(owner, send=lambda *_: None)
    state.tasks.answer(task_id, 'The configured source.')
    close_task_slice(state, task_id, 'idle')
    facts.land(state, task_id)
    cognition.replies.append(_reply(''))
    assert 'Task done:' in service.deliver_task_result(owner, send=lambda *_: None)


def test_send_failure_then_restart_preserves_selected_outcome(tmp_path):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    close_task_slice(service._state, task_id, 'ask', detail='Which source?')
    attempts = []
    def fail(text, key):
        attempts.append((text, key))
        raise OSError('transport unavailable')
    with pytest.raises(OSError):
        service.deliver_task_result(owner, send=fail)
    service._state.tasks.answer(task_id, 'Configured source.')
    restarted = _service(tmp_path, cognition)
    restarted._state.tasks.transports = {"app": facts}
    sent = []
    assert 'Which source?' in restarted.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert sent == attempts
    assert len(cognition.requests) == 1
    restarted.assess_task_result(owner)
    assert len(cognition.requests) == 1, "stale question must not start an assessment"
    assert restarted.deliver_task_result(owner, send=lambda *_: pytest.fail('duplicate')) is None


def test_crash_after_send_before_receipt_is_at_least_once(tmp_path, monkeypatch):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    close_task_slice(service._state, task_id, 'ask', detail='Which source?')
    original = service._state.save_result_receipt
    def crash(receipt):
        if receipt.get('done'):
            raise OSError('crash after transport accepted')
        original(receipt)
    monkeypatch.setattr(service._state, 'save_result_receipt', crash)
    sent = []
    with pytest.raises(OSError):
        service.deliver_task_result(owner, send=lambda *args: sent.append(args))
    restarted = _service(tmp_path, cognition)
    restarted._state.tasks.transports = {"app": facts}
    restarted.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert len(sent) == 2 and sent[0] == sent[1]
    assert restarted.deliver_task_result(owner, send=lambda *_: pytest.fail('duplicate')) is None
    assert len(cognition.requests) == 1


@pytest.mark.parametrize("old_reply", ["", "A material finding."])
def test_old_frozen_reply_replays_after_upgrade(tmp_path, old_reply):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    close_task_slice(service._state, task_id, 'ask', detail='Which source?')
    receipt = service._state.retain_pending_result(owner)
    receipt['reply'] = old_reply
    service._state.save_result_receipt(receipt)
    restarted = _service(tmp_path, cognition)
    sent = []
    assert restarted.deliver_task_result(owner, send=lambda *args: sent.append(args)) == old_reply
    assert len(sent) == bool(old_reply)
    assert len(cognition.requests) == 1


def test_waiting_task_keeps_notes_without_treating_them_as_answers(tmp_path):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    state = service._state
    close_task_slice(state, task_id, 'ask', detail='Which source?')
    state.tasks.note(task_id, 'Operator context.')
    cognition.replies.append(_task_reply(dict(operation='note', key='evidence', task_id=str(task_id), text='Additional evidence.')))
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
    assert "markers are retired" in result.task_rejection
    notes = tuple((k, t) for _, k, t, _ in state.tasks.get(task_id).pending)
    assert not notes
    restarted.accept_prepared(str(turn.turn_id))
    assert tuple((k, t) for _, k, t, _ in state.tasks.get(task_id).pending) == notes


FLAGGED = ("Existing finding remains owned; full retained evidence.\n"
           "NOTIFY: The receipt regressed and needs repair.")


def completed_review(tmp_path, *, event="rhythm:light:1", disposition="idle", findings=FLAGGED, notify=False):
    from steward_harness.task_store import ProcedureRun
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    close_task_slice(service._state, task_id, disposition,
                     detail="Which source?" if disposition == "ask" else None,
                     findings=findings)
    work = service._state.tasks.get(task_id).work_sha
    procedure = ProcedureRun(name="review", event=event, instructions="Review changes.",
                             provider="codex", model={"model": "gpt"}, access="read-only",
                             identity="a" * 64, candidate=work, base=work)
    service._state.tasks.change(task_id, lambda definition: definition.model_copy(
        update={"procedure": procedure}), message="retain review verdict")
    if notify:
        from steward_harness.task_calls import TaskExecutionCalls
        TaskExecutionCalls(service._state, task_id, {"app"})(dict(
            operation="notify", key="finding", text="The receipt regressed and needs repair."))
    return service, facts, cognition, owner, task_id


@pytest.mark.parametrize("findings", [
    "Existing finding remains owned; full retained evidence.",
    # What the launch reflections wrote when told to write nothing.
    "Nothing material has changed since the last reflection, so I'm reporting no findings.",
    "NOTIFY: NONE", FLAGGED,
])
def test_unflagged_scheduled_review_keeps_evidence_and_costs_no_turn(tmp_path, findings):
    import time
    service, facts, cognition, owner, task_id = completed_review(tmp_path, findings=findings)
    state = service._state
    assert state.tasks.get(task_id).quiet
    assert state.pending_task_result_conversations() == ()
    assert service.deliver_task_result(owner, send=lambda *_: pytest.fail("unflagged review sent")) is None
    # The evidence commit keeps the findings, and the silence is counted.
    assert findings.split()[0] in state.tasks.get(task_id).findings
    assert state.recorded_not_sent(time.time() - 60) == ["rhythm:light:1"]
    restarted = _service(tmp_path, cognition)
    restarted._state.tasks.transports = {"app": facts}
    assert restarted.deliver_task_result(owner, send=lambda *_: pytest.fail("replayed silence")) is None
    assert len(cognition.requests) == 1


def test_callable_notification_retries_without_assessment(tmp_path):
    service, facts, cognition, owner, task_id = completed_review(tmp_path, notify=True)
    attempted = []
    def unavailable(text, key):
        attempted.append((text, key))
        raise OSError("transport unavailable")
    with pytest.raises(OSError):
        service.deliver_task_result(owner, send=unavailable)
    assert attempted[0][0] == "The receipt regressed and needs repair."
    restarted = _service(tmp_path, cognition)
    restarted._state.tasks.transports = {"app": facts}
    sent = []
    restarted.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert sent == attempted and len(cognition.requests) == 1


@pytest.mark.parametrize("event,disposition", [("manual:review", "idle"), ("rhythm:light:1", "ask")])
def test_explicit_review_and_scheduled_question_still_deliver_evidence(tmp_path, event, disposition):
    service, facts, cognition, owner, task_id = completed_review(tmp_path, event=event, disposition=disposition)
    cognition.replies.append(_reply(""))
    sent = []
    service.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert len(sent) == 1 and "full retained evidence" in sent[0][0]


def test_notification_crash_after_send_keeps_at_least_once_semantics(tmp_path, monkeypatch):
    service, facts, cognition, owner, task_id = completed_review(tmp_path, notify=True)
    original = service._state.save_result_receipt
    def crash(receipt):
        if receipt.get("done"):
            raise OSError("crash after send")
        original(receipt)
    monkeypatch.setattr(service._state, "save_result_receipt", crash)
    sent = []
    with pytest.raises(OSError):
        service.deliver_task_result(owner, send=lambda *args: sent.append(args))
    restarted = _service(tmp_path, cognition)
    restarted.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert len(sent) == 2 and sent[0] == sent[1]
    assert len(cognition.requests) == 1


@pytest.mark.parametrize("narration", ["", "SILENT", "NOTIFY: extra message", "DISPOSITION: idle\ntrailing prose"])
def test_assessment_narration_never_sends_again(tmp_path, narration):
    service, facts, cognition, owner, task_id = completed_review(tmp_path, event="manual:review")
    cognition.replies.append(_reply(narration))
    sent = []
    service.deliver_task_result(owner, send=lambda *args: sent.append(args))
    service.assess_task_result(owner)
    assert len(sent) == 1 and "full retained evidence" in sent[0][0]
    assert "already been delivered" in cognition.requests[-1].prompt
    assert service.deliver_task_result(owner, send=lambda *_: pytest.fail("second send")) is None


def test_assessment_failure_does_not_send_again(tmp_path):
    service, facts, cognition, owner, task_id = completed_review(tmp_path, event="explicit:review")
    cognition.replies.append(RuntimeUnavailable("no capacity"))
    sent = []
    service.deliver_task_result(owner, send=lambda *args: sent.append(args))
    service.assess_task_result(owner)
    assert len(sent) == 1
    assert not service._state.pending_result_assessments()
    assert service.deliver_task_result(owner, send=lambda *_: pytest.fail('duplicate')) is None


def test_crash_after_assessment_acceptance_replays_without_another_turn(tmp_path, monkeypatch):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    close_task_slice(service._state, task_id, 'ask', detail='Which source?')
    service.deliver_task_result(owner, send=lambda *_: None)
    cognition.replies.append(_reply("Additional judgment."))
    original = service._state.save_result_receipt
    def crash(receipt):
        if receipt.get("assessment_done"):
            raise OSError("crash before assessment receipt")
        original(receipt)
    monkeypatch.setattr(service._state, "save_result_receipt", crash)
    with pytest.raises(OSError):
        service.assess_task_result(owner)
    restarted = _service(tmp_path, cognition)
    restarted._state.tasks.transports = {"app": facts}
    restarted.assess_task_result(owner)
    assert len(cognition.requests) == 2
    assert not restarted._state.pending_result_assessments()
    assert restarted.deliver_task_result(owner, send=lambda *_: pytest.fail('duplicate')) is None


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
    request = build_result_assessment_request(record, "Target observation: satisfied")

    assert len(request) < 20_000
    assert admitted in request
    assert "Target observation: satisfied" in request
    assert "more characters on the task's branch" in request
    short = build_result_assessment_request(admitted, "done")
    assert "task's branch" not in short


def _day_after(state, task_id, days=1):
    from datetime import datetime
    return datetime.fromisoformat(state.tasks.get(task_id).updated_at).timestamp() + days * 86_400 + 60


def test_open_ask_is_named_again_once_a_day_without_a_model_turn(tmp_path):
    # gg, 2026-09-25: the light rhythm's grant ask reached its owner once and
    # then sat for three days where only desk turns mentioned it.
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    state = service._state
    close_task_slice(state, task_id, 'ask', detail='May light read the repository clones?')
    cognition.replies.append(_reply(''))
    assert 'May light read' in service.deliver_task_result(owner, send=lambda *_: None)
    assert state.remind_open_tasks(now=_day_after(state, task_id, 0) - 120) == 0
    requests = len(cognition.requests)

    assert state.remind_open_tasks(now=_day_after(state, task_id)) == 1
    assert state.remind_open_tasks(now=_day_after(state, task_id)) == 0, "one digest per day"
    sent = []
    service.deliver_task_result(owner, send=lambda text, key: sent.append((text, key)))
    [(text, key)] = sent
    assert key.startswith(f"task_open:{owner}:")
    assert "Waiting on you for 1 day: Fix the receipt" in text
    assert "May light read the repository clones?" in text
    assert f"/task answer {task_id}" in text and f"/task cancel {task_id}" in text
    assert len(cognition.requests) == requests, "a reminder is not assessed by a model"

    assert state.remind_open_tasks(now=_day_after(state, task_id, 2)) == 1
    state.tasks.answer(task_id, 'Yes, read-only.')
    assert state.remind_open_tasks(now=_day_after(state, task_id, 3)) == 0, "answered is not open"


def test_blocked_task_owned_off_telegram_goes_to_the_operator_route(tmp_path):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    state = service._state
    state.tasks.hold(task_id, "blocked", 'No space left on device')
    state.tasks.change(task_id, lambda d: d.model_copy(update={"owner": "desk:0"}),
                       message="reassign to the desk")
    assert state.remind_open_tasks(now=_day_after(state, task_id, 3)) == 1
    [receipt] = [r for r in state.pending_result_receipts() if r["source_key"].startswith("task_open:")]
    assert receipt["owner"] is None and receipt["source_key"].startswith("task_open:operator:")
    assert "Blocked for 3 days" in receipt["reply"] and "No space left on device" in receipt["reply"]
    assert f"/task retry {task_id}" in receipt["reply"]


def test_result_delivers_while_owner_busy_and_after_clear_without_duplicate(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    entered, release = Event(), Event()
    def busy(request):
        entered.set()
        assert release.wait(5)
        return _reply('Conversation finished.')
    cognition.replies.append(busy)
    close_task_slice(service._state, task_id, 'ask', detail='Which source?')
    sent = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(_turn, service, 'busy-owner')
        try:
            assert entered.wait(1)
            service.deliver_task_result(owner, send=lambda *args: sent.append(args))
            assert len(sent) == 1 and not running.done()
            service._state.bind_conversation_provider(owner, 'codex', None)
            assert service.deliver_task_result(owner, send=lambda *_: pytest.fail('clear replay')) is None
        finally:
            release.set()
        from steward_harness.state import ConversationBusy
        with pytest.raises(ConversationBusy, match='lineage changed'):
            running.result()
    cognition.replies.append(_reply('Later judgment.'))
    service.assess_task_result(owner)
    service.assess_task_result(owner)
    assert len(cognition.requests) == 3
    assert service.deliver_task_result(owner, send=lambda *_: pytest.fail('late assessment')) is None


def test_reassigned_task_delivers_to_current_owner_and_skips_old_assessment(tmp_path):
    from steward_harness.state import ConversationId
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    close_task_slice(service._state, task_id, 'ask', detail='Which source?')
    other = ConversationId('telegram:other')
    service._state.tasks.change(task_id, lambda d: d.model_copy(update={'owner': str(other)}),
                               message='Assign current owner')
    assert service.deliver_task_result(owner, send=lambda *_: pytest.fail('wrong owner')) is None
    service.deliver_task_result(other, send=lambda *_: None)
    service._state.tasks.change(task_id, lambda d: d.model_copy(update={'owner': str(owner)}),
                               message='Reassign after delivery')
    service.assess_task_result(other)
    assert len(cognition.requests) == 1
    assert not service._state.pending_result_assessments()


def test_unprepared_legacy_rhythm_receipt_does_not_send_final_markers(tmp_path):
    service, facts, cognition, owner, task_id = completed_review(tmp_path)
    service._state.save_result_receipt(dict(owner=str(owner), task_id=str(task_id),
        source_key="legacy:done", result_text=FLAGGED, done=False))
    assert service.deliver_task_result(owner, send=lambda *_: pytest.fail("legacy marker sent")) == ""
    assert not service._state.pending_result_assessments()
    assert len(cognition.requests) == 1


def test_automatic_assessment_keeps_recorded_only_final_instruction(tmp_path):
    service, facts, cognition, owner, task_id = admitted(tmp_path)
    close_task_slice(service._state, task_id, 'ask', detail='Needs help')
    service.deliver_task_result(owner, send=lambda *_: None)
    cognition.replies.append(_reply('Recorded.'))
    service.assess_task_result(owner)
    prompt = cognition.requests[-1].prompt
    assert 'Final replies from automatic runs are recorded only' in prompt
    assert 'Your final reply is delivered to the operator' not in prompt
