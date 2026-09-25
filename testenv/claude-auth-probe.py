"""Check installed Claude credential-store routing with synthetic credentials only.

Run python3 testenv/claude-auth-probe.py. Uses a fresh private HOME and retains the
fixture. auth status proves credential lookup, not a successful OAuth exchange or
concurrent refresh. No production credential is read or copied.
"""
import json, os, pathlib, subprocess, tempfile, time
version = subprocess.check_output(['/usr/local/bin/claude', '--version'], text=True).strip()
assert version == '2.1.281 (Claude Code)', 'Revalidate native auth routing for changed CLI: ' + version
root = pathlib.Path(tempfile.mkdtemp(prefix='steward-native-auth-probe-'))
seed = root / 'seed'
seed.mkdir()
home = root / 'home'
home.mkdir()
credentials = seed / '.credentials.json'
credentials.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'fixture-not-a-real-access-token', 'refreshToken': 'fixture-not-a-real-refresh-token', 'expiresAt': int((time.time() + 86400) * 1000), 'scopes': ['user:inference', 'user:profile'], 'subscriptionType': 'pro', 'rateLimitTier': 'default_claude_pro'}}))
credentials.chmod(384)
base = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home), 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1', 'DISABLE_TELEMETRY': '1'}
results = []
for name, shared in [('unconfigured', False), ('owner-one', True), ('owner-two', True)]:
    owner = root / name
    owner.mkdir()
    env = dict(base, CLAUDE_CONFIG_DIR=str(owner))
    if shared:
        env['CLAUDE_SECURESTORAGE_CONFIG_DIR'] = str(seed)
    done = subprocess.run(['/usr/local/bin/claude', 'auth', 'status', '--json'], cwd=root, env=env, capture_output=True, text=True, timeout=15)
    value = json.loads(done.stdout)
    results.append({'owner': name, 'returncode': done.returncode, 'loggedIn': value.get('loggedIn'), 'authMethod': value.get('authMethod'), 'owner_credentials': (owner / '.credentials.json').exists()})
assert not results[0]['loggedIn']
assert all((r['loggedIn'] and (not r['owner_credentials']) for r in results[1:]))
assert credentials.exists()
print(json.dumps({'version': version, 'fixture': str(root), 'results': results}, indent=2))
