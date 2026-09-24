"""Accepted work asks about ownership and receives target outcomes on its owner."""
from dataclasses import replace
import json

import pytest

from steward_harness.config.schema import TargetConfig
from steward_harness.kernel import StewardKernel
from steward_harness.state import ConversationId, TaskSpec, TaskStatus
from steward_harness.targets import Targets
from test_conversations import FakeCognition, _reply, _service
from test_git_tasks import harness
from test_rewrite_convergence import setup_procedures
from test_task_no_changes import InvestigationAdapter
from test_task_runner_kernel import _repository


def test_query_answers_same_task_and_survives_crash_before_answer(tmp_path, monkeypatch):
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, statuses, reconciler = harness(tmp_path / "state", bare, clone, adapter)
    owner = "telegram:17"
    peer, _ = state.tasks.create(TaskSpec("app", "Repair consumer", "PRIVATE BODY"), owner="telegram:other")
    task, _ = state.tasks.create(TaskSpec("app", "Reflect", "Find current ownership"), owner=owner)
    execute = adapter.execute
    adapter.execute = lambda request: replace(execute(request), output=(
        'Inspect ownership.\nCOMMIT: inspect\nDISPOSITION: ask\n'
        'QUESTION: TASK_QUERY: {"repository":"app","text":"consumer"}'))
    answer = runner._answer_query
    monkeypatch.setattr(runner, "_answer_query", lambda _: None)
    runner.prepare(task)
    assert state.tasks.get(task).status is TaskStatus.WAITING
    assert state.pending_task_result_for(ConversationId(owner)) is None
    # Accepted ask survives; normal task owner supplies its answer on next pass.
    kernel = StewardKernel(state, reconciler, runner)
    work = dict(kernel.owners())[("task", task)]
    monkeypatch.setattr(runner, "_answer_query", answer)
    adapter.execute = execute
    work()
    kernel.stop()
    prompt = adapter.requests[-1].prompt
    assert str(peer) in prompt and "another conversation" in prompt
    assert "PRIVATE BODY" not in prompt and "telegram:other" not in prompt
    assert "accepted_revision" in prompt and "observed_at" in prompt
    assert '"origin": "controller"' in prompt
    assert '"author": "controller:task-query:' in prompt
    assert any("Controller ownership observation" in text
               for _, _, text, _ in state.tasks.get(task).inputs)
    assert "TASK_QUERY:" not in (state.tasks.get(task).reason or "")
    assert not tuple((k, t) for _, k, t, _ in state.tasks.get(task).pending)


@pytest.mark.parametrize("query", ['{"repository":"secret","text":""}', '{"repository":"app","text":"","write":true}', 'not json'])
def test_query_rejects_ungranted_repository_and_bad_shape_without_leaking(tmp_path, query):
    from steward_harness.task_query import ownership_answer
    bare, clone = _repository(tmp_path)
    state, runner, statuses, _ = harness(tmp_path / "state", bare, clone, InvestigationAdapter())
    task, _ = state.tasks.create(TaskSpec("app", "Reflect", "Query"))
    state.tasks.create(TaskSpec("secret", "Secret title", "Secret body"))
    result = ownership_answer(state.tasks, task, "TASK_QUERY: " + query, runner.repositories)
    assert result.startswith("Task query rejected:")
    assert "Secret title" not in result and "Secret body" not in result


def test_target_feedback_follows_exact_owner_transitions_and_restarts(tmp_path, monkeypatch):
    # These transitions all occur at one revision, which the observation
    # throttle would otherwise collapse into a single look. Live, the loop still
    # sees every one of them, just up to SATISFIED_REOBSERVE_SECONDS later; the
    # subject here is which receipts reach which owner, so observe every pass.
    monkeypatch.setattr("steward_harness.targets.SATISFIED_REOBSERVE_SECONDS", 0.0)
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
    assert [r["observation"][1] for r in receipts] == ["satisfied", "blocked", "satisfied"]
    cognition = FakeCognition([_reply(""), _reply("Deployment requires repair."), _reply("Recovered.")])
    service = _service(root, cognition)
    # Skip already-covered publication receipt: this journey exercises later target evidence.
    for item in (old, task):
        key = f"task_result:{item}:{state.tasks.get(item).outcome}:done"
        state.save_result_receipt(dict(owner=state.tasks.read(item)[1].owner, task_id=str(item), source_key=key, done=True))
    owner = ConversationId("telegram:17")
    assert service.deliver_task_result(owner, send=lambda *_: pytest.fail("routine target sent")) == ""
    sent = []
    def fail(*args):
        sent.append(args)
        raise OSError("transport offline")
    with pytest.raises(OSError):
        service.deliver_task_result(owner, send=fail)
    restarted = _service(root, cognition)
    restarted.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert sent[0] == sent[1]
    assert len(cognition.requests) == 2
    restarted.deliver_task_result(owner, send=lambda *args: sent.append(args))
    assert len(cognition.requests) == 3 and sent[-1][0] == "Recovered."
    assert "Desired revision: " + revision in cognition.requests[-1].prompt
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
                                  'TASK_QUERY: {"repository":"app","text":"consumer"}', runner.repositories)
        locked = ownership_answer(state.tasks, requester,
                                  'TASK_QUERY: {"repository":"app","text":"consumer 0"}', runner.repositories)
        assert '"status": "running"' in locked
    document = json.loads(answer.partition("\n")[2])
    assert document["truncated"] and len(document["tasks"]) == 10
    matching = ownership_answer(state.tasks, requester,
                                'TASK_QUERY: {"repository":"app","text":"consumer 0"}', runner.repositories)
    assert '"status": "queued"' in matching
    assert '"ownership": "same conversation"' in matching
    assert "Private body" not in answer and "telegram:17" not in answer
    assert len(answer) <= 8000
