"""Shell-to-controller task calls retain their own Git authority and receipts."""
import json
import os
import subprocess

import pytest

from steward_harness.conversations import ConversationService
from steward_harness.state import ConversationId, StateDatabase, TaskId, TaskSpec
from steward_harness.task_calls import TaskCalls
from test_conversations import _reply, _service, _turn


def call(url, **request):
    # Same native shell interface advertised in the actual conversation prompt.
    result = subprocess.run(['curl', '--silent', '--show-error', '--noproxy', '*',
                             '--unix-socket', url, '-H', 'Content-Type: application/json', '--data-binary', '@-', 'http://localhost/task'],
                            input=json.dumps(request), text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def submission(key='first', **extra):
    return dict(operation='submit', key=key, repository='app', title='A task', brief='Do the work', **extra)



class CallingCognition:
    def __init__(self, callback):
        self.callback = callback

    def cancel(self, _execution):
        return True

    def run(self, request, **_kwargs):
        request = request()
        request.on_process_started(os.getpid(), None)
        self.callback(request)
        return _reply('Submitted and inspected.')


def test_two_calls_before_completion_and_parent_failure(tmp_path):
    receipts = []
    def during(request):
        url = request.task_call_socket
        for key in ('first', 'second'):
            receipt = call(url, **submission(key))
            assert receipt['accepted'] is True
            receipts.append(receipt)
            assert service._state.get_turn(request.execution_id).state == 'running'
        assert receipts[0]['task_id'] != receipts[1]['task_id']
        assert len(call(url, operation='list')['tasks']) == 2
        task_id = receipts[0]['task_id']
        assert call(url, operation='show', task_id=task_id)['brief'] == 'Do the work'
        assert call(url, operation='note', key='context', task_id=task_id, text='Extra')['accepted']
        assert call(url, **submission())['replayed']
        assert call(url, **(submission() | {'brief': 'changed'}))['accepted'] is False
        assert call(url, **(submission('no') | {'repository': 'private'}))['accepted'] is False
        assert call(url, operation='cancel', key='stop', task_id=task_id, text='Stop')['accepted']
        assert call(url, operation='retry', key='resume', task_id=task_id, text='Resume')['accepted']
        denied = subprocess.run(['curl', '--silent', '--noproxy', '*', '--unix-socket', url,
                                 '-o', '/dev/null', '-w', '%{http_code}', '--data', '{}',
                                 'http://localhost/task'], start_new_session=True,
                                capture_output=True, text=True, check=True)
        assert denied.stdout == '403'  # Another native invocation cannot borrow the path.
        raise RuntimeError('parent failed')
    service = _service(tmp_path, CallingCognition(during))
    with pytest.raises(RuntimeError, match='parent failed'):
        _turn(service, 'first')
    assert len(service._state.tasks.all()) == 2
    # A fresh process/state object and a later execution recover the same receipt.
    fresh = StateDatabase(tmp_path / 'state.db')
    fresh.tasks.repositories = {'app'}
    def retry(request):
        receipt = call(request.task_call_socket, **submission())
        assert receipt == receipts[0] | {'replayed': True}
    service._state = fresh
    service._cognition = CallingCognition(retry)
    _turn(service, 'second')
    assert len(fresh.tasks.all()) == 2


def test_ownership_and_receipt_after_lost_reply(tmp_path, monkeypatch):
    state = StateDatabase(tmp_path / 'state.db')
    state.tasks.repositories = {'app'}
    owner = state.open_conversation(ConversationId('telegram:owner'), provider='codex', profile='balanced')
    turn, _ = state.start_turn(owner.conversation_id, 'event', 'operator', 'work')
    calls = TaskCalls(state, turn.turn_id)
    original = state.tasks.create
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('crash after update-ref')
    monkeypatch.setattr(state.tasks, 'create', lost)
    with pytest.raises(RuntimeError):
        calls(submission())
    monkeypatch.setattr(state.tasks, 'create', original)
    receipt = calls(submission())
    assert receipt['accepted'] and receipt['replayed']
    other, _ = state.tasks.create(TaskSpec('app', 'Other', 'Other brief'), owner='telegram:other')
    with pytest.raises(ValueError, match='does not own'):
        calls(dict(operation='show', task_id=str(other)))
    with pytest.raises(ValueError, match='does not own'):
        calls(dict(operation='cancel', key='no', task_id=str(other), text='Stop'))
    assert len(calls(dict(operation='list'))['tasks']) == 1


@pytest.mark.parametrize('family,cancel_parent', [('codex', False), ('claude', False), ('glm', False), ('codex', True)])
def test_native_provider_launch_to_mcp_to_controller(tmp_path, family, cancel_parent):
    """Real native adapters/pipes and MCP child; only provider reasoning is scripted."""
    import sys
    from steward_harness.cognition import Cognition
    from steward_harness.runtime.providers.claude import ClaudeRuntime
    from steward_harness.runtime.providers.codex_app_server import CodexAppServerRuntime
    from test_runtime_lifecycle import _controller
    executable = tmp_path / 'provider'
    script = r'''
import json, subprocess, sys
from pathlib import Path
family = FAMILY
session = '11111111-1111-4111-8111-111111111111'
def emit(value):
    print(json.dumps(value), flush=True)
if family == 'codex':
    config = {}
    for index, arg in enumerate(sys.argv):
        if arg == '-c' and sys.argv[index + 1].startswith('mcp_servers.steward_tasks.'):
            key, value = sys.argv[index + 1].split('=', 1)
            config[key.rsplit('.', 1)[1]] = json.loads(value)
else:
    config = json.loads(sys.argv[sys.argv.index('--mcp-config') + 1])['mcpServers']['steward_tasks']
    emit({'type': 'system', 'subtype': 'init', 'session_id': session, 'model': 'fixture'})
def work():
    child = subprocess.Popen([config['command'], *config['args']], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    def rpc(method, params):
        child.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}) + '\n')
        child.stdin.flush()
        return json.loads(child.stdout.readline())['result']
    assert rpc('initialize', {'protocolVersion': '2024-11-05'})['capabilities'] == {'tools': {}}
    assert rpc('tools/list', {})['tools'][0]['name'] == 'task'
    def call(arguments):
        return json.loads(rpc('tools/call', {'name': 'task', 'arguments': arguments})['content'][0]['text'])
    receipts = []
    for key in ['one', 'two']:
        receipts.append(call({'operation': 'submit', 'key': key, 'repository': 'app', 'title': key, 'brief': 'Do it'}))
        assert receipts[-1]['accepted']
    assert receipts[0]['task_id'] != receipts[1]['task_id']
    assert len(call({'operation': 'list'})['tasks']) == 2
    assert call({'operation': 'note', 'key': 'note', 'task_id': receipts[0]['task_id'], 'text': 'Context'})['accepted']
    child.stdin.close()
    assert child.wait(timeout=5) == 0
    return json.dumps(receipts)
for line in sys.stdin:
    event = json.loads(line)
    if family == 'codex':
        method = event.get('method')
        if method == 'initialize':
            result = {}
        elif method in ['thread/start', 'thread/resume']:
            result = {'thread': {'id': session}, 'model': 'fixture'}
        elif method == 'turn/start':
            emit({'id': event['id'], 'result': {'turn': {'id': 'parent'}}})
            output = work()  # Both durable receipts arrive before parent completion.
            if CANCEL_PARENT:
                Path(READY_FILE).write_text(output)
                continue
            emit({'method': 'item/completed', 'params': {'threadId': session, 'turnId': 'parent', 'item': {'type': 'agentMessage', 'text': output}}})
            emit({'method': 'turn/completed', 'params': {'threadId': session, 'turn': {'id': 'parent', 'status': 'completed'}}})
            continue
        elif method == 'turn/interrupt':
            emit({'id': event['id'], 'result': {}})
            emit({'method': 'turn/completed', 'params': {'threadId': session, 'turn': {'id': 'parent', 'status': 'interrupted'}}})
            continue
        elif method == 'thread/backgroundTerminals/clean':
            result = {}
        else:
            continue
        emit({'id': event['id'], 'result': result})
    elif event.get('type') == 'user':
        emit({'type': 'command_lifecycle', 'session_id': session, 'command_uuid': event['uuid'], 'state': 'started'})
        output = work()
        emit({'type': 'result', 'subtype': 'success', 'terminal_reason': 'completed', 'session_id': session, 'result': output})
        emit({'type': 'command_lifecycle', 'session_id': session, 'command_uuid': event['uuid'], 'state': 'completed'})
        break
'''.replace('FAMILY', repr(family)).replace('CANCEL_PARENT', repr(cancel_parent)).replace('READY_FILE', repr(str(tmp_path / 'ready')))
    executable.write_text(f'#!{sys.executable}\n' + script)
    executable.chmod(0o755)
    home = tmp_path / 'home'
    home.mkdir()
    if family == 'codex':
        adapter = CodexAppServerRuntime(executable, controller=_controller(), native_home=home)
    else:
        adapter = ClaudeRuntime(executable, controller=_controller(), native_home=home, family=family)
    state = StateDatabase(tmp_path / 'state.db')
    state.tasks.repositories = {'app'}
    service = ConversationService(state, Cognition({family: adapter}), provider_order=(family,),
                                  profile='balanced', workspace=tmp_path, timeout_seconds=15)
    if cancel_parent:
        import time
        from concurrent.futures import ThreadPoolExecutor
        from steward_harness.runtime.contracts import RuntimeExecutionError
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_turn, service, 'native')
            deadline = time.monotonic() + 10
            while not (tmp_path / 'ready').exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert (tmp_path / 'ready').exists()
            owner = service.conversation_for('telegram', 'chat:topic').conversation_id
            turn = state.turn_for_source(owner, 'native')
            assert state.get_turn(turn.turn_id).state == 'running'
            assert service.cancel(turn.turn_id)
            with pytest.raises(RuntimeExecutionError):
                future.result(timeout=10)
        assert len(state.tasks.all()) == 2
        assert all(task.status.value == 'queued' for task in state.tasks.all())
        return
    result = _turn(service, 'native')
    receipts = json.loads(result.reply_text)
    assert len(state.tasks.all()) == len(receipts) == 2
    assert all(task.owner == str(result.conversation_id) for task in state.tasks.all())


