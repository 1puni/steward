"""An acceptance run must never remove another fixture's persistent state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest


@pytest.mark.parametrize('resource', ['container', 'volume', 'network'])
@pytest.mark.parametrize('project_kind', ['acceptance', 'selftest'])
def test_followthrough_refuses_existing_project_without_mutation(tmp_path, resource, project_kind):
    log = tmp_path / 'docker-calls.jsonl'
    fake_docker = tmp_path / 'docker'
    fake_docker.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['DOCKER_CALL_LOG']).open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
queries = {'container': ['ps', '-aq'], 'volume': ['volume', 'ls'], 'network': ['network', 'ls']}
if args[:2] not in queries.values():
    sys.exit(99)  # Never contact Docker, even if the runner regresses.
if args[:2] == queries[os.environ['EXISTING_RESOURCE']] and ('label=com.docker.compose.project=' + os.environ['EXISTING_PROJECT']) in args:
    print('preexisting-fixture-resource')
''')
    fake_docker.chmod(0o755)
    ident = uuid.uuid4().hex
    projects = {'acceptance': f'safety-acceptance-{ident}', 'selftest': f'safety-selftest-{ident}'}
    runner = Path(__file__).resolve().parents[1] / 'scripts/follow-through-acceptance.sh'
    result = subprocess.run(
        ['sh', str(runner), 'A'], capture_output=True, text=True, timeout=10,
        env={**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
             'FAKE_PORT': '19999', 'ACCEPT_PROJECT': projects['acceptance'],
             'SELFTEST_PROJECT': projects['selftest'], 'KEEP': '0',
             'DOCKER_CALL_LOG': str(log), 'EXISTING_RESOURCE': resource,
             'EXISTING_PROJECT': projects[project_kind]},
    )
    assert result.returncode != 0
    assert f'Refusing existing Compose project {projects[project_kind]}' in result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls
    assert all(call[:2] in [['ps', '-aq'], ['volume', 'ls'], ['network', 'ls']] for call in calls), calls
    assert not any(set(call) & {'up', 'down', 'rm', 'build', 'prune'} for call in calls), calls


def test_followthrough_rejects_invalid_scenario_before_probing_resources(tmp_path):
    log = tmp_path / 'unexpected-probe'
    # Both external probes fail closed and leave evidence if invoked. Omitting
    # FAKE_PORT also proves scenario validation precedes local port allocation.
    fake_probe = f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(log)!r}).touch()\nraise SystemExit(99)\n'
    for name in ('docker', 'python3'):
        path = tmp_path / name
        path.write_text(fake_probe)
        path.chmod(0o755)
    runner = Path(__file__).resolve().parents[1] / 'scripts/follow-through-acceptance.sh'
    environment = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH']}
    environment.pop('FAKE_PORT', None)
    result = subprocess.run(['sh', str(runner), 'typo'], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert 'Usage:' in result.stderr
    assert not log.exists()
