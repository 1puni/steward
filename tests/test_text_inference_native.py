"""Opt-in, credential-free installed-binary wire acceptance (localhost only)."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from steward_harness.runtime import text_only
from steward_harness.runtime.contracts import RuntimeRequest, RuntimeExecutionError, resolve_model
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.runtime.process import ProcessController
from steward_harness.runtime.providers.codex_app_server import CodexAppServerRuntime
from steward_harness.runtime.providers.claude import ClaudeRuntime

pytestmark = pytest.mark.skipif(os.environ.get('STEWARD_NATIVE_DUMMY') != '1',
                               reason='explicit installed-binary dummy acceptance only')


@pytest.mark.parametrize('family', ['codex', 'claude'])
@pytest.mark.parametrize('attack', [False, True])
def test_installed_native_sends_no_tools_and_finishes_fresh(tmp_path, monkeypatch, family, attack):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.endswith('count_tokens'):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"input_tokens":10}'); return
            requests.append(payload)
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            def emit(kind, data):
                self.wfile.write(('event: '+kind+'\ndata: '+json.dumps({'type': kind, **data})+'\n\n').encode())
            if family == 'codex':
                item = {'id':'msg_test','type':'message','role':'assistant','status':'completed',
                        'content':[{'type':'output_text','text':'{"reply":"dummy"}','annotations':[]}]}
                if attack:
                    item = {'id':'tool_test','type':'function_call','call_id':'call_test', 'name':'exec_command',
                            'arguments':json.dumps({'cmd': 'touch ' + str(tmp_path / 'HOST_EXECUTED')})}
                emit('response.created', {'response': {'id':'resp_test','status':'in_progress','output':[]}})
                emit('response.output_item.added', {'output_index':0,'item':dict(item, content=[],status='in_progress')})
                emit('response.output_text.delta', {'item_id':'msg_test','output_index':0,'content_index':0,'delta':'{"reply":"dummy"}'})
                emit('response.output_item.done', {'output_index':0,'item':item})
                emit('response.completed', {'response':{'id':'resp_test','status':'completed','output':[item],
                    'usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}}})
            else:
                emit('message_start', {'message': {'id':'msg_test','type':'message','role':'assistant','model':'claude-sonnet-5',
                    'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':10,'output_tokens':0}}})
                if attack:
                    emit('content_block_start', {'index':0,'content_block':{'type':'tool_use','id':'tool_test','name':'Bash','input':{}}})
                    emit('content_block_delta', {'index':0,'delta':{'type':'input_json_delta','partial_json':json.dumps({'command':'touch '+str(tmp_path / 'HOST_EXECUTED')})}})
                else:
                    emit('content_block_start', {'index':0,'content_block':{'type':'text','text':''}})
                emit('content_block_delta', {'index':0,'delta':{'type':'text_delta','text':'{"reply":"dummy"}'}})
                emit('content_block_stop', {'index':0})
                emit('message_delta', {'delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':5}})
                emit('message_stop', {})
    server = HTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever); thread.start()
    seed = tmp_path / 'native'; seed.mkdir()
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig(inherited_environment=()))
    controller = ProcessController(broker)
    endpoint = f'http://127.0.0.1:{server.server_port}'
    if family == 'codex':
        monkeypatch.setattr(text_only, 'CODEX_CONFIG', 'model_provider="dummy"\n'+text_only.CODEX_CONFIG+
            f'\n[model_providers.dummy]\nname="dummy"\nbase_url="{endpoint}/v1"\nwire_api="responses"\n')
        runtime = CodexAppServerRuntime(Path('/usr/local/bin/codex'), controller=controller, native_home=seed)
    else:
        token = tmp_path / 'dummy-token'; token.write_text('dummy-not-a-credential')
        runtime = ClaudeRuntime(Path('/usr/local/bin/claude'), controller=controller, native_home=seed,
                                base_url=endpoint, credential_path=token)
    sessions = []
    try:
        def execute(i):
            return runtime.execute(RuntimeRequest(execution_id=f'dummy-{i}', resolved=resolve_model(family,'balanced'),
                provider_session_id=None, prompt='Return {"reply":"dummy"}', cwd=tmp_path,
                text_only=True, timeout_seconds=10 if attack else 30, token_budget=8192))
        if attack:
            with pytest.raises(RuntimeExecutionError):
                execute(0)
            assert not (tmp_path / 'HOST_EXECUTED').exists()
            assert not list(seed.glob('.inference-*'))
            return
        for i in range(2):
            result = execute(i)
            assert json.loads(result.output) == {'reply':'dummy'}
            sessions.append(result.provider_session_id)
            assert not list(seed.glob('.inference-*'))
        assert sessions[0] != sessions[1]
        assert requests and all(not r.get('tools') for r in requests)
    finally:
        server.shutdown(); thread.join(); server.server_close()