def test_steering_crash_retry_and_cancel_signal_are_idempotent(tmp_path, monkeypatch):
    state = StateDatabase(tmp_path / 'state.db')
    state.tasks.repositories = {'app'}
    owner = state.open_conversation(ConversationId('telegram:owner'), provider='claude', profile='deep')
    turn, _ = state.start_turn(owner.conversation_id, 'event', 'operator', 'work')
    signals = []
    calls = TaskCalls(state, turn.turn_id, cancel=signals.append)
    admitted = calls(submission())
    task_id = TaskId(admitted['task_id'])
    lineage = state.get_conversation(ConversationId.for_task(task_id))
    assert (lineage.provider, lineage.profile) == ('claude', 'deep')
    note = dict(operation='note', key='evidence', task_id=str(task_id), text='Evidence')
    original = state.tasks.input
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('lost steering receipt')
    monkeypatch.setattr(state.tasks, 'input', lost)
    with pytest.raises(RuntimeError):
        calls(note)
    monkeypatch.setattr(state.tasks, 'input', original)
    assert calls(note)['replayed']
    assert len(state.tasks.get(task_id).pending) == 1
    with pytest.raises(ValueError, match='different request'):
        calls(note | {'operation': 'cancel'})
    cancel = dict(operation='cancel', key='stop', task_id=str(task_id), text='Stop')
    assert calls(cancel)['accepted']
    assert signals == [str(ConversationId.for_task(task_id))]
    assert calls(cancel)['replayed']
    calls(dict(operation='retry', key='resume', task_id=str(task_id), text='Resume'))
    count = len(signals)
    assert calls(cancel)['replayed']
    assert len(signals) == count  # Replaying an old withdrawal cannot stop a later retry.


