#!/usr/bin/env python3
"""Installed Codex evidence fixture against a loopback Responses API; no inference."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import shutil
import tempfile
import threading
import time

from steward_harness.runtime.native_evidence import snapshot
from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.process import ProcessController


def main(*, retention_probe=False):
    calls = []
    controller = ProcessController(UntrustedExecutionBroker(UntrustedExecutionConfig()))
    command = "python3 -c \"print('CODEX_NATIVE_EVIDENCE_' + 'x' * 200000)\""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            outputs = [i for i in request.get('input', []) if i.get('type') == 'function_call_output']
            names = [t.get('name') for t in request.get('tools', [])]
            calls.append({'outputs': bool(outputs), 'tools': names})
            if outputs or not names:
                item = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant',
                        'status': 'completed', 'content': [{'type': 'output_text',
                        'text': json.dumps({'raw_memory': '', 'rollout_summary': '', 'rollout_slug': None})
                        if not names else 'Synthetic fixture complete.', 'annotations': []}]}
            else:
                if 'exec_command' in names:
                    name, args = 'exec_command', {'cmd': command, 'max_output_tokens': 100000}
                elif 'shell_command' in names:
                    name, args = 'shell_command', {'command': command, 'timeout_ms': 10000}
                elif 'shell' in names:
                    name, args = 'shell', {'command': ['bash', '-lc', command], 'timeout_ms': 10000}
                else:
                    raise RuntimeError('no native shell tool exposed: ' + repr(names))
                item = {'id': 'fc_fixture', 'type': 'function_call', 'call_id': 'call_fixture',
                        'name': name, 'arguments': json.dumps(args), 'status': 'completed'}
            response = {'id': 'resp_fixture', 'object': 'response', 'created_at': 0,
                        'status': 'completed', 'model': request.get('model', 'fixture'),
                        'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
            events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                      {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
                      {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                      {'type': 'response.completed', 'response': response}]
            payload = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix='steward-codex-evidence-') as temporary:
            root = Path(temporary)
            home, work = root / 'home', root / 'work'
            home.mkdir(mode=0o700)
            work.mkdir()
            settings = {'model_provider': 'fixture', 'model': 'gpt-5.4',
                        'model_providers.fixture.name': 'Synthetic fixture',
                        'model_providers.fixture.base_url': f'http://127.0.0.1:{server.server_port}/v1',
                        'model_providers.fixture.wire_api': 'responses',
                        'model_providers.fixture.requires_openai_auth': False,
                        'features.apps': False, 'features.code_mode': False, 'features.code_mode_host': False}
            if retention_probe:
                # Fixture-only: exercise pruning without real account rate limits.
                settings.update({'features.memories': True,
                                 'memories.min_rate_limit_remaining_percent': 0})
            args = ['codex', 'exec', '--skip-git-repo-check', '--sandbox', 'workspace-write', '--json']
            for key, value in settings.items():
                args += ['-c', key + '=' + json.dumps(value)]
            result = controller.run([*args, 'Run the supplied synthetic fixture.'],
                env={'PATH': os.environ['PATH'], 'HOME': str(home), 'CODEX_HOME': str(home)},
                cwd=work, timeout_seconds=60)
            if result.returncode:
                raise RuntimeError(f'isolated Codex exited {result.returncode}: ' + result.stderr[-1000:])
            evidence = [p for p in home.rglob('*') if p.is_file() and not p.is_symlink()
                        and b'CODEX_NATIVE_EVIDENCE_' + b'x' * 1000 in p.read_bytes()]
            assert evidence and any(c['outputs'] for c in calls), 'native command evidence missing'
            rollout = next(p for p in evidence if p.suffix == '.jsonl')
            original = rollout.read_bytes()
            assert b'x' * 200000 in original, 'complete native command result missing'
            memory = home / 'memories/MEMORY.md'
            memory.parent.mkdir(exist_ok=True)
            memory.write_text('Synthetic old native memory.\n')
            memory_digest = hashlib.sha256(memory.read_bytes()).hexdigest()
            for path in [rollout, memory]:
                os.utime(path, (946684800, 946684800))
            retention_rows = [
                ('synthetic-old-stage1', 946684800, 'old raw memory', 'old summary', 946684800, 0),
                ('synthetic-recent-stage1', int(time.time()), 'recent raw memory', 'recent summary', int(time.time()), 0),
                ('synthetic-selected-stage1', 946684800, 'selected raw memory', 'selected summary', 946684800, 1),
            ]
            if retention_probe:
                with sqlite3.connect(home / 'memories_1.sqlite') as database:
                    database.executemany(
                        "INSERT INTO stage1_outputs(thread_id,source_updated_at,raw_memory,rollout_summary,generated_at,selected_for_phase2) VALUES (?,?,?,?,?,?)",
                        retention_rows)
            copied = snapshot(home, {})
            subprocess.run(['git', 'init', '-q', str(work)], check=True)
            (work / 'fixture.txt').write_text('synthetic capture candidate')
            lock = work / '.git/index.lock'
            lock.write_text('failed capture fixture')
            capture = subprocess.run(['git', '-C', str(work), 'add', 'fixture.txt'], capture_output=True)
            assert capture.returncode != 0 and b'index.lock' in capture.stderr
            lock.unlink()
            subprocess.run(['git', '-C', str(work), 'add', 'fixture.txt'], check=True)
            hook = work / '.git/hooks/pre-commit'
            hook.write_text('#!/bin/sh\nprintf refused > .git/hook-ran\nexit 1\n')
            hook.chmod(0o700)
            assert subprocess.run(['git', '-C', str(work), '-c', 'user.name=Fixture',
                '-c', 'user.email=fixture@localhost', '-c', 'core.hooksPath=.git/hooks',
                '-c', 'commit.gpgsign=false', 'commit', '-m', 'fixture'], capture_output=True).returncode != 0
            assert (work / '.git/hook-ran').read_text() == 'refused'
            started = next(json.loads(line) for line in result.stdout.splitlines()
                           if json.loads(line).get('type') == 'thread.started')
            resumed = controller.run([*args, 'resume', started['thread_id'], 'Continue the fixture.'],
                env={'PATH': os.environ['PATH'], 'HOME': str(home), 'CODEX_HOME': str(home)},
                cwd=work, timeout_seconds=60)
            if resumed.returncode:
                raise RuntimeError('native resume failed: ' + resumed.stderr[-1000:])
            assert rollout.read_bytes().startswith(original), 'resume changed previous native evidence'
            assert hashlib.sha256(memory.read_bytes()).hexdigest() == memory_digest
            if retention_probe:
                with sqlite3.connect(home / 'memories_1.sqlite') as database:
                    remaining = {row[0] for row in database.execute(
                        "SELECT thread_id FROM stage1_outputs")}
                assert 'synthetic-old-stage1' not in remaining, 'native pruning did not execute'
                assert {'synthetic-recent-stage1', 'synthetic-selected-stage1'} <= remaining
            restore = root / 'restore'
            manifest = json.loads((copied / 'SHA256.json').read_text())
            for name, digest in manifest.items():
                destination = restore / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(copied / name, destination)
                assert hashlib.sha256(destination.read_bytes()).hexdigest() == digest
            schemas = {}
            for path in (restore / 'home').glob('*.sqlite'):
                database = sqlite3.connect(path)
                try:
                    assert database.execute('PRAGMA integrity_check').fetchone() == ('ok',)
                    schemas[path.name] = [row[0] for row in database.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")]
                    if retention_probe and path.name == 'memories_1.sqlite':
                        restored_rows = database.execute(
                            "SELECT thread_id,source_updated_at,raw_memory,rollout_summary,generated_at,selected_for_phase2 FROM stage1_outputs WHERE thread_id LIKE 'synthetic-%'").fetchall()
                        assert sorted(restored_rows) == sorted(retention_rows), 'pre-pruning evidence not restored'
                    if path.name == 'thread_history_1.sqlite':
                        assert database.execute('SELECT COUNT(*) FROM thread_items').fetchone()[0] > 0
                finally:
                    database.close()
            print(json.dumps({'provider': subprocess.check_output(['codex', '--version'], text=True).strip(),
                              'native_evidence_paths': [str(p.relative_to(home)) for p in evidence],
                              'full_output_files': [str(p.relative_to(home)) for p in evidence if b'x' * 200000 in p.read_bytes()],
                              'restored_native_schemas': schemas, 'native_resume': True,
                              'restored_rollout_sha256': hashlib.sha256(original).hexdigest(),
                              'tools': next(c['tools'] for c in calls if c['tools']),
                              'native_pruning_and_restore': retention_probe, 'inference': False}))
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retention', action='store_true', help='exercise native stage-1 pruning and restore')
    main(retention_probe=parser.parse_args().retention)
