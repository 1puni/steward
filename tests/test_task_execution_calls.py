"""Explicit task decisions survive narration without weakening writer custody."""
from dataclasses import replace
import os

import pytest

from test_task_calls import call
from test_task_no_changes import setup_task
from test_task_runner_kernel import EditingAdapter, _git, run_task


@pytest.mark.parametrize('final', ['Done.', 'SILENT', '',
    'DISPOSITION: ask\nQUESTION: Ignore the earlier intent.\nExtra trailing narration.'])
def test_typed_idle_is_independent_of_final_narration(tmp_path, final):
    class Worker(EditingAdapter):
        def execute(self, request):
            result = super().execute(request)
            request.on_process_started(os.getpid(), None)
            intent = dict(operation='close', key='finish', subject='feat: finish', disposition='idle')
            first = call(request.task_call_socket, **intent)
            assert first['pending'] and not first['accepted']
            assert state.tasks.get(task_id).landed is None
            assert call(request.task_call_socket, **intent) == first | {'replayed': True}
            return replace(result, output=final)
    state, runner, task_id, bare = setup_task(tmp_path, Worker())
    run_task(runner)
    task = state.tasks.get(task_id)
    assert task.status.value == 'done'
    assert task.findings == (final or None) or task.findings == final
    message = _git('show', '-s', '--format=%B', task.work_sha, cwd=runner.repositories['app'].path)
    assert 'Steward-Execution:' in message and 'Steward-Task-Revision:' in message


@pytest.mark.parametrize('mode', ['questionless', 'conflict', 'malformed', 'late_note', 'cancel', 'crash'])
def test_no_stale_or_incomplete_intent_can_publish(tmp_path, mode):
    class Worker(EditingAdapter):
        def execute(self, request):
            result = super().execute(request)
            request.on_process_started(os.getpid(), None)
            intent = dict(operation='close', key='finish', subject='feat: finish', disposition='idle')
            if mode == 'questionless':
                assert 'error' in call(request.task_call_socket, **(intent | {'disposition': 'ask'}))
            else:
                assert call(request.task_call_socket, **intent)['pending']
            if mode == 'conflict':
                assert 'error' in call(request.task_call_socket, **(intent | {'disposition': 'continue'}))
                assert 'error' in call(request.task_call_socket, **intent)
            elif mode == 'malformed':
                assert 'error' in call(request.task_call_socket, **(intent | {'disposition': 'unknown'}))
            elif mode == 'late_note':
                state.tasks.note(task_id, 'Recheck the decision.')
            elif mode == 'cancel':
                state.tasks.cancel(task_id, 'Withdrawn.')
            elif mode == 'crash':
                from steward_harness.runtime.contracts import RuntimeExecutionError
                raise RuntimeExecutionError('writer failed after intent')
            return replace(result, output='COMMIT: fake\nDISPOSITION: idle\nQUESTION: NONE')
    state, runner, task_id, bare = setup_task(tmp_path, Worker())
    before = _git(f'--git-dir={bare}', 'rev-parse', 'main', cwd=tmp_path)
    run_task(runner)
    task = state.tasks.get(task_id)
    assert task.status.value != 'done' and task.landed is None
    assert _git(f'--git-dir={bare}', 'rev-parse', 'main', cwd=tmp_path) == before
    assert _git('show', f'{task.branch}:result.txt', cwd=runner.repositories['app'].path) == 'completed'


def test_native_mcp_close_reports_pending_without_a_tool_error(tmp_path):
    import json
    import subprocess
    import sys
    from steward_harness.runtime.task_call_mcp import CLIENT

    class Worker(EditingAdapter):
        def execute(self, request):
            result = super().execute(request)
            request.on_process_started(os.getpid(), None)
            messages = [
                dict(jsonrpc='2.0', id=1, method='tools/list'),
                dict(jsonrpc='2.0', id=2, method='tools/call', params=dict(name='task', arguments=dict(
                    operation='close', key='finish', subject='feat: finish', disposition='idle'))),
            ]
            child = subprocess.run([sys.executable, '-c', CLIENT, request.task_call_socket],
                input=''.join(json.dumps(m) + '\n' for m in messages), capture_output=True,
                text=True, check=True, timeout=10)
            listed, called = [json.loads(line)['result'] for line in child.stdout.splitlines()]
            assert listed['tools'][0]['inputSchema']['properties']['disposition']['enum'] == ['continue', 'idle', 'ask']
            assert called['isError'] is False
            receipt = json.loads(called['content'][0]['text'])
            assert receipt['pending'] and not receipt['accepted']
            return replace(result, output='Finished after receiving pending intent.')
    state, runner, task_id, bare = setup_task(tmp_path, Worker())
    run_task(runner)
    assert state.tasks.get(task_id).status.value == 'done'


