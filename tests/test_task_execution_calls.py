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


@pytest.mark.parametrize('mode', ['missing', 'questionless', 'conflict', 'malformed', 'late_note', 'cancel', 'crash', 'replacement_writer'])
def test_no_stale_or_incomplete_intent_can_publish(tmp_path, mode):
    class Worker(EditingAdapter):
        def execute(self, request):
            result = super().execute(request)
            request.on_process_started(os.getpid(), None)
            intent = dict(operation='close', key='finish', subject='feat: finish', disposition='idle')
            if mode == 'questionless':
                assert 'error' in call(request.task_call_socket, **(intent | {'disposition': 'ask'}))
            elif mode != 'missing':
                assert call(request.task_call_socket, **intent)['pending']
            if mode == 'conflict':
                assert 'error' in call(request.task_call_socket, **(intent | {'disposition': 'continue'}))
                assert 'error' in call(request.task_call_socket, **intent)
            elif mode == 'malformed':
                assert 'error' in call(request.task_call_socket, **(intent | {'disposition': 'unknown'}))
            elif mode == 'replacement_writer':
                # A fallback/rotated session launches a new native writer.
                request.on_process_started(os.getpid(), None)
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
