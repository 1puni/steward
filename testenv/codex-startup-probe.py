"""Measure installed Codex initialize handshakes with empty cold/warm homes.

No model turn, credentials, imported history or process reuse. Each sample starts
and contains its own process group; fixture homes and diagnostics are retained.
The warm samples reuse provider files, not a process. This is not UID/systemd or
production latency acceptance, and does not reproduce the reported large-history
backfill workload. Run with: uv run python testenv/codex-startup-probe.py
"""
import json
import os
from pathlib import Path
import queue
import signal
import statistics
import subprocess
import tempfile
import threading
import time


def sample(root, native, index):
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(root / 'home'),
           'CODEX_HOME': str(native), 'NO_PROXY': '127.0.0.1', 'no_proxy': '127.0.0.1'}
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        env[name] = 'http://127.0.0.1:1'
    incoming = queue.Queue()
    with (root / f'stderr-{index}.log').open('w') as errors:
        started = time.monotonic()
        process = subprocess.Popen(['codex', '-c', 'cli_auth_credentials_store="file"', 'app-server'],
            cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errors, text=True, start_new_session=True)
        def collect():
            try:
                for line in process.stdout:
                    incoming.put(json.loads(line))
            except Exception as error:
                incoming.put({'fixture_error': type(error).__name__})
            finally:
                incoming.put(None)
        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            process.stdin.write(json.dumps({'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'steward-startup-fixture', 'version': '1'}}}) + '\n')
            process.stdin.flush()
            deadline = started + 15
            while True:
                response = incoming.get(timeout=max(.001, deadline - time.monotonic()))
                if response is None or 'fixture_error' in response:
                    raise RuntimeError('initialize response stream ended')
                if response.get('id') == 1 and 'method' not in response:
                    if 'result' not in response:
                        raise RuntimeError('initialize rejected')
                    elapsed = time.monotonic() - started
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('initialize deadline expired')
            process.stdin.write('{"method":"initialized"}\n')
            process.stdin.flush()
            # Give the native server a bounded interval to create its own state.
            # Not counted in handshake latency; not a quiescence assertion.
            time.sleep(.25)
        finally:
            # Kill before wait/reap: keep the group leader identity reserved.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            reader.join(timeout=5)
            process.stdin.close()
            process.stdout.close()
            if reader.is_alive():
                raise RuntimeError('fixture reader did not finish after containment')
    return {'seconds': elapsed, 'home': str(native), 'database_inodes': {
        name: (native / name).stat().st_ino for name in
        ('state_5.sqlite', 'goals_1.sqlite', 'memories_1.sqlite', 'queue_1.sqlite')}, 'files': sorted(
        str(path.relative_to(native)) for path in native.rglob('*') if path.is_file())}


def main():
    root = Path(tempfile.mkdtemp(prefix='steward-codex-startup-'))
    (root / 'home').mkdir(mode=0o700)
    version_home = root / 'version'
    version_home.mkdir(mode=0o700)
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(root / 'home'), 'CODEX_HOME': str(version_home)}
    version = subprocess.check_output(['codex', '--version'], env=env, cwd=root, text=True, timeout=10).strip()
    if version != 'codex-cli 0.153.4':
        raise RuntimeError('revalidate changed CLI: ' + version)
    cold, warm = [], []
    for index in range(5):
        native = root / f'pair-{index}'
        native.mkdir(mode=0o700)
        cold.append(sample(root, native, f'{index}-cold'))
        warm.append(sample(root, native, f'{index}-warm'))
        if cold[-1]['database_inodes'] != warm[-1]['database_inodes']:
            raise RuntimeError('provider replaced a retained database during warm startup')
    result = {'version': version, 'fixture': str(root), 'provider_process_starts': len(cold) + len(warm),
        'history_records': 0, 'model_turns': 0, 'cold': cold, 'warm': warm,
        'cold_median_seconds': statistics.median(row['seconds'] for row in cold),
        'warm_median_seconds': statistics.median(row['seconds'] for row in warm),
        'retained_fixture_homes': [str(root / f'pair-{index}') for index in range(5)]}
    (root / 'result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
