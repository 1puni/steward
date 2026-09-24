"""Real systemd acceptance: success, cancellation and controller death own trees.

Run under a root systemd service on a disposable Linux host. This deliberately
skips on ordinary laptops; root Linux without a systemd manager fails explicitly.
"""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import signal
import subprocess
import sys
import time

import pytest

from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.ownership import prerequisites


@pytest.fixture
def broker(tmp_path):
    if sys.platform != 'linux' or os.geteuid() != 0:
        pytest.skip('requires root Linux systemd ownership acceptance')
    prerequisites()  # Fail, not skip, when a claimed Linux acceptance lacks ownership.
    account = pwd.getpwnam('nobody')
    os.chmod(tmp_path, 0o777)
    for path in tmp_path.parents:
        if path == Path('/tmp'):
            break
        os.chmod(path, path.stat().st_mode | 0o111)
    return UntrustedExecutionBroker(UntrustedExecutionConfig(user=account.pw_name)), tmp_path


def _wait(path):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            value = path.read_text()
        except FileNotFoundError:
            value = ''
        if value:
            return int(value)
        time.sleep(.05)
    raise AssertionError(f'{path} was not written')


def _gone(pid):
    try:
        status = Path(f'/proc/{pid}/stat').read_text()
    except (FileNotFoundError, ProcessLookupError):
        return True
    return status.rsplit(')', 1)[1].split()[0] == 'Z'


