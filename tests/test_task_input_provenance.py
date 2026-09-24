"""Input authority follows accepted Git provenance through startup and live delivery."""
from dataclasses import replace

import pytest

from state_fixtures import close_task_slice
from steward_harness.runtime.contracts import RuntimeInputResult
from steward_harness.state import StateDatabase, TaskSpec
from test_conversations import _reply, _turn
from test_git_tasks import harness
from test_task_no_changes import InvestigationAdapter
from test_task_result_delivery import admitted
from test_task_runner_kernel import _repository


def test_accepted_assistant_answer_retains_its_source_after_restart(tmp_path):
    service, facts, cognition, _, task = admitted(tmp_path)
    state = service._state
    close_task_slice(state, task, "ask", detail="Can missing deployment evidence be waived?")
    cognition.replies.append(_reply(
        f'Use the existing limitation.\nTASK_ACTION: {{"task_id":"{task}",'
        '"action":"answer","text":"Operator clarification: retain the limitation."}'
    ))
    result = _turn(service, "answer-with-context", operator_id="harness:task-result")
    assert result.task_rejection is None
    fresh = StateDatabase(state.path)
    [(commit, kind, text, source)] = fresh.tasks.get(task).pending
    assert kind == "answer" and text.startswith("Operator clarification:")
    assert source == str(result.turn_id)
    assert f"source: assistant {source}" in fresh.tasks.git("show", "-s", "--format=%B", commit)
    assert fresh.tasks.git("show", "-s", "--format=%(trailers:key=Steward-Source,valueonly)", commit) == source


@pytest.mark.parametrize("source,origin,author", [
    ("operator", "operator", "operator"),
    ("turn_" + "a" * 32, "controller", "assistant:turn_" + "a" * 32),
    ("controller:task-query:" + "b" * 40, "controller", "controller:task-query:" + "b" * 40),
    (None, "controller", "unverified:unknown"),
])
def test_input_origin_survives_restart_and_reaches_startup_and_live_context(tmp_path, source, origin, author):
    bare, clone = _repository(tmp_path)
    delivered = []

    class StreamingAdapter(InvestigationAdapter):
        capabilities = replace(InvestigationAdapter.capabilities, ongoing_input=True)

        def execute(self, request):
            assert f'"origin": "{origin}"' in request.prompt
            assert f'"author": "{author}"' in request.prompt
            assert "initial context" in request.prompt
            def send(message):
                delivered.append(message)
                request.on_input_result(RuntimeInputResult(message.source_id, "accepted"))
            request.on_input_ready(send)
            runner.state.tasks.input(task, "note", "live context", source=source)
            runner.flush_inputs()
            runner.flush_inputs()
            return super().execute(request)

    state, runner, _, _ = harness(tmp_path / "state", bare, clone, StreamingAdapter())
    task, _ = state.tasks.create(TaskSpec("app", "Inspect", "Read retained context."))
    if source == "operator":
        state.tasks.note(task, "initial context")
    else:
        state.tasks.input(task, "note", "initial context", source=source)
    # Fresh controller reads provenance from Git; no SQL turn lookup is required.
    runner.state = StateDatabase(state.path)
    runner.state.tasks.transports = runner.transports
    runner.state.tasks.default_provider = "claude"
    runner.prepare(task)
    assert len(delivered) == 1
    assert delivered[0].origin == origin and delivered[0].author == author
    assert delivered[0].text == "note: live context"
    assert not tuple((k, t) for _, k, t, _ in runner.state.tasks.get(task).pending)