def test_followthrough_stub_uses_native_task_tool(tmp_path, monkeypatch):
    from pathlib import Path
    from steward_harness.cognition import Cognition
    from steward_harness.config.schema import UntrustedExecutionConfig
    from steward_harness.runtime.execution import UntrustedExecutionBroker
    from steward_harness.runtime.process import ProcessController
    from steward_harness.runtime.providers.claude import ClaudeRuntime

    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps(dict(steps=[dict(reply='Submitted', task_calls=[submission('one'), submission('two')])])))
    monkeypatch.setenv('STUB_PLAN', str(plan))
    config = UntrustedExecutionConfig()
    config = config.model_copy(update={'inherited_environment': (*config.inherited_environment, 'STUB_PLAN')})
    broker = UntrustedExecutionBroker(config)
    home = tmp_path / 'home'
    home.mkdir()
    executable = Path(__file__).parents[1] / 'testenv' / 'claude_stub.py'
    adapter = ClaudeRuntime(executable, controller=ProcessController(broker), native_home=home)
    state = StateDatabase(tmp_path / 'state.db')
    state.tasks.repositories = {'app'}
    service = ConversationService(state, Cognition({'claude': adapter}), provider_order=('claude',),
                                  profile='fast', workspace=tmp_path, timeout_seconds=15)
    result = _turn(service, 'stub')
    assert len(state.tasks.all()) == 2
    assert result.reply_text.count('Task admitted:') == 2


