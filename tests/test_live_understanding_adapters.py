"""The live understanding handoff over each built-in adapter's real wire protocol.

``test_live_understanding`` proves the controller decision with an in-process
adapter. Here the real ``ClaudeRuntime`` (stream-json) and
``CodexAppServerRuntime`` (JSON-RPC ``turn/steer``) drive a fake native parent
executable. The parent keeps a real child process working, offers its account
with real ``git hash-object``/``git update-ref``, and receives the controller's
acknowledgement through the adapter's live-input channel. The runner's per-turn
offer watcher observes the ref; the test also plays the daemon's note flush.
"""

from __future__ import annotations

import json
import os
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
from steward_harness.state import StateDatabase, TaskSpec, TaskStatus
from steward_harness.task_runner import TaskRunner
from steward_harness.task_store import PREFIX
from test_runtime_lifecycle import _controller
from test_task_runner_kernel import _git, _repository, publish_task, task_status

SESSION = "22222222-2222-4222-8222-222222222222"
CHILD_THREAD = "33333333-3333-4333-8333-333333333333"
ACCOUNT = "## Understanding\n\nThe consumer is src/feed.py; a child is still drafting draft.py."

# The native parent: protocol-neutral work shared by both fakes. A reader
# thread owns stdin and answers the wire protocol; the main thread works.
_PARENT = r'''
import json, os, pathlib, re, subprocess, sys, threading, time
SIGNALS = pathlib.Path(SIGNALS_DIR)
out_lock = threading.Lock()
started, acknowledged = threading.Event(), threading.Event()
state = {"prompt": None, "messages": []}

def emit(event):
    with out_lock:
        print(json.dumps(event), flush=True)

def signal(name, value=""):
    tmp = SIGNALS / (name + ".tmp")
    tmp.write_text(value)
    tmp.rename(SIGNALS / name)

def wait_for(name):
    while not (SIGNALS / name).exists():
        time.sleep(0.02)

def git(*args, stdin=None):
    return subprocess.run(["git", *args], check=True, text=True, input=stdin,
                          capture_output=True).stdout.strip()

def delivered(attributed):
    """A live input is attribution text plus one JSON source record."""
    record = json.loads(attributed.split("\n", 1)[1])
    state["messages"].append(record)
    signal("messages.json", json.dumps(state["messages"]))
    acknowledged.set()

def work():
    started.wait()
    prompt = state["prompt"]
    base = re.search(r"it is ([0-9a-f]{40})", prompt).group(1)
    ref = re.search(r"git update-ref (refs/steward/understanding/\S+)", prompt).group(1)
    task_file = pathlib.Path("tasks") / (ref.rsplit("/", 1)[1] + ".md")
    # A delegated child keeps drafting a product file and a heartbeat.
    child = subprocess.Popen([sys.executable, "-c",
        "import os, pathlib, sys, time\n"
        "while True:\n"
        "    pathlib.Path('draft.py').write_text(f'# half written {time.time()}\\n')\n"
        "    pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {time.time()}')\n"
        "    time.sleep(0.02)\n", str(SIGNALS / "heartbeat")],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while not pathlib.Path("draft.py").exists():
        time.sleep(0.01)
    signal("ready", json.dumps({"cwd": os.getcwd(), "child": child.pid, "base": base, "ref": ref}))
    wait_for("go")
    assert not task_file.exists(), "the accepted account must not have a product-side copy"
    blob = git("hash-object", "-w", "--stdin", stdin=f"Steward-Base: {base}\n\n{ACCOUNT}\n")
    git("update-ref", ref, blob)
    signal("offered", blob)
    acknowledged.wait()
    # The same blob again, after the ref briefly disappeared: no new request.
    git("update-ref", "-d", ref)
    git("update-ref", ref, blob)
    signal("repointed", blob)
    wait_for("release")
    child.terminate(); child.wait()
    pathlib.Path("draft.py").unlink()  # half-written drafts never reach closure
    pathlib.Path("result.txt").write_text("consumer: src/feed.py\n")
    signal("child-stopped")
    return ("Traced the consumer to src/feed.py.\n"
            "COMMIT: docs: record the feed consumer\nDISPOSITION: idle\nQUESTION: NONE")
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


def native_parent(tmp_path: Path, provider: str):
    signals = tmp_path / "signals"
    signals.mkdir()
    executable = tmp_path / f"fake-{provider}"
    executable.write_text(
        f"#!{sys.executable}\n"
        f"SIGNALS_DIR = {str(signals)!r}\nSESSION = {SESSION!r}\n"
        f"CHILD_THREAD = {CHILD_THREAD!r}\nACCOUNT = {ACCOUNT!r}\n"
        + _PARENT + (_CLAUDE if provider == "claude" else _CODEX)
    )
    executable.chmod(0o755)
    native_home = tmp_path / "native"
    native_home.mkdir()
    runtime = {"claude": ClaudeRuntime, "codex": CodexAppServerRuntime}[provider]
    return runtime(executable, controller=_controller(), native_home=native_home), signals


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
        provider_fallbacks=(), timeout_seconds=30, poll_seconds=0.05,
    )
    return state, runner, task.task_id, bare


def tip(state, task_id) -> str:
    return state.tasks.refs()[PREFIX + str(task_id)]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_native_parent_offers_understanding_over_real_adapter_while_child_works(tmp_path, provider):
    adapter, signals = native_parent(tmp_path, provider)
    state, runner, task_id, bare = make_runner(tmp_path, provider, adapter)
    opened = tip(state, task_id)
    errors: list[BaseException] = []

    def execute():
        try:
            runner.prepare(task_id)
        except BaseException as error:  # surfaced on the test thread
            errors.append(error)

    worker = threading.Thread(target=execute, daemon=True)
    worker.start()

    def daemon_until(name: str, *, timeout: float = 15) -> str:
        """Play the daemon: flush live inputs until the native parent signals."""
        deadline = time.monotonic() + timeout
        while not (signals / name).exists():
            assert not errors, errors
            assert worker.is_alive(), f"execution ended before {name!r}"
            assert time.monotonic() < deadline, f"native parent never signalled {name!r}"
            runner.flush_inputs()
            time.sleep(0.02)
        return (signals / name).read_text()

    child = None
    try:
        ready = json.loads(daemon_until("ready"))
        cwd, child = Path(ready["cwd"]), ready["child"]
        assert ready["base"] == opened
        assert ready["ref"] == f"refs/steward/understanding/{task_id}"
        head, index = _git("rev-parse", "HEAD", cwd=cwd), _git("write-tree", cwd=cwd)
        (signals / "go").touch()

        blob = daemon_until("offered")
        daemon_until("messages.json")
        accepted = tip(state, task_id)
        assert accepted != opened

        # The acknowledgement crossed the adapter's live channel as controller words.
        [ack] = json.loads((signals / "messages.json").read_text())
        assert ack == {
            "source_id": f"understanding-{blob}-0", "origin": "controller", "author": "steward",
            "text": f"Steward accepted understanding offer {blob} as accepted revision "
                    f"{accepted}. Use Steward-Base: {accepted} for your next offer."}

        # While the child still works: the account is durable and nothing else moved.
        assert "src/feed.py" in StateDatabase(state.path).tasks.get(task_id).brief
        assert task_status(runner, task_id) is TaskStatus.RUNNING
        assert alive(child)
        beat = (signals / "heartbeat").read_text()
        deadline = time.monotonic() + 5
        while (signals / "heartbeat").read_text() == beat:
            assert time.monotonic() < deadline, "child stopped beating after acceptance"
            time.sleep(0.02)
        assert _git("rev-parse", "HEAD", cwd=cwd) == head
        assert _git("write-tree", cwd=cwd) == index
        assert _git("status", "--porcelain", "--", "draft.py", cwd=cwd) == "?? draft.py"
        definition = state.tasks.read(task_id)[1]
        assert definition.work is None
        assert state.tasks.git("ls-tree", "--name-only", accepted) == "task.md"

        # Re-pointing the ref at the same blob is not a second request.
        daemon_until("repointed")
        for _ in range(3):
            runner.flush_inputs()
        assert tip(state, task_id) == accepted
        assert len(json.loads((signals / "messages.json").read_text())) == 1

        (signals / "release").touch()
        worker.join(30)
        assert not worker.is_alive(), "native execution did not finish after release"
    finally:
        (signals / "go").touch()
        (signals / "release").touch()
        worker.join(10)
        if child is not None and alive(child):
            os.kill(child, 9)
    assert not errors, errors
    assert not alive(child)

    history = state.tasks.git("log", "--first-parent", "--format=%s", tip(state, task_id)).splitlines()
    assert history.count("accept understanding") == 1
    body = state.tasks.get(task_id).brief
    assert body.startswith(ACCOUNT)
    assert "Concurrent task understanding" not in body
    definition = state.tasks.read(task_id)[1]
    assert definition.hold is None and definition.resume is None
    assert task_status(runner, task_id) is not TaskStatus.BLOCKED
    consumed = state.tasks.git("log", "-1", "--format=%(trailers:key=Steward-Consumed,valueonly)",
                               tip(state, task_id))
    assert "understanding-" not in consumed

    # Ordinary publication: the finished product lands, the dropped draft does not.
    assert publish_task(runner) == task_id
    assert task_status(runner, task_id) is TaskStatus.DONE
    assert _git("show", "main:result.txt", cwd=bare) == "consumer: src/feed.py"
    assert "draft.py" not in _git("ls-tree", "--name-only", "main", cwd=bare).splitlines()
    assert not _git("ls-tree", "--name-only", "main", "--", f"tasks/{task_id}.md", cwd=bare)
