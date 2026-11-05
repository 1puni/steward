"""Native DSH lifecycle, ownership and public cognition routing invariants."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from steward_harness.cognition import Cognition, CognitionRequest
from steward_harness.config.schema import ProviderConfig, UntrustedExecutionConfig
from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeRequest, RuntimeUnavailable, resolve_model
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.process import ProcessTimeout, TokenBudgetExhausted
from steward_harness.runtime.providers import build_runtimes
from steward_harness.runtime.providers.deepseek import DeepSeekEvents

SESSION = 'session-11111111-1111-4111-8111-111111111111'


def request(tmp_path, **kw):
    return RuntimeRequest(execution_id='test', resolved=resolve_model('deepseek', 'fast'),
        provider_session_id=None, prompt='work', cwd=tmp_path, timeout_seconds=10, **kw)


def opening(tmp_path):
    return [{'type': 'session', 'sessionId': SESSION, 'cwd': str(tmp_path)},
            {'type': 'status', 'phase': 'turn_start', 'turn': 0}]


def ending():
    return [{'type': 'status', 'phase': 'turn_end', 'turn': 0, 'reason': {'kind': 'completed'}},
            {'type': 'final', 'text': 'complete'}]


@pytest.mark.parametrize('tail', [
    [{'type': 'final', 'text': 'looks successful'}],
    [{'type': 'status', 'phase': 'turn_end', 'turn': 1, 'reason': {'kind': 'completed'}}],
    [{'type': 'status', 'phase': 'turn_end', 'turn': 0, 'reason': {'kind': 'completed'}, 'truncated': True}],
    ending() + [{'type': 'final', 'text': 'duplicate'}],
    [{'type': 'status', 'phase': 'step_end', 'turn': 0, 'step': []}],
    [{'type': 'status', 'phase': 'turn_end', 'turn': False, 'reason': {'kind': 'completed'}}],
    [{'type': 'session', 'sessionId': SESSION, 'cwd': 'another'}],
])
def test_refuses_false_completion_or_identity(tmp_path, tail):
    events = DeepSeekEvents(request(tmp_path))
    with pytest.raises(RuntimeExecutionError) as failure:
        for event in opening(tmp_path) + tail:
            events.consume(json.dumps(event))
    assert failure.value.session_id == SESSION
    assert 'secret' not in str(failure.value)


def test_final_is_lossless_and_quiet_requires_native_completion(tmp_path):
    events = DeepSeekEvents(request(tmp_path, allow_empty_output=True))
    for event in opening(tmp_path) + ending()[:-1] + [{'type': 'final', 'text': ''}]:
        events.consume(json.dumps(event))
    assert events.output == ''
    events = DeepSeekEvents(request(tmp_path))
    text = 'x' * 20000
    for event in opening(tmp_path) + ending()[:-1] + [{'type': 'final', 'text': text}]:
        events.consume(json.dumps(event))
    assert events.output == text


@pytest.fixture
def native(tmp_path):
    seed = tmp_path / 'seed'
    seed.mkdir(mode=0o700)
    (seed / '.credentials.yaml').write_text('DEEPSEEK_API_KEY: private-fixture')
    (seed / '.credentials.yaml').chmod(0o600)
    # Seed runtime state must never become another owner's mutable state.
    (seed / 'storages').mkdir()
    (seed / 'storages/private').write_text('other owner')
    world = tmp_path / 'world'
    world.mkdir()
    subprocess.run(['git', 'init', '-q', str(world)], check=True)
    executable = tmp_path / 'dsh'
    executable.write_text(f'#!{sys.executable}\n' + r'''
import json, os, pathlib, signal, sys, time
if '--version' in sys.argv:
    print('0.2.0-rc.2'); sys.exit()
patch = json.loads(pathlib.Path(sys.argv[sys.argv.index('--patch')+1]).read_text())
by_id = {row['id']: row for row in patch if 'id' in row}
assert by_id['approval']['config']['policy'] == 'never'
home = pathlib.Path(os.environ['DSH_HOME'])
assert not (home/'storages/private').exists()
assert not (home/'.credentials.yaml').exists()
assert pathlib.Path(by_id['credentials']['config']['path']).read_text().endswith('private-fixture')
session = 'session-11111111-1111-4111-8111-111111111111'
record = home / 'sessions' / 'workspace' / session / 'session.v4.jsonl.zstd'
if '--session-id' in sys.argv:
    assert sys.argv[sys.argv.index('--session-id')+1] == session
    assert record.read_text() in {'original', 'interrupted'}
record.parent.mkdir(parents=True, exist_ok=True)
record.write_text('original')
def emit(**kw): print(json.dumps(kw), flush=True)
emit(type='session', sessionId=session, cwd=os.getcwd())
emit(type='status', phase='turn_start', turn=0)
prompt = sys.stdin.read()
if prompt == 'wait':
    def interrupt(*args):
        record.write_text('interrupted')
        sys.exit(130)
    signal.signal(signal.SIGINT, interrupt)
    emit(type='tool_call', tool='waiting')
    time.sleep(60)
if prompt == 'over budget':
    emit(type='status', phase='step_end', turn=0, step=0, usage={'outputTokens': 20})
    time.sleep(60)
if prompt == 'native error':
    emit(type='status', phase='turn_end', turn=0, reason={'kind':'error'})
    time.sleep(.2)
    record.write_text('failure flushed')
    emit(type='final', text='')
    sys.exit(1)
if prompt == 'fail':
    emit(type='final', text='false success')
    sys.exit(1)
emit(type='status', phase='turn_end', turn=0, reason={'kind':'completed'})
if prompt != 'truncated': emit(type='final', text='resumed' if '--session-id' in sys.argv else 'fresh')
''')
    executable.chmod(0o755)
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    config = ProviderConfig(default_family='deepseek', fallback_families=(),
        native_homes={'deepseek': str(seed)}, deepseek_executable=str(executable))
    cognition = Cognition(build_runtimes(config, broker))
    req = CognitionRequest(execution_id='deepseek', profile='fast', provider_order=('deepseek',),
        prompt='work', cwd=world, timeout_seconds=10, native_owner='one', sandbox_mode='workspace-write')
    return cognition, req, seed


def test_public_cognition_discovery_resume_and_record_capture(native):
    cognition, req, seed = native
    assert cognition.families == ('deepseek',)
    first = cognition.run(req)
    assert first.output == 'fresh'
    resumed = cognition.run(replace(req, provider_session_id=first.provider_session_id, session_provider='deepseek'))
    assert resumed.output == 'resumed' and resumed.provider_session_id == first.provider_session_id
    homes = list(seed.glob('.steward-owner-*'))
    assert len(homes) == 1
    assert homes[0].stat().st_mode & 0o777 == 0o700
    files = subprocess.check_output(['git', 'ls-files'], cwd=req.cwd, text=True)
    assert 'artefacts/deepseek/sessions/workspace/' + SESSION + '/session.v4.jsonl.zstd' in files
    assert 'credentials' not in files
    assert not list(req.cwd.rglob('.credentials.yaml'))
    cognition.run(replace(req, native_owner='two'))
    assert len(list(seed.glob('.steward-owner-*'))) == 2


@pytest.mark.parametrize('prompt', ['fail', 'truncated'])
def test_public_cognition_refuses_missing_terminal_result(native, prompt):
    cognition, req, _ = native
    with pytest.raises(RuntimeExecutionError) as failure:
        cognition.run(replace(req, prompt=prompt))
    assert failure.value.session_id == SESSION


def test_deadline_preserves_native_interrupted_record_and_resume(native):
    cognition, req, seed = native
    with pytest.raises(ProcessTimeout):
        cognition.run(replace(req, prompt='wait', timeout_seconds=2))
    record, = seed.glob('.steward-owner-*/sessions/*/*/session.v4.jsonl.zstd')
    assert record.read_text() == 'interrupted'
    assert cognition.run(replace(req, provider_session_id=SESSION, session_provider='deepseek')).output == 'resumed'


def test_cancellation_remains_error_after_native_shutdown(native):
    cognition, req, seed = native
    ready = Event()
    with ThreadPoolExecutor() as pool:
        call = pool.submit(cognition.run, replace(req, prompt='wait', on_progress=lambda *_: ready.set()))
        assert ready.wait(10)
        assert cognition.cancel(req.execution_id)
        with pytest.raises(RuntimeExecutionError, match='cancelled'):
            call.result(timeout=15)
    record, = seed.glob('.steward-owner-*/sessions/*/*/session.v4.jsonl.zstd')
    assert record.read_text() == 'interrupted'


def test_output_budget_uses_native_report_and_cannot_be_replayed(native):
    cognition, req, _ = native
    with pytest.raises(TokenBudgetExhausted):
        cognition.run(replace(req, prompt='over budget', token_budget=10))


def test_native_failure_drains_until_exit_and_preserves_quiet_failure(tmp_path):
    events = DeepSeekEvents(request(tmp_path, allow_empty_output=True))
    for event in opening(tmp_path) + [
        {'type': 'status', 'phase': 'turn_end', 'turn': 0, 'reason': {'kind': 'error', 'error': {'code': 'API_ERROR'}}},
        {'type': 'final', 'text': ''},
    ]:
        events.consume(json.dumps(event))
    assert events.failure and not events.completed
    events = DeepSeekEvents(request(tmp_path))
    events.consume(json.dumps({'type': 'error', 'message': 'private sensitive diagnostic'}))
    assert events.failure and 'private sensitive diagnostic' not in events.failure


def test_resumption_restores_original_from_candidate_git(native):
    import shutil
    cognition, req, seed = native
    first = cognition.run(req)
    home, = seed.glob('.steward-owner-*')
    shutil.rmtree(home)
    assert cognition.run(replace(req, provider_session_id=first.provider_session_id,
                                session_provider='deepseek')).output == 'resumed'


def test_missing_native_record_never_restarts_or_rebinds(native):
    cognition, req, _ = native
    with pytest.raises(RuntimeExecutionError, match='absent or ambiguous'):
        cognition.run(replace(req, provider_session_id=SESSION, session_provider='deepseek'))


def test_nonprivate_credentials_decline_before_provider_starts(native):
    cognition, req, seed = native
    (seed / '.credentials.yaml').chmod(0o644)
    with pytest.raises(RuntimeUnavailable, match='unavailable'):
        cognition.run(req)
    assert not list(seed.glob('.steward-owner-*'))


def test_native_error_flushes_original_before_failure_is_returned(native):
    cognition, req, seed = native
    with pytest.raises(RuntimeExecutionError, match='did not complete successfully'):
        cognition.run(replace(req, prompt='native error', allow_empty_output=True))
    record, = seed.glob('.steward-owner-*/sessions/*/*/session.v4.jsonl.zstd')
    assert record.read_text() == 'failure flushed'


def test_anonymous_bounded_calls_have_distinct_private_configuration(native):
    cognition, req, seed = native
    req = replace(req, native_owner=None, sandbox_mode='read-only')
    assert cognition.run(req).output == 'fresh'
    assert cognition.run(req).output == 'fresh'
    assert len(list(seed.glob('.steward-launch-*'))) == 2
    assert not (seed / '.steward-run.patch.json').exists()