@pytest.mark.parametrize('cancel', ['success', 'cancel', 'wrapper-death'])
def test_owned_descendant_cannot_escape_and_peer_survives(broker, cancel):
    execution, root = broker
    peer = execution.popen_command([sys.executable, '-c', 'import time; time.sleep(100)'], cwd='/')
    child_file = root / 'child'
    code = '''import os, signal, sys, time
pid = os.fork()
if pid == 0:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open(sys.argv[1], 'w').write(str(os.getpid()))
    for fd in (0, 1, 2):
        os.close(fd)
    time.sleep(100)
else:
    while not os.path.exists(sys.argv[1]):
        time.sleep(.01)
    if sys.argv[2] != 'success':
        time.sleep(100)
'''
    process = execution.popen_command([sys.executable, '-c', code, str(child_file), str(cancel)], cwd='/', stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        child = _wait(child_file)
        if cancel == 'cancel':
            execution.signal_process(process, signal.SIGTERM)
        elif cancel == 'wrapper-death':
            os.kill(process.pid, signal.SIGKILL)
        process.communicate(timeout=15)
        assert _gone(child), 'escaped child survived invocation completion'
        assert peer.poll() is None, 'another invocation was killed'
    finally:
        execution.signal_process(process, signal.SIGTERM)
        execution.signal_process(peer, signal.SIGTERM)
        peer.wait(timeout=15)


@pytest.mark.parametrize("controller_mode", ["service", "standalone"])
def test_controller_death_cleans_owned_descendants(broker, controller_mode):
    execution, root = broker
    import uuid
    owner = 'steward-test-owner-' + uuid.uuid4().hex + '.service'
    child_file = root / 'orphan'
    child_code = """import os,time
if os.fork() == 0:
    os.setsid()
    open(%r, 'w').write(str(os.getpid()))
time.sleep(100)
""" % str(child_file)
    controller_code = '''import sys,time
from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.runtime.execution import UntrustedExecutionBroker
broker = UntrustedExecutionBroker(UntrustedExecutionConfig(user='nobody'))
broker.popen_command([sys.executable, '-c', sys.argv[1]], cwd='/')
time.sleep(100)
'''
    peer = execution.popen_command([sys.executable, '-c', 'import time; time.sleep(100)'], cwd='/')
    command = [sys.executable, '-c', controller_code, child_code]
    if controller_mode == 'service':
        command = ['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--unit=' + owner,
                   '-p', 'Environment=PYTHONPATH=' + os.environ.get('PYTHONPATH', ''), *command]
    controller = subprocess.Popen(command)
    try:
        child = _wait(child_file)
        if controller_mode == 'service':
            subprocess.run(['systemctl', 'kill', '--kill-whom=main', '--signal=KILL', owner], check=True)
        else:
            controller.kill()
        controller.wait(timeout=15)
        deadline = time.monotonic() + 10
        while not _gone(child):
            assert time.monotonic() < deadline, 'child survived controller death'
            time.sleep(.05)
        assert peer.poll() is None
    finally:
        if controller_mode == 'service':
            subprocess.run(['systemctl', 'stop', owner], capture_output=True)
        elif controller.poll() is None:
            controller.kill()
            controller.wait(timeout=15)
        execution.signal_process(peer, signal.SIGTERM)
        peer.wait(timeout=15)


@pytest.mark.parametrize("controller_pid", [-1, os.getpid()], ids=["missing", "expired"])
def test_standalone_launcher_rejects_expired_controller_before_execution(tmp_path, controller_pid):
    import json
    from steward_harness.runtime import ownership_launcher
    scratch = tmp_path / 'private'
    scratch.mkdir()
    payload = scratch / 'invocation.json'
    marker = tmp_path / 'must-not-run'
    payload.write_text(json.dumps(dict(
        argv=[sys.executable, '-c', 'from pathlib import Path; Path(%r).touch()' % str(marker)],
        cwd=str(tmp_path), env={}, uid=os.getuid(), gid=os.getgid(),
        controller_pid=controller_pid, controller_start='expired',
    )))
    result = subprocess.run([sys.executable, '-I', '-S', ownership_launcher.__file__, str(payload)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'ModuleNotFoundError' not in result.stderr
    assert not marker.exists()
    assert not scratch.exists()


def test_owned_launch_preserves_literal_provider_environment_and_drops_groups(broker):
    import json
    execution, root = broker
    credential = 'literal $HOME %n "quote"\nnext line'
    process = execution.popen(
        [sys.executable, '-c', 'import json,os; print(json.dumps([os.getuid(), os.getgroups(), os.getcwd(), os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("CONTROLLER_SECRET")]))'],
        cwd=root, env={'ANTHROPIC_API_KEY': credential, 'CONTROLLER_SECRET': 'must-not-inherit'},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 0, stderr
    assert json.loads(stdout) == [pwd.getpwnam('nobody').pw_uid, [], str(root), credential, None]


@pytest.mark.parametrize('text', [False, True])
def test_owned_communicate_preserves_large_input_with_slow_reader(broker, text):
    """A wrapper liveness check must not abandon partially written stdin."""
    execution, root = broker
    payload = 'abc\n' * 75_000 if text else b'abc\n' * 75_000
    code = '''import sys,time
time.sleep(.3)
data = sys.stdin.buffer.read()
sys.stdout.buffer.write(data[:100000])
sys.stdout.buffer.flush()
time.sleep(.15)
sys.stdout.buffer.write(data[100000:])
'''
    result = execution.run([sys.executable, '-c', code], cwd='/', timeout=5,
                           input_text=payload, text=text)
    assert result.returncode == 0, result.stderr
    assert result.stdout == payload
    assert result.stderr == ('' if text else b'')


def test_owned_communicate_keeps_original_deadline_with_pending_input(broker):
    execution, root = broker
    ready = root / 'reader-ready'
    process = execution.popen_command(
        [sys.executable, '-c', "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)", str(ready)],
        cwd='/', stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        _wait(ready)
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            process.communicate(b'x' * 300_000, timeout=.3)
        assert raised.value.timeout == .3
        assert time.monotonic() - started < 2, 'wrapper monitoring extended the declared deadline'
    finally:
        execution.signal_process(process, signal.SIGTERM)
        process.communicate(timeout=15)


def test_service_stop_allows_controller_to_drain_native_turn(broker):
    """SIGTERM reaches the controller before its still-live native turn ends."""
    import uuid
    _execution, root = broker
    owner = 'steward-test-drain-' + uuid.uuid4().hex + '.service'
    ready = root / 'ready'
    checkpoint = root / 'checkpoint'
    native_code = '''import os,sys,time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
if sys.stdin.buffer.readline() == b'checkpoint\\n':
    time.sleep(.3)
    Path(sys.argv[2]).write_text('native turn drained')
'''
    controller_code = '''import signal,subprocess,sys,threading,time
from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.runtime.execution import UntrustedExecutionBroker
stopping = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stopping.set())
broker = UntrustedExecutionBroker(UntrustedExecutionConfig(user='nobody'))
process = broker.popen_command([sys.executable, '-c', *sys.argv[1:]], cwd='/',
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
assert stopping.wait(10)
time.sleep(.2)
out, err = process.communicate(b'checkpoint\\n', timeout=5)
assert process.returncode == 0, err
'''
    controller = subprocess.Popen([
        'systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--unit=' + owner,
        '-p', 'KillMode=mixed', '-p', 'TimeoutStopSec=10s',
        '-p', 'Environment=PYTHONPATH=' + os.environ.get('PYTHONPATH', ''),
        sys.executable, '-c', controller_code, native_code, str(ready), str(checkpoint),
    ])
    try:
        child = _wait(ready)
        subprocess.run(['systemctl', 'stop', owner], check=True, timeout=15)
        assert controller.wait(timeout=5) == 0
        assert checkpoint.read_text() == 'native turn drained'
        assert _gone(child)
    finally:
        subprocess.run(['systemctl', 'stop', owner], capture_output=True, timeout=15)
