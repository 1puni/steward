"""Installed Claude continuity probe against a local fake Anthropic API.

Run with uv run python testenv/claude-session-probe.py on a Linux development fixture.
Uses an empty private HOME and fake credential, disables nonessential traffic and
provider tools, and retains its temporary home for inspection. Two independent
CLI processes must resume one transcript despite changing cwd. This is not a
real-model latency benchmark, retained-process test, or root boundary acceptance.
Pass --retention to require a completed sweep: a default-policy control must delete
a synthetic aged transcript through a symlink; the long-retention launch must keep it.
"""
import http.server, json, os, pathlib, re, signal, subprocess, sys, tempfile, threading, time

from steward_harness.runtime.providers.claude import NATIVE_RETENTION_DAYS
retention = "--retention" in sys.argv[1:]
root = pathlib.Path(tempfile.mkdtemp(prefix='steward-native-pin-probe-'))
requests = []

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        if retention:
            time.sleep(8)  # Installed CLI schedules housekeeping after five seconds.
        requests.append({'messages': len(body.get('messages', []))})
        message = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'model': body.get('model', 'claude-sonnet-4-6'), 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 1}}
        self.send_response(200)
        if body.get('stream'):
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            events = [{'type': 'message_start', 'message': message}, {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}, {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'fixture reply'}}, {'type': 'content_block_stop', 'index': 0}, {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 1}}, {'type': 'message_stop'}]
            for event in events:
                self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())
        else:
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            message.update(content=[{'type': 'text', 'text': 'fixture reply'}], stop_reason='end_turn')
            self.wfile.write(json.dumps(message).encode())
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
home = root / 'home'
home.mkdir()
native = root / 'native'
native.mkdir()
if retention:
    artefacts = root / 'artefacts'
    artefacts.mkdir()
    (native / 'projects').symlink_to(artefacts, target_is_directory=True)
env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home), 'CLAUDE_CONFIG_DIR': str(native), 'CLAUDE_CODE_PROJECT_DIR_NAME': 'steward', 'ANTHROPIC_API_KEY': 'fixture-not-a-secret', 'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{server.server_port}', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1', 'DISABLE_TELEMETRY': '1'}
results = []
session = None
try:
    for name in (('first-cwd', 'control-cwd', 'protected-cwd') if retention else ('first-cwd', 'second-cwd')):
        cwd = root / name
        cwd.mkdir()
        old = None
        if retention and session:
            original = native / 'projects/steward' / (session + '.jsonl')
            old = native / 'projects/old-project/00000000-0000-4000-8000-000000000001.jsonl'
            old.parent.mkdir(exist_ok=True)
            content = original.read_text().replace(session, old.stem)
            old.write_text(re.sub(r'\d{4}-\d{2}-\d{2}T[^" ]+', '2000-01-01T00:00:00.000Z', content))
            os.utime(old, (1, 1))
            (native / '.last-cleanup').unlink(missing_ok=True)
        start = time.monotonic()
        process = subprocess.Popen(['/usr/local/bin/claude', '-p', '--model', 'claude-sonnet-4-6', '--tools', '', '--output-format', 'json', '--permission-mode', 'dontAsk', *(['--resume', session] if session else []), *(['--settings', json.dumps({'cleanupPeriodDays': NATIVE_RETENTION_DAYS})] if name == 'protected-cwd' else []), 'Reply fixture.'], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            out, err = process.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            out, err = process.communicate()
            raise
        parsed = json.loads(out)
        session = parsed.get('session_id') or next(native.glob('projects/steward/*.jsonl')).stem
        if old is not None:
            assert (native / '.last-cleanup').exists(), 'sweep did not complete'
            assert old.exists() == (name == 'protected-cwd'), 'retention behavior changed'
        results.append({'old_preserved': old.exists() if old is not None else None, 'session': session, 'cwd': name, 'returncode': process.returncode, 'seconds': round(time.monotonic() - start, 3), 'result': parsed.get('result'), 'stderr': err.decode()[-1000:]})
finally:
    server.shutdown()
assert all((result['returncode'] == 0 and result['result'] == 'fixture reply' for result in results)), results
assert all(result['session'] == results[0]['session'] for result in results)
assert len(list(native.glob('projects/steward/*.jsonl'))) == 1
assert [request['messages'] for request in requests] == ([1, 3, 5] if retention else [1, 3]), requests
if not retention:
    assert [p.name for p in (native / 'projects').iterdir()] == ['steward']
print(json.dumps({'fixture': str(root), 'results': results, 'requests': requests, 'project_dirs': [p.name for p in (native / 'projects').glob('*')], 'transcripts': [str(p.relative_to(native)) for p in native.glob('projects/**/*.jsonl')]}, indent=2))