@pytest.mark.parametrize('first,later,accepted', [
    ('harness:task-result', 'operator', True),
])
def test_live_source_authority_does_not_follow_first_speaker(tmp_path, first, later, accepted):
    from steward_harness.runtime.contracts import RuntimeInputResult
    from steward_harness.state import ConversationBusy
    from test_conversations import _waiting_rhythm_task, close_task_slice

    receipts = []
    def during(request):
        request.on_session_started('codex', 'session')
        sources = []
        def receive(source):
            sources.append(source.source_id)
            request.on_input_result(RuntimeInputResult(source.source_id, 'accepted'))
        request.on_input_ready(receive)
        with pytest.raises(ConversationBusy):
            _turn(service, 'later', 'Answer the waiting task', operator_id=later)
        action = dict(operation='answer', key='answer', task_id=str(task), text='Package index')
        assert call(request.task_call_socket, **action)['accepted'] is False
        receipt = call(request.task_call_socket, **action, source_id=sources[0])
        assert receipt['accepted'] is accepted
        receipts.append(receipt)
        if accepted:
            assert call(request.task_call_socket, **action, source_id=sources[0])['replayed']
            # An accepted operation's identity includes its source, not just its text.
            changed = call(request.task_call_socket, **action, source_id=request.execution_id)
            assert changed['accepted'] is False
    service = _service(tmp_path, CallingCognition(during))
    task = _waiting_rhythm_task(service)
    _turn(service, 'root', operator_id=first)
    assert service._state.tasks.get(task).status.value == ('queued' if accepted else 'waiting')


@pytest.mark.parametrize('disposition', ['unresolved', 'rejected'])
def test_unaccepted_live_sources_cannot_grant_authority(tmp_path, disposition):
    from steward_harness.runtime.contracts import RuntimeInputResult
    from steward_harness.state import ConversationBusy
    def during(request):
        request.on_session_started('codex', 'session')
        sources = []
        def receive(source):
            sources.append(source.source_id)
            request.on_input_result(RuntimeInputResult(source.source_id, disposition))
        request.on_input_ready(receive)
        with pytest.raises(ConversationBusy):
            _turn(service, 'later', operator_id='operator')
        receipt = call(request.task_call_socket, **submission(source_id=sources[0]))
        assert receipt['accepted'] is False
        assert 'controller-accepted' in receipt['error']
        assert call(request.task_call_socket, **submission(source_id='turn_fake'))['accepted'] is False
    service = _service(tmp_path, CallingCognition(during))
    _turn(service, 'root', operator_id='harness:task-result')
    assert not service._state.tasks.all()


@pytest.mark.parametrize("operation", ["submit", "notify"])
def test_source_receipt_replays_after_restart_but_cannot_authorize_new_work(tmp_path, operation):
    state = StateDatabase(tmp_path / 'state.db')
    state.tasks.repositories = {'app'}
    owner = state.open_conversation(ConversationId('telegram:owner'), provider='codex', profile='balanced')
    turn, _ = state.start_turn(owner.conversation_id, 'first', 'operator', 'work')
    calls = TaskCalls(state, turn.turn_id)
    request = (submission(source_id=str(turn.turn_id)) if operation == "submit" else
               dict(operation="notify", key="notice", text="A message", source_id=str(turn.turn_id)))
    receipt = calls(request)
    state.interrupt_turn(turn.turn_id, 'lost parent')
    with pytest.raises(ValueError, match='active execution'):
        calls(submission('late', source_id=str(turn.turn_id)))
    second, _ = state.start_turn(owner.conversation_id, 'second', 'operator', 'retry')
    calls = TaskCalls(state, second.turn_id)
    assert calls(request) == receipt | {'replayed': True}
    with pytest.raises(ValueError, match='not an input'):
        calls(request | {'key': 'new'})
    state.clear_conversation(owner.conversation_id)
    with pytest.raises(ValueError, match='active execution'):
        calls(submission('after-clear', source_id=str(second.turn_id)))
    assert len(state.tasks.all()) == int(operation == "submit")


