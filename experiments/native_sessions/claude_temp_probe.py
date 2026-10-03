#!/usr/bin/env python3
"""Exercise installed Claude's Bash/temp path using a local synthetic API.

No real provider credentials, paid inference or existing private homes are used.
This tests one foreground Bash result, not all temporary-output cleanup paths.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading

from retention_startup_probe import startup
from steward_harness.runtime.native_evidence import snapshot


def main():
    calls = []
    command = "python3 -c \"import os; print('NATIVE_TMP_EVIDENCE_' + 'x' * 200000); print('SANDBOX_RUNTIME=' + os.environ.get('SANDBOX_RUNTIME', 'unset'))\""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'count_tokens' in self.path:
                payload = json.dumps({'input_tokens': 1}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            messages = request.get('messages', [])
            results = [block for message in messages for block in message.get('content', [])
                       if isinstance(block, dict) and block.get('type') == 'tool_result']
            calls.append(bool(results))
            block = ({'type': 'text', 'text': 'Synthetic fixture complete.'} if results else
                     {'type': 'tool_use', 'id': 'tool_fixture', 'name': 'Bash', 'input': {}})
            events = [
                ('message_start', {'type': 'message_start', 'message': {
                    'id': 'msg_fixture', 'type': 'message', 'role': 'assistant',
                    'content': [], 'model': request.get('model', 'fixture'),
                    'stop_reason': None, 'stop_sequence': None,
                    'usage': {'input_tokens': 1, 'output_tokens': 0}}}),
                ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': block}),
            ]
            if not results:
                events.append(('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                    'delta': {'type': 'input_json_delta', 'partial_json': json.dumps({
                        'command': command, 'description': 'Write synthetic native output', 'timeout': 10000})}}))
            events += [
                ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                ('message_delta', {'type': 'message_delta', 'delta': {
                    'stop_reason': 'end_turn' if results else 'tool_use', 'stop_sequence': None},
                    'usage': {'output_tokens': 1}}),
                ('message_stop', {'type': 'message_stop'}),
            ]
            payload = ''.join(f'event: {name}\ndata: {json.dumps(data)}\n\n' for name, data in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix='steward-claude-tool-') as temporary:
            root = Path(temporary)
            work = root / 'work'
            work.mkdir()
            home = root / ('native-' + 'long-' * 28)
            home.mkdir(mode=0o700)
            native_tmp = home / '.steward-tmp'
            native_tmp.mkdir(mode=0o700)
            settings = {'cleanupPeriodDays': 365000,
                        'env': {'CLAUDE_CODE_TMPDIR': str(native_tmp)},
                        'sandbox': {'enabled': True, 'failIfUnavailable': True,
                                    'allowUnsandboxedCommands': False,
                                    'network': {'allowAllUnixSockets': True}}}
            environment = {'PATH': os.environ['PATH'], 'HOME': str(home),
                           'CLAUDE_CONFIG_DIR': str(home), 'CLAUDE_CODE_TMPDIR': str(native_tmp),
                           'ANTHROPIC_API_KEY': 'synthetic-fixture-only',
                           'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{server.server_port}',
                           'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}
            result = subprocess.run(['claude', '-p', 'Run the supplied synthetic fixture.',
                '--model', 'sonnet', '--output-format', 'json', '--allowed-tools', 'Bash',
                '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                '--settings', json.dumps(settings)], env=environment, cwd=work,
                capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError(f'isolated Claude exited {result.returncode}')
            evidence = [p for p in home.rglob('*') if p.is_file() and not p.is_symlink()
                        and b'NATIVE_TMP_EVIDENCE_' + b'x' * 200000 + b'\n' in p.read_bytes()]
            assert False in calls and True in calls, 'native tool result was not returned'
            assert evidence, 'native Bash did not retain the complete synthetic output'
            assert any(p.is_relative_to(native_tmp) for p in evidence)
            assert any('tool-results' in p.parts for p in evidence)
            assert any(b'SANDBOX_RUNTIME=1' in p.read_bytes() for p in evidence)
            transcripts = list((home / 'projects').glob('*/*.jsonl'))
            assert transcripts, 'native transcript missing'
            memory = transcripts[0].parent / 'memory/MEMORY.md'
            memory.parent.mkdir(exist_ok=True)
            memory.write_text('Synthetic native memory retained across restart.\n')
            expected = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in [*evidence, *transcripts, memory]}
            for path in expected:
                os.utime(path, (946684800, 946684800))
            copied = snapshot(home, {})
            # Both actual Git failure paths use only a separate synthetic repo.
            subprocess.run(['git', 'init', '-q', str(work)], check=True)
            fixture = work / 'fixture.txt'
            fixture.write_text('synthetic capture candidate')
            lock = work / '.git/index.lock'
            lock.write_text('simulate failed capture')
            capture = subprocess.run(['git', '-C', str(work), 'add', 'fixture.txt'], capture_output=True)
            assert capture.returncode != 0 and b'index.lock' in capture.stderr
            lock.unlink()
            subprocess.run(['git', '-C', str(work), 'add', 'fixture.txt'], check=True)
            hook = work / '.git/hooks/pre-commit'
            hook.write_text('#!/bin/sh\nprintf refused > .git/hook-ran\nexit 1\n')
            hook.chmod(0o700)
            assert subprocess.run(['git', '-C', str(work), '-c', 'user.name=Fixture',
                                   '-c', 'user.email=fixture@localhost', '-c', 'core.hooksPath=.git/hooks',
                                   '-c', 'commit.gpgsign=false', 'commit', '-m', 'fixture'],
                                  capture_output=True).returncode != 0
            assert (work / '.git/hook-ran').read_text() == 'refused'
            startup('claude', home, work, native_tmp=native_tmp)
            assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest
                       for p, digest in expected.items())
            # Restore one selected complete native temporary result after
            # deleting only that synthetic fixture; never touch a real record.
            selected = next(p for p in expected if p.is_relative_to(native_tmp))
            restore = root / 'restored-output'
            selected.unlink()
            restore.write_bytes((copied / 'home' / selected.relative_to(home)).read_bytes())
            assert hashlib.sha256(restore.read_bytes()).hexdigest() == expected[selected]
            print(json.dumps({'provider': subprocess.check_output(['claude', '--version'], text=True).strip(),
                              'native_tmp_path_length': len(str(native_tmp)),
                              'api': 'loopback synthetic responses; no inference',
                              'native_evidence_paths': [str(p.relative_to(home)) for p in expected],
                              'sandbox_runtime_observed': True,
                              'old_native_fixtures_survived_restart_and_git_failures': len(expected),
                              'selected_native_output_restore_sha256': expected[selected]}))
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == '__main__':
    main()
