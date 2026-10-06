"""Exercise installed native OAuth refresh with synthetic credentials only.

Run python3 testenv/claude-refresh-probe.py. Requires Linux strace, openssl and Claude 2.1.281.
Traces successful credential-file opens (no file contents) to require both CLIs
to access the expired store before the first refresh response. Exact request
counts intentionally fail on unexpected provider retries.
A process-local HTTPS proxy terminates TLS with a fixture-only CA and never
forwards requests. Shared-store concurrency must refresh once; separate stores
must refresh twice. Model requests intentionally fail after refresh. This is not
real account authorization, a successful model turn or root/systemd acceptance.
Fixtures remain in private temporary directories for inspection.
"""
import concurrent.futures, http.server, json, os, pathlib, re, signal, ssl, stat, subprocess, tempfile, threading, time
root = pathlib.Path(tempfile.mkdtemp(prefix='steward-refresh-proxy-'))
subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(root / 'key.pem'), '-out', str(root / 'cert.pem'), '-days', '1', '-subj', '/CN=platform.claude.com', '-addext', 'subjectAltName=DNS:platform.claude.com,DNS:api.anthropic.com,DNS:claude.ai'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(root / 'cert.pem', root / 'key.pem')
requests = []

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.flush()
        try:
            sock = context.wrap_socket(self.connection, server_side=True)
            Handler(sock, self.client_address, self.server)
        except (OSError, ssl.SSLError):
            pass
        self.close_connection = True

    def do_GET(self):
        requests.append({'method': 'GET', 'path': self.path})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{}')

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        record = {'method': 'POST', 'path': self.path, 'rotated_access': self.headers.get('Authorization') == 'Bearer fixture-new-access', 'arrived': time.time()}
        requests.append(record)
        if '/oauth/token' in self.path:
            record['refresh_token_ok'] = json.loads(body).get('refresh_token') == 'fixture-expired-refresh'
            time.sleep(2)
            value = {'access_token': 'fixture-new-access', 'refresh_token': 'fixture-new-refresh', 'expires_in': 86400, 'token_type': 'Bearer', 'scope': 'user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload'}
        else:
            value = {'error': {'type': 'invalid_request_error', 'message': 'fixture stops after refresh'}}
        self.send_response(200 if '/oauth/token' in self.path else 400)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        record['response_started'] = time.time()
        self.wfile.write(json.dumps(value).encode())
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
home = root / 'home'
home.mkdir()
base = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home), 'HTTPS_PROXY': f'http://127.0.0.1:{server.server_port}', 'HTTP_PROXY': f'http://127.0.0.1:{server.server_port}', 'NODE_EXTRA_CA_CERTS': str(root / 'cert.pem'), 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1', 'DISABLE_TELEMETRY': '1'}
version = subprocess.check_output(['/usr/local/bin/claude', '--version'], env=base, cwd=root, text=True).strip()
assert version == '2.1.281 (Claude Code)', 'Revalidate native refresh for changed CLI: ' + version
results = []
try:
    for shared in [False, True]:
        scenario = root / ('shared' if shared else 'separate')
        scenario.mkdir()
        stores = [scenario / ('store-' + str(i)) for i in range(1 if shared else 2)]
        for seed in stores:
            seed.mkdir(mode=0o700)
            auth = seed / '.credentials.json'
            auth.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'fixture-expired-access', 'refreshToken': 'fixture-expired-refresh', 'expiresAt': 1, 'scopes': ['user:profile', 'user:inference'], 'subscriptionType': 'pro', 'rateLimitTier': 'default_claude_pro'}}))
            auth.chmod(0o600)
        barrier = threading.Barrier(2)

        def run(i):
            owner = scenario / ('owner-' + str(i))
            owner.mkdir(mode=0o700)
            barrier.wait(timeout=10)
            process = subprocess.Popen(['/usr/bin/strace', '-f', '-ttt', '-e', 'trace=openat', '-o', str(owner / 'opens.log'), '/usr/local/bin/claude', '-p', '--model', 'sonnet', '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--output-format', 'json', 'fixture'], cwd=owner, env=dict(base, CLAUDE_CONFIG_DIR=str(owner), CLAUDE_SECURESTORAGE_CONFIG_DIR=str(stores[0 if shared else i])), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                out, err = process.communicate(timeout=25)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
                raise
            assert process.returncode == 1 and 'fixture stops after refresh' in out, 'fixture must reach intentional post-refresh model refusal'
            assert not (owner / '.credentials.json').exists(), 'runtime home must not fork secure storage'
            return {'owner': i, 'post_refresh_refusal': True}
        start = len(requests)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, range(2)))
        seen = requests[start:]
        refreshes = [r for r in seen if '/oauth/token' in r['path']]
        messages = [r for r in seen if '/v1/messages' in r['path']]
        assert len(refreshes) == (1 if shared else 2), seen
        assert all(r['refresh_token_ok'] for r in refreshes), seen
        first_response = min(r['response_started'] for r in refreshes)
        for i in range(2):
            auth_path = str(stores[0 if shared else i] / '.credentials.json')
            lines = (scenario / ('owner-' + str(i)) / 'opens.log').read_text().splitlines()
            opens = [float(m.group(1)) for line in lines
                     if auth_path in line and 'O_RDONLY' in line and re.search(r'= [0-9]+$', line)
                     if (m := re.search(r'([0-9]+\.[0-9]+) openat', line))]
            assert opens and min(opens) < first_response, 'inconclusive: both CLIs must open expired store before refresh response'
        assert len(messages) == 2 and all((r['rotated_access'] for r in messages)), seen
        for seed in stores:
            stored = json.loads((seed / '.credentials.json').read_text())['claudeAiOauth']
            assert stored['accessToken'] == 'fixture-new-access'
            assert stored['refreshToken'] == 'fixture-new-refresh'
            assert stored['expiresAt'] > time.time() * 1000
            assert stored['subscriptionType'] == 'pro'
            assert stat.S_IMODE((seed / '.credentials.json').stat().st_mode) == 0o600
        results.append({'shared': shared, 'refresh_requests': len(refreshes), 'model_requests_with_rotated_access': len(messages), 'outcomes': outcomes})
finally:
    server.shutdown()
    server.server_close()
print(json.dumps({'version': version, 'fixture': str(root), 'results': results}, indent=2))
