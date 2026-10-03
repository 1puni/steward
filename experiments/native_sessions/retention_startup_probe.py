#!/usr/bin/env python3
"""Real provider restart survival in disposable homes, without auth or inference.

This probes initialization only, not every asynchronous cleanup/consolidation
path. It never opens a real provider home or infers deletion-off from survival.
"""
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import tempfile
import time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def startup(provider, home, cwd, *, native_tmp=None):
    env = {'PATH': os.environ['PATH'], 'HOME': str(home),
           'CODEX_HOME': str(home), 'CLAUDE_CONFIG_DIR': str(home)}
    if native_tmp is not None:
        env['CLAUDE_CODE_TMPDIR'] = str(native_tmp)
    if provider == 'codex':
        args = ['codex', 'app-server', '--stdio', '-c', 'features.apps=false']
        messages = [dict(id=1, method='initialize', params={
            'clientInfo': {'name': 'steward-retention-probe', 'version': '1'},
            'capabilities': {'experimentalApi': True}}),
            dict(id=2, method='config/read', params={'includeLayers': False})]
    else:
        args = ['claude', '-p', '--input-format', 'stream-json', '--output-format',
                'stream-json', '--verbose', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}', '--settings',
                json.dumps({'cleanupPeriodDays': 365000, 'autoMemoryEnabled': True,
                            **({'env': {'CLAUDE_CODE_TMPDIR': str(native_tmp)}} if native_tmp else {})})]
        messages = [{'type': 'control_request', 'request_id': 'init',
                     'request': {'subtype': 'initialize'}}]
    process = subprocess.Popen(args, cwd=cwd, env=env, start_new_session=True,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = b''
    results = []
    try:
        for message in messages:
            process.stdin.write(json.dumps(message).encode() + b'\n')
            process.stdin.flush()
            until = time.monotonic() + 25
            found = False
            while time.monotonic() < until and not found:
                for key, _ in selector.select(.25):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError('provider exited before initialization')
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        item = json.loads(line)
                        if provider == 'codex' and item.get('id') == message['id']:
                            if 'error' in item:
                                raise RuntimeError('provider rejected startup RPC')
                            results.append(item['result'])
                            found = True
                        elif provider == 'claude' and item.get('type') == 'control_response':
                            found = True
                if process.poll() is not None:
                    raise RuntimeError('provider exited during startup')
            if not found:
                raise TimeoutError('provider initialization timed out')
        time.sleep(1)
        if provider == 'codex':
            config = results[-1]['config']
            return {key: config.get(key) for key in ['history', 'memories']}
        return {'launch_cleanupPeriodDays': 365000, 'effective_readback': False}
    finally:
        selector.close()
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        except ProcessLookupError:
            process.wait(timeout=5)


def main():
    output = []
    with tempfile.TemporaryDirectory(prefix='steward-native-retention-') as temporary:
        root = Path(temporary)
        for provider in ['codex', 'claude']:
            home = root / (provider if provider == 'codex' else 'claude-' + 'long-' * 28)
            home.mkdir(mode=0o700)
            native_tmp = home / '.steward-tmp' if provider == 'claude' else None
            if native_tmp is not None:
                native_tmp.mkdir(mode=0o700)
            session = '11111111-1111-4111-8111-111111111111'
            names = ([f'sessions/2000/01/01/rollout-2000-01-01T00-00-00-{session}.jsonl',
                      'memories/MEMORY.md'] if provider == 'codex' else
                     [f'projects/fixture/{session}.jsonl',
                      f'projects/fixture/{session}/subagents/agent-fixture.jsonl',
                      'projects/fixture/memory/MEMORY.md'])
            fixtures = []
            for name in names:
                path = home / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}\n' if name.endswith('jsonl') else 'synthetic old memory\n')
                os.utime(path, (946684800, 946684800))
                fixtures.append((path, digest(path)))
            settings = []
            for restart in range(2):
                settings.append(startup(provider, home, root, native_tmp=native_tmp))
                if native_tmp is not None and restart == 0:
                    uid_root = native_tmp / f'claude-{os.getuid()}'
                    assert uid_root.is_dir() and not uid_root.is_symlink()
                    assert uid_root.stat().st_mode & 0o777 == 0o700
                    # Synthetic task output under the installed cwd/task layout;
                    # no claim that initialization executes a Bash tool.
                    import re
                    output_path = uid_root / re.sub('[^a-zA-Z0-9]', '-', str(root)) / 'tasks/fixture.output'
                    output_path.parent.mkdir(parents=True)
                    output_path.write_bytes(b'old synthetic temporary tool output\n')
                    os.utime(output_path, (946684800, 946684800))
                    fixtures.append((output_path, digest(output_path)))
                assert all(path.is_file() and digest(path) == expected for path, expected in fixtures)
            schemas = {}
            for path in home.glob('*.sqlite'):
                with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as database:
                    schemas[path.name] = [row[0] for row in database.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            output.append({'native_database_tables': schemas, 'provider': subprocess.check_output([provider, '--version'], text=True).strip(),
                           'restarts': 2, 'old_fixtures_unchanged': len(fixtures),
                           'inference': False, 'settings': settings[-1],
                           'native_tmp_path_length': len(str(native_tmp)) if native_tmp else None,
                           'cleanup_path_exercised': 'initialization only; sweep not established'})
    print(json.dumps(output, sort_keys=True))


if __name__ == '__main__':
    main()
