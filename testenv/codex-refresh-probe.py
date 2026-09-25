"""Probe installed Codex refresh with synthetic expired credentials only.

Requires Linux strace and Codex 0.153.4. Each owner has a distinct localhost
refresh URL. Other HTTP proxy settings point to a closed loopback port, and
connect syscalls are checked for non-loopback destinations (not a firewall).
No real credentials are supplied or successful model turn claimed. Fixtures are
retained. Exit 1 means cross-owner duplicate observed; 2 means inconclusive;
0 means no duplicate observed in this bounded run, not general auth acceptance.
"""
import base64
import concurrent.futures
import http.server
import json
import os
import pathlib
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time


def main():
    root = pathlib.Path(tempfile.mkdtemp(prefix='steward-codex-refresh-'))
    home, seed = root / 'home', root / 'seed'
    home.mkdir()
    seed.mkdir()
    base = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home), 'CODEX_HOME': str(seed)}
    version = subprocess.check_output(['codex', '--version'], env=base, cwd=root, text=True).strip()
    if version != 'codex-cli 0.153.4':
        raise RuntimeError('Revalidate changed CLI: ' + version)

    def jwt(n):
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
        claims = {'email': 'fixture@example.invalid', 'exp': 1 if n == 0 else int(time.time()) + 86400,
                  'https://api.openai.com/auth': {'chatgpt_account_id': 'fixture-account', 'chatgpt_plan_type': 'plus'}, 'nonce': n}
        return encode({'alg': 'none'}) + '.' + encode(claims) + '.fixture'

    auth = seed / 'auth.json'
    auth.write_text(json.dumps({'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None, 'tokens': {
        'id_token': jwt(0), 'access_token': jwt(0), 'refresh_token': 'fixture-old-refresh',
        'account_id': 'fixture-account'}, 'last_refresh': '2020-01-01T00:00:00Z'}))
    auth.chmod(0o600)
    requests, handler_errors = [], []
    lock = threading.Lock()
    completed = [threading.Event(), threading.Event()]

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                owner = {'/token/0': 0, '/token/1': 1}[self.path]
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with lock:
                    requests.append({'owner': owner, 'old_refresh': body.get('refresh_token') == 'fixture-old-refresh'})
                    n = len(requests)
                time.sleep(1)
                value = {'id_token': jwt(n), 'access_token': jwt(n), 'refresh_token': f'fixture-new-refresh-{n}'}
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())
                self.wfile.flush()
                completed[owner].set()
            except Exception as error:
                with lock:
                    handler_errors.append(type(error).__name__)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    barrier = threading.Barrier(2)

    def run(i):
        owner = root / str(i)
        owner.mkdir()
        (owner / 'auth.json').symlink_to(auth)
        env = dict(base, CODEX_HOME=str(owner), CODEX_REFRESH_TOKEN_URL_OVERRIDE=f'http://127.0.0.1:{server.server_port}/token/{i}')
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            env[key] = 'http://127.0.0.1:1'
        env.update(NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
        with (owner / 'stderr.log').open('w') as errors:
            process = subprocess.Popen(['/usr/bin/strace', '-f', '-e', 'trace=connect', '-o', str(owner / 'connect.log'),
                'codex', '-c', 'cli_auth_credentials_store="file"', 'app-server'], cwd=owner, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, start_new_session=True)
            incoming = queue.Queue()

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

            def send(value):
                process.stdin.write(json.dumps(value) + '\n')
                process.stdin.flush()

            def read(identifier):
                end = time.monotonic() + 15
                while True:
                    value = incoming.get(timeout=max(.01, end - time.monotonic()))
                    if value is None or 'fixture_error' in value:
                        raise RuntimeError('app-server response stream ended')
                    if value.get('id') == identifier and 'method' not in value and ('result' in value or 'error' in value):
                        return value
                    if time.monotonic() >= end:
                        raise RuntimeError('protocol timeout')

            result = {'owner': i}
            try:
                send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'fixture', 'version': '1'}}})
                if 'result' not in read(1):
                    raise RuntimeError('initialize rejected')
                send({'method': 'initialized'})
                barrier.wait(timeout=10)
                send({'id': 2, 'method': 'thread/start', 'params': {'model': 'gpt-5.4', 'cwd': str(owner), 'approvalPolicy': 'never', 'sandbox': 'read-only'}})
                thread = read(2)['result']['thread']['id']
                send({'id': 3, 'method': 'turn/start', 'params': {'threadId': thread, 'input': [{'type': 'text', 'text': 'fixture only'}]}})
                result['turn_started'] = 'result' in read(3)
                result['both_refresh_responses'] = all(event.wait(10) for event in completed)
            except Exception as error:
                result['fixture_error'] = type(error).__name__
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
                reader.join(timeout=5)
                process.stdin.close()
                process.stdout.close()
            result['link_preserved'] = (owner / 'auth.json').is_symlink()
            trace = (owner / 'connect.log').read_text()
            addresses = re.findall(r'inet_addr\("([^\"]+)"\)|inet_pton\(AF_INET6, "([^\"]+)"', trace)
            result['loopback_only_connects'] = bool(addresses) and all((ipv4 or ipv6) in ('127.0.0.1', '::1') for ipv4, ipv6 in addresses)
            result['stderr'] = str(owner / 'stderr.log')
            return result

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
    finally:
        server.shutdown()
        server.server_close()
    with lock:
        seen = list(requests)
    duplicate = {r['owner'] for r in seen if r['old_refresh']} == {0, 1}
    healthy = not handler_errors and all(r.get('turn_started') and r.get('both_refresh_responses') and r.get('loopback_only_connects') and not r.get('fixture_error') for r in results)
    verdict = 'inconclusive' if not healthy else 'duplicate_observed' if duplicate else 'no_duplicate_observed'
    print(json.dumps({'version': version, 'fixture': str(root), 'requests': seen, 'results': results,
                      'duplicate_refresh_submission': duplicate, 'handler_errors': handler_errors, 'verdict': verdict}, indent=2))
    return 2 if not healthy else 1 if duplicate else 0


if __name__ == '__main__':
    try:
        code = main()
    except Exception as error:
        print(json.dumps({'verdict': 'inconclusive', 'error': type(error).__name__, 'detail': str(error)}))
        code = 2
    raise SystemExit(code)
