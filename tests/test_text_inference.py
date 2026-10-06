"""Tool-free application calls retain native lifecycle and authority boundaries."""
import importlib.util
import json
import socket
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from steward_harness.cognition import Cognition, CognitionRequest
from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.runtime.contracts import (Availability, ProviderCapabilities, RuntimeExecutionError,
    RuntimeRequest, RuntimeResult, RuntimeUnavailable, resolve_model)
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.native_workspace import native_workspace
from steward_harness.runtime.process import ProcessController
from steward_harness.runtime.providers.claude import ClaudeRuntime
from steward_harness.runtime.text_only import reject_claude_tools, verify_version
from test_codex_app_server import start, event


def runtime_request(tmp_path, **kwargs):
    return RuntimeRequest(execution_id='test', resolved=resolve_model('codex', 'balanced'),
        provider_session_id=None, prompt='test', cwd=tmp_path, timeout_seconds=10, text_only=True, **kwargs)


def test_ephemeral_state_never_imports_skills_history_or_hooks_and_cleans_on_failure(tmp_path):
    seed = tmp_path / 'seed'; seed.mkdir()
    auth = tmp_path / 'auth'; auth.mkdir(); (auth / 'auth.json').write_text('secret')
    for name in ('config.toml', 'hooks.json', 'AGENTS.md', 'session.jsonl'):
        (seed / name).write_text('must not import')
    (seed / 'skills').mkdir()
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    paths = []
    for fail in (False, True):
        try:
            with native_workspace(broker, runtime_request(tmp_path), seed,
                    mappings={}, resume_pattern='', credential_home=auth) as workspace:
                paths.append(workspace.home)
                assert workspace.resume is None
                assert not (workspace.home / 'skills').exists()
                assert not (workspace.home / 'hooks.json').exists()
                assert not (workspace.home / 'AGENTS.md').exists()
                assert (workspace.home / 'auth.json').is_symlink()
                assert (workspace.home / 'auth.json').resolve() == auth / 'auth.json'
                assert 'must not import' not in (workspace.home / 'config.toml').read_text()
                (workspace.home / 'participant.jsonl').write_text('private')
                if fail:
                    raise RuntimeError('provider failed')
        except RuntimeError:
            assert fail
        assert not paths[-1].exists()
    assert paths[0] != paths[1]
    assert (auth / 'auth.json').read_text() == 'secret'
    assert not list(seed.glob('.inference-*'))


@pytest.mark.parametrize('kwargs', [{'native_owner': 'owner'}, {'record_checkout': Path('/tmp')},
    {'task_call_socket': '/tmp/task.sock'}, {'images': (Path('/tmp/image'),)},
    {'on_input_ready': lambda _: None}, {'sandbox_mode': 'workspace-write'}])
def test_tool_free_requests_reject_retained_or_tool_state(tmp_path, kwargs):
    with pytest.raises(ValueError, match='text-only'):
        runtime_request(tmp_path, **kwargs)


def test_codex_uses_ephemeral_protocol_and_rejects_every_tool_kind(tmp_path):
    for kind in ('commandExecution', 'fileChange', 'mcpToolCall', 'webSearch', 'subAgentActivity', 'unknownNewTool'):
        turn, wire, _ = start(tmp_path, text_only=True)
        request = next(m for m in wire.messages if m.get('method') == 'thread/start')
        assert request['params']['ephemeral'] is True
        assert request['params']['approvalPolicy'] == 'never'
        assert request['params']['approvalsReviewer'] == 'user'
        with pytest.raises(RuntimeExecutionError, match='Tool activity'):
            event(turn, 'item/started', item={'type': kind})
    turn, _, _ = start(tmp_path, text_only=True)
    with pytest.raises(RuntimeExecutionError, match='Server tool'):
        turn.consume(json.dumps({'id': 999, 'method': 'item/tool/call', 'params': {}}))


@pytest.mark.parametrize('event', [
    {'type': 'system', 'subtype': 'init', 'tools': ['Bash']},
    {'type': 'system', 'subtype': 'init'},
    {'type': 'assistant', 'message': {'content': [{'type': 'tool_use'}]}},
    {'type': 'stream_event', 'event': {'content_block': {'type': 'server_tool_use'}}},
    {'type': 'control_request'}, {'type': 'system', 'subtype': 'hook_started'},
])
def test_claude_tool_boundary(event):
    with pytest.raises(RuntimeExecutionError):
        reject_claude_tools(event)


def test_claude_flags_preserve_oauth_and_disable_tools_hooks_skills(tmp_path):
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    runtime = ClaudeRuntime(controller=ProcessController(broker), native_home=tmp_path,
                            credential_home=tmp_path / 'common-auth')
    command = runtime._command(runtime_request(tmp_path), None)
    assert command[command.index('--tools') + 1] == ''
    assert command[command.index('--setting-sources') + 1] == ''
    for option in ('--disable-slash-commands', '--no-session-persistence', '--strict-mcp-config'):
        assert option in command
    assert '--bare' not in command
    assert json.loads(command[command.index('--settings') + 1])['disableAllHooks'] is True
    assert runtime.environment({})['CLAUDE_SECURESTORAGE_CONFIG_DIR'] == str(tmp_path / 'common-auth')


def test_unsupported_version_fails_closed():
    broker = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout='new version'))
    with pytest.raises(RuntimeExecutionError, match='Unsupported'):
        verify_version(broker, Path('/native'), 'codex')


def payload(**updates):
    value = {'messages': [{'role': 'user', 'content': 'hello'}], 'max_completion_tokens': 1800,
        'response_format': {'type': 'json_schema', 'json_schema': {'name': 'support', 'strict': True,
        'schema': {'type': 'object', 'properties': {'reply': {'type': 'string'}}, 'required': ['reply'], 'additionalProperties': False}}}}
    value.update(updates)
    return json.dumps(value).encode()


def test_deadline_does_not_restart_for_fallback(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('steward_harness.cognition.time.monotonic', lambda: clock[0])
    calls = []
    class Adapter:
        capabilities = ProviderCapabilities(text_only=True)
        def __init__(self, family): self.family = family
        def available(self): return Availability(True)
        def execute(self, request):
            calls.append(request.timeout_seconds)
            clock[0] += 6
            raise RuntimeUnavailable('quota')
    cognition = Cognition({family: Adapter(family) for family in ('codex', 'claude', 'glm')})
    with pytest.raises(RuntimeExecutionError, match='deadline'):
        cognition.run(CognitionRequest(execution_id='deadline', profile='balanced', prompt='text',
            cwd=tmp_path, timeout_seconds=10, provider_order=('codex', 'claude', 'glm'), text_only=True))
    assert calls == [10, 4]


def test_unadvertised_adapter_never_receives_text_only(tmp_path):
    adapter = SimpleNamespace(family='custom', capabilities=ProviderCapabilities(),
                              available=lambda: pytest.fail('must not launch'))
    with pytest.raises(RuntimeUnavailable, match='missing text-only'):
        Cognition({'custom': adapter}).run(CognitionRequest(execution_id='unsupported', profile='balanced',
            prompt='text', cwd=tmp_path, timeout_seconds=10, provider_order=('custom',), text_only=True))


def test_text_only_rejects_tool_events_from_unexpected_threads(tmp_path):
    turn, _, _ = start(tmp_path, text_only=True)
    with pytest.raises(RuntimeExecutionError, match='Tool activity'):
        event(turn, 'item/started', thread_id='unexpected-child', item={'type':'commandExecution'})