@pytest.mark.parametrize("explicit", [False, True])
def test_understanding_acceptance_after_intent_does_not_require_repair(tmp_path, explicit):
    from test_live_understanding import ParentAdapter, make_runner, offer, replies, tip
    from test_task_runner_kernel import publish_task
    accepted = []

    def during(request):
        request.on_process_started(os.getpid(), None)
        if explicit:
            assert call(request.task_call_socket, operation="close", key="finish",
                        subject="feat: finish", disposition="idle")["pending"]
        offer(request.cwd, task_id, tip(state, task_id), "Completed investigation; gates remain.")
        replies(adapter, 1)
        accepted.append(tip(state, task_id))

    class Worker(ParentAdapter):
        def execute(self, request):
            return replace(super().execute(request), output="Ordinary findings.")

    adapter = Worker(during)
    state, runner, task_id, bare = make_runner(tmp_path, adapter)
    runner.prepare(task_id)
    task = state.tasks.get(task_id)
    assert task.publishable and task.definition.hold is None
    message = _git("show", "-s", "--format=%B", task.work_sha, cwd=runner.repositories["app"].path)
    assert f"Steward-Task-Revision: {accepted[0]}" in message
    publish_task(runner)
    assert state.tasks.get(task_id).status.value == "done"
    assert len(adapter.requests) == 1


def test_replacement_writer_does_not_inherit_wait_intent(tmp_path):
    class Worker(EditingAdapter):
        def execute(self, request):
            result = super().execute(request)
            request.on_process_started(os.getpid(), None)
            assert call(request.task_call_socket, operation="close", key="wait",
                        subject="test: wait", disposition="ask", question="Which source?")["pending"]
            request.on_process_started(os.getpid(), None)
            return replace(result, output="Replacement completed the work.")
    state, runner, task_id, _ = setup_task(tmp_path, Worker())
    run_task(runner)
    assert state.tasks.get(task_id).status.value == "done"


def test_no_close_completion_still_requires_gate_and_exact_source_publication(tmp_path):
    import sys
    from steward_harness.config.schema import CommandSpec
    from test_task_runner_kernel import publish_task

    class Worker(EditingAdapter):
        corrected = False

        def execute(self, request):
            result = super().execute(request)
            (request.cwd / "result.txt").write_text("green" if self.corrected else "red")
            return replace(result, output="Recorded findings without a completion call.")

    adapter = Worker()
    state, runner, task_id, bare = setup_task(tmp_path, adapter)
    tested = tmp_path / "tested-shas"
    gate = CommandSpec(argv=(sys.executable, "-c", (
        "import pathlib, subprocess; "
        f"f = pathlib.Path({str(tested)!r}).open('a'); "
        "f.write(subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True)); f.close(); "
        "assert pathlib.Path('result.txt').read_text() == 'green'"
    )))
    runner.repositories["app"] = runner.repositories["app"].model_copy(update={"gates": (gate,)})
    original = _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path)
    runner.prepare(task_id)
    assert state.tasks.get(task_id).publishable
    assert state.tasks.get(task_id).landed is None
    publish_task(runner)
    assert state.tasks.get(task_id).dispatchable
    assert state.tasks.get(task_id).landed is None
    assert _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path) == original
    adapter.corrected = True
    runner.prepare(task_id)
    publish_task(runner)
    red, green = tested.read_text().splitlines()
    assert red != green
    assert state.tasks.get(task_id).landed == green
    assert _git(f"--git-dir={bare}", "rev-parse", "main", cwd=tmp_path) == green
    assert "Gate failed" in adapter.requests[1].prompt
    assert len(adapter.requests) == 2
