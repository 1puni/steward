"""Audited Git path reads stay finite without inheriting executable configuration."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from steward_harness.git import is_git_path_query
from steward_harness.runtime.git_metadata import run_metadata

pytestmark = pytest.mark.skipif(sys.platform != 'linux' or os.geteuid() == 0,
                                reason='unprivileged Linux tests; root UID tests are in host acceptance')


@pytest.fixture
def repository(tmp_path):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    return tmp_path


def test_only_fixed_path_queries_qualify():
    assert is_git_path_query(('rev-parse', '--git-path', 'MERGE_HEAD'))
    assert is_git_path_query(('rev-parse', '--path-format=absolute', '--git-common-dir'))
    for args in [('status', '--porcelain'), ('rev-parse', 'HEAD^{tree}'),
                 ('-c', 'alias.foo=!evil', 'foo'), ('fetch', 'origin'),
                 ('rev-parse', '--git-path', '../../outside'), ('rev-parse',),
                 ('rev-parse', '--show-toplevel', '--exec-path=evil')]:
        assert not is_git_path_query(args)


def test_repository_executable_configuration_is_inert(repository, monkeypatch):
    marker = repository / 'executed'
    script = repository / 'evil'
    script.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    script.chmod(0o755)
    for key in ['alias.rev-parse', 'core.fsmonitor', 'core.pager', 'core.sshCommand',
                'credential.helper', 'filter.evil.clean', 'diff.external', 'core.hooksPath']:
        subprocess.run(['git', '-C', str(repository), 'config', key, str(script)], check=True)
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.worktree')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', '/wrong-world')
    monkeypatch.setenv('GIT_DIR', '/wrong-repository')
    result = run_metadata(('rev-parse', '--show-toplevel'), cwd=repository,
                          timeout=3, home=repository, identity={})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(repository)
    assert not marker.exists()


def test_included_config_fifo_cannot_outlive_deadline(repository):
    fifo = repository / 'blocked-config'
    os.mkfifo(fifo)
    with (repository / '.git/config').open('a') as config:
        config.write(f'\n[include]\npath = {fifo}\n')
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_metadata(('rev-parse', '--show-toplevel'), cwd=repository,
                     timeout=.2, home=repository, identity={})
    assert time.monotonic() - start < 3


def test_controller_death_leaves_only_bounded_supervisor(repository):
    fifo = repository / 'blocked-config'
    os.mkfifo(fifo)
    with (repository / '.git/config').open('a') as config:
        config.write(f'\n[include]\npath = {fifo}\n')
    code = '''import sys
from pathlib import Path
from steward_harness.runtime.git_metadata import run_metadata
run_metadata(('rev-parse', '--show-toplevel'), cwd=sys.argv[1], timeout=1,
             home=Path(sys.argv[1]), identity={})
'''
    controller = subprocess.Popen([sys.executable, '-c', code, str(repository)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    children = Path(f'/proc/{controller.pid}/task/{controller.pid}/children')
    supervisor = None
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            pids = children.read_text().split()
            if pids:
                supervisor = int(pids[0])
                break
            time.sleep(.01)
        assert supervisor is not None
        controller.kill()
        controller.communicate(timeout=3)
        status = Path(f'/proc/{supervisor}/stat')
        deadline = time.monotonic() + 3
        while status.exists() and status.read_text().rsplit(')', 1)[1].split()[0] != 'Z':
            assert time.monotonic() < deadline, 'orphan supervisor exceeded its deadline'
            time.sleep(.01)
    finally:
        if controller.poll() is None:
            controller.kill()
        controller.communicate(timeout=3)


@pytest.mark.parametrize('args,extra,input_text', [
    (('rev-parse', 'HEAD'), None, None),
    (('status', '--porcelain'), None, None),
    (('rev-parse', '--show-toplevel'), {'GIT_DIR': '/elsewhere'}, None),
    (('rev-parse', '--show-toplevel'), None, 'input'),
])
def test_unqualified_controller_git_stays_in_owned_lane(repository, monkeypatch, args, extra, input_text):
    from steward_harness.config.schema import UntrustedExecutionConfig
    from steward_harness.runtime.execution import UntrustedExecutionBroker

    broker = UntrustedExecutionBroker(UntrustedExecutionConfig(user='nobody'))
    calls = []

    def owned(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '', '')

    monkeypatch.setattr(broker, 'run', owned)
    broker.run_controller_git(args, cwd=repository, timeout=3, extra_env=extra, input_text=input_text)
    assert len(calls) == 1
    assert calls[0][1]['extra_env'] == extra
    assert calls[0][1]['input_text'] == input_text


def test_direct_lane_refuses_root_execution_identity(repository, monkeypatch):
    from steward_harness.config.schema import UntrustedExecutionConfig
    from steward_harness.runtime.execution import ExecutionBoundaryUnavailable, UntrustedExecutionBroker

    broker = UntrustedExecutionBroker(UntrustedExecutionConfig(user='nobody'))
    monkeypatch.setattr(broker, '_resolved_identity', lambda: ('unsafe-alias', 0, 0, '/root'))
    with pytest.raises(ExecutionBoundaryUnavailable, match='non-root'):
        broker.run_controller_git(('rev-parse', '--show-toplevel'), cwd=repository, timeout=3)


def test_cancellation_kills_supervisor_and_blocked_git(repository):
    import signal

    fifo = repository / 'blocked-config'
    os.mkfifo(fifo)
    with (repository / '.git/config').open('a') as config:
        config.write(f'\n[include]\npath = {fifo}\n')
    code = '''import sys
from pathlib import Path
from steward_harness.runtime.git_metadata import run_metadata
run_metadata(('rev-parse', '--show-toplevel'), cwd=sys.argv[1], timeout=30,
             home=Path(sys.argv[1]), identity={})
'''
    controller = subprocess.Popen([sys.executable, '-c', code, str(repository)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    descendants = []
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            children = Path(f'/proc/{controller.pid}/task/{controller.pid}/children').read_text().split()
            if children:
                supervisor = int(children[0])
                grandchildren = Path(f'/proc/{supervisor}/task/{supervisor}/children').read_text().split()
                if grandchildren:
                    descendants = [supervisor, int(grandchildren[0])]
                    break
            time.sleep(.01)
        assert len(descendants) == 2
        assert all(os.getpgid(pid) == descendants[0] for pid in descendants)
        controller.send_signal(signal.SIGINT)
        controller.communicate(timeout=3)
        for pid in descendants:
            status = Path(f'/proc/{pid}/stat')
            assert not status.exists() or status.read_text().rsplit(')', 1)[1].split()[0] == 'Z'
    finally:
        if controller.poll() is None:
            controller.kill()
        if descendants:
            try:
                os.killpg(descendants[0], signal.SIGKILL)
            except ProcessLookupError:
                pass
        controller.communicate(timeout=3)


@pytest.mark.parametrize('name', [b'non-utf8-\xff', b'carriage\rreturn'])
def test_native_repository_path_round_trips_without_decode_failure(repository, name):
    path = repository / os.fsdecode(name)
    subprocess.run(['git', 'init', str(path)], check=True, capture_output=True)
    result = run_metadata(('rev-parse', '--show-toplevel'), cwd=path,
                          timeout=3, home=repository, identity={})
    assert result.returncode == 0
    assert result.stdout.rstrip('\n') == str(path)


def test_relative_working_directory_is_rejected(repository):
    with pytest.raises(ValueError, match='absolute'):
        run_metadata(('rev-parse', '--show-toplevel'), cwd='relative',
                     timeout=3, home=repository, identity={})


def test_cleanup_failure_preserves_original_cancellation(repository, monkeypatch):
    import io

    class Process:
        pid = 123456789
        returncode = None
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        calls = 0
        killed = False

        def communicate(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt('cancel this query')
            raise subprocess.TimeoutExpired('cleanup', timeout)

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            self.returncode = -9
            return self.returncode

    process = Process()
    signals = []
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(os, 'killpg', lambda *args: signals.append(args))
    with pytest.raises(KeyboardInterrupt, match='cancel this query') as failure:
        run_metadata(('rev-parse', '--show-toplevel'), cwd=repository,
                     timeout=3, home=repository, identity={})
    assert signals and process.killed and process.returncode == -9
    assert any('cleanup failed' in note for note in failure.value.__notes__)
    assert process.stdout.closed and process.stderr.closed
