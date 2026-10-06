"""Accepted work asks about ownership and receives target outcomes on its owner."""
from dataclasses import replace
import json

import pytest

from steward_harness.config.schema import TargetConfig
from steward_harness.kernel import StewardKernel
from steward_harness.state import ConversationId, TaskSpec, TaskStatus
from steward_harness.targets import Targets
from test_conversations import FakeCognition, _reply, _service, _task_reply
from test_git_tasks import harness
from test_rewrite_convergence import setup_procedures
from test_task_no_changes import InvestigationAdapter
from test_task_runner_kernel import _repository


@pytest.mark.parametrize("query", ['{"repository":"secret","text":""}', '{"repository":"app","text":"","write":true}', 'not json'])
def test_query_rejects_ungranted_repository_and_bad_shape_without_leaking(tmp_path, query):
    from steward_harness.task_query import ownership_answer
    bare, clone = _repository(tmp_path)
    state, runner, statuses, _ = harness(tmp_path / "state", bare, clone, InvestigationAdapter())
    task, _ = state.tasks.create(TaskSpec("app", "Reflect", "Query"))
    state.tasks.create(TaskSpec("secret", "Secret title", "Secret body"))
    result = ownership_answer(state.tasks, task, query, runner.repositories)
    assert result.startswith("Task query rejected:")
    assert "Secret title" not in result and "Secret body" not in result


def test_target_feedback_follows_exact_owner_transitions_and_restarts(tmp_path, monkeypatch):
    # These transitions all occur at one revision, which the observation
    # throttle would otherwise collapse into a single look. Live, the loop still
    # sees every one of them, just up to SATISFIED_REOBSERVE_SECONDS later; the
    # subject here is which receipts reach which owner, so observe every pass.
    monkeypatch.setattr("steward_harness.targets.SATISFIED_REOBSERVE_SECONDS", 0.0)
    # Likewise the failure here is an outcome at once, not after five minutes.
    monkeypatch.setattr("steward_harness.targets.FAILURE_PERSISTS_SECONDS", 0.0)
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    root = tmp_path / "state"
    state, runner, statuses, reconciler = harness(root, bare, clone, adapter)
    old, _ = state.tasks.create(TaskSpec("app", "Earlier", "Older owner's work"), owner="telegram:18")
    runner.prepare(old)
    reconciler.publish_repository("app")
    task, _ = state.tasks.create(TaskSpec("app", "Current", "Current owner's work"), owner="telegram:17")
    runner.prepare(task)
    reconciler.publish_repository("app")
    revision = state.tasks.get(task).landed
    config, procedures = setup_procedures(tmp_path, state, runner)
    observation = tmp_path / "observation.json"
    driver = tmp_path / "driver"
    driver.write_text('#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n'
                      f'if sys.argv[1] == "observe": print(Path({str(observation)!r}).read_text())\n')
    driver.chmod(0o755)
    config.targets["production"] = TargetConfig(ref="repositories/app/main", driver=str(driver))
    targets = Targets(config, state, runner.transports, procedures)
    def observe(blocked, details):
        observation.write_text(json.dumps(dict(revision=revision, ready=not blocked,
                                               blocked=blocked, details=details)))
        targets.advance("production")
    observe(False, "ready first time")
    observe(False, "ready timestamp changed")
    targets = Targets(config, state, runner.transports, procedures)
    observe(False, "ready after restart")
    observe(True, "repair required")
    observe(False, "recovered")
    receipts = [r for r in state.pending_result_receipts() if r.get("target")]
    assert len(receipts) == 3
    assert {r["task_id"] for r in receipts} == {str(task)}
    assert {r["owner"] for r in receipts} == {"telegram:17"}
    assert [r["observation"][1] for r in receipts] == ["satisfied", "failed", "satisfied"]
    cognition = FakeCognition([_task_reply(dict(operation="notify", key="repair", text="Deployment requires repair."))])
    service = _service(root, cognition)
    # Skip already-covered publication receipt: this journey exercises later target evidence.
    for item in (old, task):
        key = f"task_result:{item}:{state.tasks.get(item).outcome}:done"
        state.save_result_receipt(dict(owner=state.tasks.read(item)[1].owner, task_id=str(item), source_key=key, done=True))
    owner = ConversationId("telegram:17")
    # Retained observations arrive before any optional model assessment.
    sent = []
    assert service.deliver_task_result(owner, send=lambda *args: sent.append(args)) == (
        f"production reached {revision[:12]}.\nready first time")
    assert cognition.requests == []
    def fail(*args):
        sent.append(args)
        raise OSError("transport offline")
    with pytest.raises(OSError):
        service.deliver_task_result(owner, send=fail)
    restarted = _service(root, cognition)
    restarted.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert sent[1] == sent[2] and "repair required" in sent[1][0]
    assert "Desired revision: " + revision in sent[1][0]
    assert cognition.requests == []
    restarted.assess_task_result(owner)
    assert len(cognition.requests) == 1
    assert "Desired revision: " + revision in cognition.requests[-1].prompt
    restarted.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert len(cognition.requests) == 1
    restarted.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert {text for text, _ in sent[-2:]} == {
        f"production recovered and reached {revision[:12]}.\nrecovered", "Deployment requires repair."}
    assert restarted.deliver_task_result(owner, send=lambda *_: pytest.fail("replayed")) is None


