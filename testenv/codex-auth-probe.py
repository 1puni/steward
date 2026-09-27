"""Check installed Codex file-store writes using synthetic API keys only.

Run python3 testenv/codex-auth-probe.py. Retains its private fixture for inspection.
This proves sequential API-key persistence through links, not OAuth refresh,
concurrent refresh safety, keyring behavior, or live credential validity.
"""
import json
import pathlib
import subprocess
import tempfile

binary = '/usr/local/bin/codex'
version = subprocess.check_output([binary, '--version'], text=True).strip()
assert version == 'codex-cli 0.153.4', 'Revalidate native auth writes for changed CLI: ' + version
root = pathlib.Path(tempfile.mkdtemp(prefix='steward-codex-auth-probe-'))
seed = root / 'seed'
seed.mkdir(mode=0o700)
home = root / 'home'
home.mkdir(mode=0o700)
auth = seed / 'auth.json'
auth.write_text(json.dumps({'OPENAI_API_KEY': 'fixture-old-not-real'}))
auth.chmod(0o600)
owners = [root / 'owner-one', root / 'owner-two']
for owner in owners:
    owner.mkdir(mode=0o700)
    (owner / 'auth.json').symlink_to(auth)
results = []
for index, owner in enumerate(owners):
    key = f'fixture-new-{index}-not-real'
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home), 'CODEX_HOME': str(owner)}
    done = subprocess.run(
        [binary, '-c', 'cli_auth_credentials_store="file"', 'login', '--with-api-key'],
        input=key + '\n', cwd=root, env=env, capture_output=True, text=True, timeout=20,
    )
    assert done.returncode == 0, 'Native fixture login failed'
    assert all((entry / 'auth.json').is_symlink() for entry in owners)
    assert json.loads(auth.read_text()).get('OPENAI_API_KEY') == key
    assert all(json.loads((entry / 'auth.json').read_text()).get('OPENAI_API_KEY') == key for entry in owners)
    results.append({'owner': owner.name, 'link_preserved': True, 'shared_write_visible': True})
print(json.dumps({'version': version, 'fixture': str(root), 'results': results}, indent=2))