@pytest.mark.parametrize('count', [0, 1, 3])
def test_notifications_are_independent_calls_and_final_reply_is_not_a_send(tmp_path, count):
    receipts = []
    def during(request):
        for n in range(count):
            receipt = call(request.task_call_socket, operation='notify', key=f'message-{n}', text=f'Notice {n}')
            assert receipt['accepted'] and not receipt['replayed']
            receipts.append(receipt)
            assert call(request.task_call_socket, operation='notify', key=f'message-{n}', text=f'Notice {n}')['replayed']
    service = _service(tmp_path, CallingCognition(during))
    _turn(service, 'notify')
    pending = service._state.pending_result_receipts()
    assert len(pending) == count
    assert {r['reply'] for r in pending} == {f'Notice {n}' for n in range(count)}
    assert {r['source_key'] for r in pending} == {r['receipt'] for r in receipts}


def test_notification_survives_crash_before_receipt_and_exact_replay(tmp_path, monkeypatch):
    state = StateDatabase(tmp_path / 'state.db')
    owner = state.open_conversation(ConversationId('telegram:owner'), provider='codex', profile='balanced')
    turn, _ = state.start_turn(owner.conversation_id, 'event', 'operator', 'work')
    calls = TaskCalls(state, turn.turn_id)
    original = state.save_result_receipt
    def crash(receipt):
        original(receipt)
        raise OSError('lost response after durable intent')
    monkeypatch.setattr(state, 'save_result_receipt', crash)
    request = dict(operation='notify', key='notice', text='A decision is needed.')
    with pytest.raises(OSError):
        calls(request)
    fresh = StateDatabase(state.path)
    recovered = TaskCalls(fresh, turn.turn_id)(request)
    assert recovered['accepted'] and recovered['replayed']
    assert len(fresh.pending_result_receipts()) == 1
    with pytest.raises(ValueError, match='different text'):
        TaskCalls(fresh, turn.turn_id)(request | {'text': 'Changed intent'})


def test_rhythm_notification_is_bound_to_configured_owner_and_cannot_mutate_tasks(tmp_path):
    state = StateDatabase(tmp_path / 'state.db')
    owner = state.open_conversation(ConversationId('rhythm:sleep'), provider='codex', profile='balanced')
    turn, _ = state.start_turn(owner.conversation_id, 'rhythm:sleep:1', 'harness:rhythm', 'work')
    calls = TaskCalls(state, turn.turn_id, notify_owner='telegram:world')
    receipt = calls(dict(operation='notify', key='notice', text='Sleep needs help.'))
    assert receipt['owner'] == 'telegram:world' and receipt['source_id'] == str(turn.turn_id)
    with pytest.raises(ValueError, match='only notify'):
        calls(submission())
    with pytest.raises(ValueError, match='incorrect fields'):
        calls(dict(operation='notify', key='notice', text='Sleep needs help.', owner='telegram:elsewhere'))