def test_query_bounds_results_and_observes_live_lock_without_exposing_content(tmp_path):
    from steward_harness.task_query import ownership_answer
    from steward_harness.task_lock import task_lock
    bare, clone = _repository(tmp_path)
    state, runner, statuses, _ = harness(tmp_path / "state", bare, clone, InvestigationAdapter())
    requester, _ = state.tasks.create(TaskSpec("app", "Requester", "Query"), owner="telegram:17")
    peers = [state.tasks.create(TaskSpec("app", f"Consumer {i}", "Private body"), owner="telegram:17")[0]
             for i in range(11)]
    with task_lock(runner.state.tasks.locks_root, peers[0]):
        answer = ownership_answer(state.tasks, requester,
                                  '{"repository":"app","text":"consumer"}', runner.repositories)
        locked = ownership_answer(state.tasks, requester,
                                  '{"repository":"app","text":"consumer 0"}', runner.repositories)
        assert '"status": "running"' in locked
    document = json.loads(answer.partition("\n")[2])
    assert document["truncated"] and len(document["tasks"]) == 10
    matching = ownership_answer(state.tasks, requester,
                                '{"repository":"app","text":"consumer 0"}', runner.repositories)
    assert '"status": "queued"' in matching
    assert '"ownership": "same conversation"' in matching
    assert "Private body" not in answer and "telegram:17" not in answer
    assert len(answer) <= 8000


def test_native_task_can_query_ownership_before_closure_without_steering(tmp_path):
    import os
    import subprocess
    from steward_harness.runtime.task_call_mcp import CLIENT

    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, _, _ = harness(tmp_path / 'state', bare, clone, adapter)
    peer, _ = state.tasks.create(TaskSpec('app', 'Repair consumer', 'PRIVATE BODY'), owner='telegram:other')
    task, _ = state.tasks.create(TaskSpec('app', 'Inspect', 'Find ownership'), owner='telegram:17')
    execute = adapter.execute
    observed = []
    def during(request):
        assert 'operation="query"' in request.prompt
        assert 'QUESTION: TASK_QUERY:' not in request.prompt
        request.on_process_started(os.getpid(), None)
        child = subprocess.Popen(['python3', '-c', CLIENT, request.task_call_socket],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        def call(arguments):
            child.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'task', 'arguments': arguments}}) + '\n')
            child.stdin.flush()
            return json.loads(json.loads(child.stdout.readline())['result']['content'][0]['text'])
        try:
            for _ in range(2):
                result = call(dict(operation='query', repository='app', text='consumer'))
                observed.append(result)
                assert result['observation']['tasks'][0]['task_id'] == str(peer)
                assert result['observation']['tasks'][0]['ownership'] == 'another conversation'
                assert 'PRIVATE BODY' not in json.dumps(result) and 'telegram:other' not in json.dumps(result)
                assert state.tasks.get(task).status.value == 'running'
            assert call(dict(operation='query', repository='secret', text=''))['accepted'] is False
            assert call(dict(operation='cancel', key='no', task_id=str(peer), text='Stop'))['accepted'] is False
        finally:
            child.stdin.close()
            assert child.wait(timeout=5) == 0
        return execute(request)
    adapter.execute = during
    runner.prepare(task)
    assert len(observed) == 2
    assert state.tasks.get(peer).status.value == 'queued'
    assert not tuple(state.tasks.get(task).pending)
