"""Native provider processes save accounts through the shared synchronous task tool."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from state_fixtures import admit_task
from steward_harness.cognition import Cognition
from steward_harness.config.schema import RepositoryConfig, UntrustedExecutionConfig
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.providers.claude import ClaudeRuntime
from steward_harness.runtime.providers.codex_app_server import CodexAppServerRuntime
from steward_harness.state import StateDatabase, TaskSpec
from steward_harness.task_runner import TaskRunner
from test_runtime_lifecycle import _controller
from test_task_runner_kernel import _repository

SESSION = "22222222-2222-4222-8222-222222222222"
CHILD_THREAD = "33333333-3333-4333-8333-333333333333"
ACCOUNT = "The consumer is src/feed.py; delegated work is still running."
_PARENT = r'''
import json, pathlib, re, subprocess, sys, threading, time
out_lock = threading.Lock()
started = threading.Event()
state = {}
def emit(event):
    with out_lock:
        print(json.dumps(event), flush=True)
def delivered(_):
    pass

def work():
    started.wait()
    if '--mcp-config' in sys.argv:
        config = json.loads(sys.argv[sys.argv.index('--mcp-config') + 1])['mcpServers']['steward_tasks']
    else:
        config = {}
        for arg in sys.argv:
            if arg.startswith('mcp_servers.steward_tasks.'):
                key, value = arg.split('=', 1)
                config[key.rsplit('.', 1)[1]] = json.loads(value)
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    client = subprocess.Popen([config['command'], *config['args']], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    def rpc(method, params):
        client.stdin.write(json.dumps(dict(jsonrpc='2.0', id=1, method=method, params=params)) + '\n')
        client.stdin.flush()
        return json.loads(client.stdout.readline())['result']
    try:
        rpc('initialize', {'protocolVersion': '2024-11-05'})
        result = rpc('tools/call', dict(name='task', arguments=dict(operation='understanding', base=re.search(r'has base `([0-9a-f]{40})`', state['prompt']).group(1), key='native-account', body=ACCOUNT)))
        assert not result['isError'], result
        receipt = json.loads(result['content'][0]['text'])
        assert receipt['accepted'], receipt
        assert child.poll() is None
        pathlib.Path(READY).write_text(json.dumps(receipt))
        while not pathlib.Path(RELEASE).exists():
            time.sleep(.02)
    finally:
        client.stdin.close()
        client.wait(timeout=5)
        child.terminate()
        child.wait(timeout=5)
    return 'The account is durable.'
'''

_CLAUDE = r'''
def reader():
    root = None
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("type") != "user":
            continue
        command = message["uuid"]
        if root is None:
            root = state["root"] = command
            state["prompt"] = message["message"]["content"]
            emit({"type": "system", "subtype": "init", "session_id": SESSION, "model": "claude-fake"})
        # --replay-user-messages echoes each accepted user line.
        emit({"type": "user", "session_id": SESSION, "uuid": command, "message": message["message"]})
        if command == root:
            lifecycle(command, "queued", "started")
            started.set()
        else:
            # Folded into the running turn: queued, started, completed before its result.
            lifecycle(command, "queued", "started", "completed")
            delivered(message["message"]["content"])

def lifecycle(command, *states):
    for s in states:
        emit({"type": "command_lifecycle", "session_id": SESSION, "command_uuid": command, "state": s})

thread = threading.Thread(target=reader)
thread.start()
final = work()
emit({"type": "result", "subtype": "success", "terminal_reason": "completed", "session_id": SESSION,
      "user_message_uuid": state["root"], "result": final, "usage": {}})
lifecycle(state["root"], "completed")
thread.join()  # the adapter closes stdin once every command completed
'''

_CODEX = r'''
TURN = "parent-turn"

def reader():
    for line in sys.stdin:
        message = json.loads(line)
        method, ident, params = message.get("method"), message.get("id"), message.get("params", {})
        if method == "initialize":
            emit({"id": ident, "result": {}})
        elif method == "thread/start":
            emit({"id": ident, "result": {"thread": {"id": SESSION}, "model": "codex-fake"}})
        elif method == "turn/start":
            state["prompt"] = params["input"][0]["text"]
            emit({"id": ident, "result": {"turn": {"id": TURN}}})
            activity("started")
            started.set()
        elif method == "turn/steer":
            assert params["expectedTurnId"] == TURN and params["threadId"] == SESSION
            emit({"id": ident, "result": {"turnId": TURN}})
            delivered(params["input"][0]["text"])
        elif method == "thread/backgroundTerminals/clean":
            emit({"id": ident, "result": {}})

def activity(kind):
    emit({"method": "item/completed", "params": {"threadId": SESSION, "turnId": TURN,
          "item": {"type": "subAgentActivity", "kind": kind, "agentThreadId": CHILD_THREAD}}})

thread = threading.Thread(target=reader)
thread.start()
final = work()
activity("completed")  # no native child is left open at turn/completed
emit({"method": "item/completed", "params": {"threadId": SESSION, "turnId": TURN,
      "item": {"type": "agentMessage", "text": final}}})
emit({"method": "turn/completed", "params": {"threadId": SESSION,
      "turn": {"id": TURN, "status": "completed"}}})
thread.join()  # the adapter closes stdin once terminal cleanup is acknowledged
'''


def make_runner(tmp_path: Path, provider: str, adapter):
    bare, clone = _repository(tmp_path)
    state = StateDatabase(tmp_path / "state.db")
    task = admit_task(state, TaskSpec("app", "Understand", "Find the consumer."), provider=provider)
    runner = TaskRunner(
        state=state,
        repositories={"app": RepositoryConfig(path=str(clone), remote_url=str(bare))},
        transports={"app": ControllerGitTransport(
            tmp_path / "controller.db", "app", str(bare), "main", allow_local=True)},
        worktrees_root=tmp_path / "worktrees",
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
        cognition=Cognition({provider: adapter}),
        provider_fallbacks=(), timeout_seconds=30,
    )
    return state, runner, task.task_id, bare



@pytest.mark.parametrize("provider", ["claude", "codex", "glm"])
def test_native_account_is_visible_before_provider_completes(tmp_path, provider):
    ready, release = tmp_path / "ready", tmp_path / "release"
    executable = tmp_path / "native-parent"
    executable.write_text(f"#!{sys.executable}\nSESSION={SESSION!r}\nCHILD_THREAD={CHILD_THREAD!r}\n"
                          f"ACCOUNT={ACCOUNT!r}\nREADY={str(ready)!r}\nRELEASE={str(release)!r}\n"
                          + _PARENT + (_CODEX if provider == "codex" else _CLAUDE))
    executable.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    adapter = (CodexAppServerRuntime(executable, controller=_controller(), native_home=home)
               if provider == "codex" else
               ClaudeRuntime(executable, controller=_controller(), native_home=home, family=provider))
    state, runner, task_id, _ = make_runner(tmp_path, provider, adapter)
    errors = []
    def run():
        try:
            runner.prepare(task_id)
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not ready.exists() and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists(), (errors, state.tasks.get(task_id))
        assert thread.is_alive()
        fresh = StateDatabase(state.path)
        assert fresh.tasks.get(task_id).brief == ACCOUNT
        assert fresh.tasks.read(task_id)[1].work is None
        assert json.loads(ready.read_text())["accepted"]
    finally:
        release.write_text("finish")
        thread.join(timeout=15)
    assert not thread.is_alive() and not errors
    assert state.tasks.get(task_id).brief == ACCOUNT