@pytest.mark.parametrize('operation', ['list', 'show'])
def test_task_reads_do_not_block_provider_binding(tmp_path, monkeypatch, operation):
    """A slow accepted-task Git read must not own SQLite's writer slot."""
    from concurrent.futures import ThreadPoolExecutor, wait
    from threading import Event
    from time import monotonic
    from steward_harness.kernel import Dispatch

    state = StateDatabase(tmp_path / 'state.db')
    state.tasks.repositories = {'app'}
    owner = state.open_conversation(ConversationId('telegram:owner'),
                                    provider='codex', profile='balanced')
    other = state.open_conversation(ConversationId('telegram:other'),
                                    provider='codex', profile='balanced')
    turn, _ = state.start_turn(owner.conversation_id, 'event', 'operator', 'work')
    calls = TaskCalls(state, turn.turn_id)
    receipt = calls(submission())
    entered, release = Event(), Event()
    original = state.tasks.refs if operation == 'list' else state.tasks.read

    def slow_read(*args, **kwargs):
        entered.set()
        assert release.wait(15), 'test did not release Git reader'
        return original(*args, **kwargs)

    monkeypatch.setattr(state.tasks, 'refs' if operation == 'list' else 'read', slow_read)
    request = {'operation': operation}
    if operation == 'show':
        request['task_id'] = receipt['task_id']
    dispatch = Dispatch(1)
    with ThreadPoolExecutor(1) as pool:
        reading = pool.submit(calls, request)
        try:
            assert entered.wait(5)
            started = monotonic()
            dispatch.submit('bind', lambda: state.bind_conversation_provider(
                other.conversation_id, 'codex', 'independent-session',
                expected_generation=other.generation))
            # Wait for the worker, then exercise the daemon's fault boundary.
            job = dispatch._inflight['bind']
            assert wait([job], timeout=7).done
            elapsed = monotonic() - started
            print(f'{operation}: provider bind worker finished after {elapsed:.3f}s with Git blocked')
            dispatch.reap()
            assert not reading.done()
            assert state.lineage(other.conversation_id).provider_session_id == 'independent-session'
        finally:
            release.set()
            dispatch.stop()
        result = reading.result(timeout=5)
        assert result['operation'] == operation


@pytest.mark.parametrize('operation', ['list', 'show'])
@pytest.mark.parametrize('invalidate', ['interrupt', 'rotate'])
def test_task_reads_reject_expired_execution(tmp_path, operation, invalidate):
    state = StateDatabase(tmp_path / 'state.db')
    state.tasks.repositories = {'app'}
    owner = state.open_conversation(ConversationId('telegram:owner'),
                                    provider='codex', profile='balanced')
    turn, _ = state.start_turn(owner.conversation_id, 'event', 'operator', 'work')
    calls = TaskCalls(state, turn.turn_id)
    receipt = calls(submission())
    request = {'operation': operation}
    if operation == 'show':
        request['task_id'] = receipt['task_id']
    with pytest.raises(ValueError, match='controller-accepted'):
        calls(request | {'source_id': 'unknown'})
    if invalidate == 'interrupt':
        state.interrupt_turn(turn.turn_id, 'execution ended')
    else:
        state.bind_conversation_provider(owner.conversation_id, 'claude', 'replacement')
    with pytest.raises(ValueError, match='active execution'):
        calls(request)


def test_telegram_capability_uses_bound_conversation(tmp_path):
    seen = []
    def during(request):
        receipt = call(request.task_call_socket, operation='telegram', action='info', key='inspect', text='')
        assert receipt['accepted']
    service = _service(tmp_path, CallingCognition(during))
    service._telegram_admin = lambda scope, request: seen.append((scope, request)) or {'accepted': True}
    _turn(service, 'cosmetics')
    assert seen[0][0] == 'telegram:chat:topic'


def test_rhythm_can_inspect_only_its_notification_owners_tasks(tmp_path):
    state = StateDatabase(tmp_path / 'state.db')
    state.tasks.repositories = {'app'}
    mine, _ = state.tasks.create(TaskSpec('app', 'Owned work', 'Read me'), owner='telegram:0')
    other, _ = state.tasks.create(TaskSpec('app', 'Other work', 'Private'), owner='telegram:99')
    owner = state.open_conversation(ConversationId('rhythm:review'), provider='codex', profile='balanced')
    turn, _ = state.start_turn(owner.conversation_id, 'review:1', 'harness:rhythm', 'review')
    calls = TaskCalls(state, turn.turn_id, notify_owner='telegram:0')
    assert [r['task_id'] for r in calls({'operation': 'list'})['tasks']] == [str(mine)]
    assert calls({'operation': 'show', 'task_id': str(mine)})['brief'] == 'Read me'
    with pytest.raises(ValueError, match='does not own'):
        calls({'operation': 'show', 'task_id': str(other)})
    with pytest.raises(ValueError, match='only notify'):
        calls(dict(operation='answer', key='no', task_id=str(mine), text='Continue'))


def test_ownerless_rhythm_cannot_read_unowned_tasks(tmp_path):
    state = StateDatabase(tmp_path / 'state.db')
    owner = state.open_conversation(ConversationId('rhythm:quiet'), provider='codex', profile='balanced')
    turn, _ = state.start_turn(owner.conversation_id, 'quiet:1', 'harness:rhythm', 'quiet')
    with pytest.raises(ValueError, match='no task inspection owner'):
        TaskCalls(state, turn.turn_id)({'operation': 'list'})


def test_driving_rhythm_admits_to_topic_and_replays_across_runs(tmp_path):
    state = StateDatabase(tmp_path / 'state.db')
    state.tasks.repositories = {'app'}
    owner = state.open_conversation(ConversationId('rhythm:drive'), provider='codex', profile='balanced')
    turn, _ = state.start_turn(owner.conversation_id, 'drive:1', 'harness:rhythm', 'work')
    calls = TaskCalls(state, turn.turn_id, notify_owner='telegram:0', drive_tasks=True)
    receipt = calls(submission())
    task_id = TaskId(receipt['task_id'])
    assert state.tasks.get(task_id).owner == 'telegram:0'
    state.tasks.create(TaskSpec('app', 'Waiting', 'Needs evidence'), task_id=TaskId('task-waiting'),
                       owner='telegram:0')
    from test_conversations import close_task_slice
    close_task_slice(state, TaskId('task-waiting'), 'ask', detail='Evidence?')
    answer = dict(operation='answer', key='evidence', task_id='task-waiting', text='Verified evidence')
    assert calls(answer)['accepted']
    assert calls(answer)['replayed']
    state.tasks.create(TaskSpec('app', 'Blocked', 'Needs repair'), task_id=TaskId('task-blocked'),
                       owner='telegram:0', hold='blocked', reason='Unavailable')
    assert calls(dict(operation='retry', key='repair', task_id='task-blocked', text='Now available'))['accepted']
    assert calls(dict(operation='note', key='detail', task_id=str(task_id), text='Relevant context'))['accepted']
    assert state.tasks.get(TaskId('task-waiting')).status.value == 'queued'
    # New invocation has a new source, but stable intent produces no duplicate task.
    state.interrupt_turn(turn.turn_id, 'Parent interrupted after accepted calls')
    later, _ = state.start_turn(owner.conversation_id, 'drive:2', 'harness:rhythm', 'work')
    again = TaskCalls(state, later.turn_id, notify_owner='telegram:0', drive_tasks=True)
    assert again(submission()) == receipt | {'replayed': True}
    assert len(again({'operation': 'list'})['tasks']) == 3
    with pytest.raises(ValueError, match='different request'):
        again(submission() | {'brief': 'Changed'})


def test_driving_rhythm_cannot_cross_authority_or_undo_cancellation(tmp_path):
    state = StateDatabase(tmp_path / 'state.db')
    state.tasks.repositories = {'app'}
    owner = state.open_conversation(ConversationId('rhythm:drive'), provider='codex', profile='balanced')
    turn, _ = state.start_turn(owner.conversation_id, 'drive:1', 'harness:rhythm', 'work')
    calls = TaskCalls(state, turn.turn_id, notify_owner='telegram:0', drive_tasks=True)
    for name, task_owner in [('other', 'telegram:24'), ('unowned', None)]:
        task_id, _ = state.tasks.create(TaskSpec('app', name, 'Work'), owner=task_owner)
        with pytest.raises(ValueError, match='does not own'):
            calls(dict(operation='answer', key=name, task_id=str(task_id), text='Proceed'))
    with pytest.raises(ValueError, match='not authorized'):
        calls(submission() | {'repository': 'private'})
    task_id, _ = state.tasks.create(TaskSpec('app', 'Cancelled', 'Work'), owner='telegram:0')
    state.tasks.cancel(task_id, 'Operator stopped', source='operator:stop')
    with pytest.raises(ValueError, match='cancelled'):
        calls(dict(operation='retry', key='resume', task_id=str(task_id), text='Proceed'))
    with pytest.raises(ValueError, match='only notify'):
        calls(dict(operation='cancel', key='stop', task_id=str(task_id), text='Stop'))
    with pytest.raises(ValueError, match='only notify'):
        TaskCalls(state, turn.turn_id, drive_tasks=True)(submission())
