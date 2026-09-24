"""Live native Codex probe, isolated from the production runtime.

Uses provider quota. See README.md for the tested scope and remaining limits.
"""

import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.runtime.contracts import RuntimeExecutionError
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.process import ProcessController


class Connection:
    def __init__(self, env, cwd, command=None):
        self.q = queue.Queue()
        self.events = []
        self.errors = []
        self.counter = 0
        self.fixture_pids = set()
        self.deadline = time.monotonic() + 150
        self.input = None
        self.failure = None
        self.stop = None
        ready = threading.Event()
        policy = UntrustedExecutionConfig()
        broker = UntrustedExecutionBroker(
            policy.model_copy(
                update={
                    "inherited_environment": (
                        *policy.inherited_environment,
                        "CODEX_HOME",
                        "CLAUDE_CONFIG_DIR",
                    ),
                }
            )
        )
        self.controller = ProcessController(broker)

        def output(line):
            self.q.put(json.loads(line))

        def input_ready(writer):
            self.input = writer
            ready.set()

        def run():
            try:
                result = self.controller.run(
                    command or [shutil.which("codex"), "app-server", "--stdio"],
                    cwd=cwd,
                    env=env,
                    timeout_seconds=150,
                    on_stdout_line=output,
                    on_input_ready=input_ready,
                    on_started=self._adopt,
                )
                self.errors.append(result.stderr)
            except BaseException as error:  # noqa: BLE001 - propagate across the reader thread
                self.failure = error
            finally:
                ready.set()
                self.q.put({"eof": True})

        self.thread = threading.Thread(target=run)
        self.thread.start()
        if not ready.wait(5) or self.input is None:
            self.close()
            raise RuntimeError("provider input unavailable") from self.failure

    def _adopt(self, stop):
        self.stop = stop

    def write(self, text):
        self.input.write(text)

    def send(self, method, params, request=True):
        event = {"method": method, "params": params}
        if request:
            self.counter += 1
            event["id"] = self.counter
        self.write(json.dumps(event) + "\n")
        return event.get("id")

    def next(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("probe deadline")
        try:
            event = self.q.get(timeout=remaining)
        except queue.Empty as error:
            raise TimeoutError(
                "native probe exceeded its connection deadline"
            ) from error
        self.events.append(event)
        if event.get("eof"):
            raise RuntimeError(
                "provider EOF: " + "".join(self.errors)[-1200:]
            ) from self.failure
        if "method" in event and "id" in event:
            # Fail closed on interactive authority requests.
            self.write(
                json.dumps(
                    {
                        "id": event["id"],
                        "error": {
                            "code": -32601,
                            "message": "probe does not grant additional authority",
                        },
                    }
                )
                + "\n"
            )
        return event

    def call(self, method, params):
        request = self.send(method, params)
        while True:
            event = self.next()
            if event.get("id") == request and "method" not in event:
                if "error" in event:
                    raise RuntimeError(method + ": " + json.dumps(event["error"]))
                return event["result"]

    def close(self):
        # Fixture-specific cleanup: Codex can retain a separate tool process group.
        for pid in self.fixture_pids:
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.stop is not None:
            self.stop()
        self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError("native probe process did not stop")


def completed(c, start, thread_id, turn_id):
    while True:
        for event in c.events[start:]:
            if (
                event.get("method") == "turn/completed"
                and event["params"].get("threadId") == thread_id
                and event["params"]["turn"]["id"] == turn_id
            ):
                turn = event["params"]["turn"]
                if turn["status"] != "completed":
                    raise RuntimeError("turn failed: " + json.dumps(turn))
                return turn
        start = len(c.events)
        c.next()


def followup(c, tid, prompt):
    start = len(c.events)
    turn = c.call("turn/start", {"threadId": tid, "input": [{"type": "text", "text": prompt}]})
    return completed(c, start, tid, turn["turn"]["id"])


def main():
    parser = argparse.ArgumentParser(
        description="Bounded live Codex native-session experiment; uses provider authentication and model quota."
    )
    parser.add_argument(
        "--auth-file", type=Path, default=Path.home() / ".codex/auth.json"
    )
    parser.add_argument("--interrupt-only", action="store_true")
    parser.add_argument("--interrupt-cleanup", action="store_true")
    parser.add_argument("--disconnect-only", action="store_true")
    parser.add_argument("--fanout-only", action="store_true")
    parser.add_argument("--fanout-write", action="store_true")
    parser.add_argument("--runtime-only", action="store_true")
    parser.add_argument("--task-only", action="store_true")
    parser.add_argument("--live-sources", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="steward-codex-live-") as temp:
        root = Path(temp)
        home = root / "provider"
        world = root / "world"
        home.mkdir(mode=0o700)
        world.mkdir()
        shutil.copyfile(args.auth_file, home / "auth.json")
        (home / "auth.json").chmod(0o600)
        if args.fanout_only or args.fanout_write:
            agents = home / "agents"
            agents.mkdir()
            for name, token in (
                ("child_alpha", "CHILD_ALPHA"),
                ("child_beta", "CHILD_BETA"),
            ):
                (agents / f"{name}.toml").write_text(
                    f'name = "{name}"\n'
                    f'description = "Return the native fanout token {token}."\n'
                    + (
                        f'developer_instructions = "Search the inputs directory for the {name} marker. '
                        f'Write its value to {name}.txt in the shared working directory. '
                        'Use native tools; do not delegate further. Return your evidence."\n'
                        'sandbox_mode = "workspace-write"\n'
                        if args.fanout_write
                        else f'developer_instructions = "Do not use tools or write files. Return exactly {token}."\n'
                        'sandbox_mode = "read-only"\n'
                    )
                )
        (home / "config.toml").write_text(
            'model = "gpt-5.6-sol"\napproval_policy = "never"\nsandbox_mode = "workspace-write"\n[features]\ngoals = false\napps = false\n'
            + (
                "[agents]\nenabled = true\nmax_concurrent_threads_per_session = 2\n"
                if args.fanout_only or args.fanout_write
                else ""
            )
        )
        if not (args.runtime_only or args.task_only or args.live_sources):
            for name in ("sessions", "memories"):
                target = world / "native" / "codex" / name
                target.mkdir(parents=True)
                (home / name).symlink_to(target, target_is_directory=True)

        def git(*argv):
            return subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgsign=false",
                    *argv,
                ],
                cwd=world,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()

        git("init", "-q")
        git("config", "user.name", "Native session probe")
        git("config", "user.email", "probe@localhost")
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            in {
                "HOME",
                "PATH",
                "TMPDIR",
                "LANG",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "HTTPS_PROXY",
                "HTTP_PROXY",
                "NO_PROXY",
            }
        }
        env["CODEX_HOME"] = str(home)
        if args.runtime_only or args.task_only or args.live_sources:
            git("commit", "--allow-empty", "-qm", "Initialize native runtime fixture")
            if args.task_only or args.live_sources:
                from task_journey_probe import task_journey_probe
                task_journey_probe(world, home, live_sources=args.live_sources)
                return
            runtime_probe(world, home, interrupt=args.interrupt_only, fanout=args.fanout_write)
            return
        c = Connection(env, world)
        try:
            print(
                "initialize",
                c.call(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "steward_native_probe",
                            "version": "0.1.0",
                        },
                        "capabilities": {"experimentalApi": args.interrupt_cleanup},
                    },
                ),
                flush=True,
            )
            c.send("initialized", {}, request=False)
            thread = c.call(
                "thread/start",
                {
                    "cwd": str(world),
                    "approvalPolicy": "never",
                    "sandbox": "workspace-write",
                    "model": "gpt-5.6-sol",
                },
            )
            tid = thread["thread"]["id"]
            print("thread started", tid, flush=True)
            if args.interrupt_only or args.interrupt_cleanup:
                interrupt_probe(c, tid, world, cleanup=args.interrupt_cleanup)
                return
            if args.disconnect_only:
                disconnect_probe(c, tid, world, env)
                return
            if args.fanout_only or args.fanout_write:
                fanout_probe(c, tid, world, git, write=args.fanout_write)
                return
            cursor = len(c.events)
            turn = c.call(
                "turn/start",
                {
                    "threadId": tid,
                    "input": [
                        {
                            "type": "text",
                            "text": "Bounded transport test. Use a shell tool to sleep for 3 seconds, then write ORIGINAL to marker.txt in the working directory. No network, no other files. Reply with the marker.",
                        }
                    ],
                },
            )
            turnid = turn["turn"]["id"]
            print("turn started", turnid, flush=True)
            steered = False
            while True:
                if cursor == len(c.events):
                    c.next()
                event = c.events[cursor]
                cursor += 1
                method = event.get("method")
                if method in ("item/started", "item/completed"):
                    print(
                        method,
                        event.get("params", {}).get("item", {}).get("type"),
                        flush=True,
                    )
                if (
                    method == "item/started"
                    and event["params"]["item"]["type"] == "commandExecution"
                    and not steered
                ):
                    print(
                        "steer",
                        c.call(
                            "turn/steer",
                            {
                                "threadId": tid,
                                "expectedTurnId": turnid,
                                "input": [
                                    {
                                        "type": "text",
                                        "text": "Correction: the final contents of marker.txt must be CORRECTED, replacing ORIGINAL. Confirm CORRECTED in the final reply.",
                                    }
                                ],
                            },
                        ),
                        flush=True,
                    )
                    steered = True
                if (
                    method == "turn/completed"
                    and event["params"].get("threadId") == tid
                    and event["params"]["turn"]["id"] == turnid
                ):
                    print("completed", json.dumps(event["params"]["turn"]), flush=True)
                    break
            print(
                "steering exercised",
                steered,
                "marker",
                (world / "marker.txt").read_text()
                if (world / "marker.txt").exists()
                else None,
                flush=True,
            )
            assert steered and (world / "marker.txt").read_text().strip() == "CORRECTED"
            # A stale expected turn must fail rather than silently create new work.
            try:
                c.call(
                    "turn/steer",
                    {
                        "threadId": tid,
                        "expectedTurnId": turnid,
                        "input": [{"type": "text", "text": "Late correction."}],
                    },
                )
            except RuntimeError as error:
                assert "no active turn to steer" in str(error), str(error)
                print("late steer rejected", str(error), flush=True)
            else:
                raise AssertionError("late steer unexpectedly accepted")
            print(
                "reflection",
                json.dumps(
                    followup(
                        c,
                        tid,
                        "Do not use tools. Give a one-line commit subject for the actual marker correction just completed.",
                    )
                ),
                flush=True,
            )
            git("add", "--all")
            git(
                "commit",
                "-qm",
                "Checkpoint corrected marker and native session evidence",
            )
            sha = git("rev-parse", "HEAD")
            print(
                "checkpoint", sha, "marker", git("show", "HEAD:marker.txt"), flush=True
            )
            files = list((world / "native/codex/sessions").rglob("*.jsonl"))
            assert files and any("CORRECTED" in f.read_text() for f in files)
            print("native session records present in Git world", len(files), flush=True)
            followup(
                c,
                tid,
                "Keep marker.txt unchanged. Write continued.txt containing the corrected marker from our prior exchange. No network or other files.",
            )
            assert (world / "continued.txt").read_text().strip() == "CORRECTED"
            print("same-connection continuation passed", flush=True)
            c.close()
            c = Connection(env, world)
            c.call(
                "initialize",
                {"clientInfo": {"name": "steward_native_probe", "version": "0.1.0"}},
            )
            c.send("initialized", {}, request=False)
            resumed = c.call(
                "thread/resume",
                {
                    "threadId": tid,
                    "cwd": str(world),
                    "approvalPolicy": "never",
                    "sandbox": "workspace-write",
                },
            )
            assert resumed["thread"]["id"] == tid
            followup(
                c,
                tid,
                "Without reading files, write resumed.txt containing the corrected marker from our earlier exchange. No network or other files.",
            )
            assert (world / "resumed.txt").read_text().strip() == "CORRECTED"
            print("reconnect and explicit resume passed", flush=True)
            evidence = c.call("thread/read", {"threadId": tid, "includeTurns": True})
            print(
                "native history turn count",
                len(evidence["thread"]["turns"]),
                flush=True,
            )
        finally:
            c.close()
            print("diagnostics", "".join(c.errors)[-1600:], flush=True)


def interrupt_probe(c, tid, world, *, cleanup=False):
    """Observe a native terminal state and the actual child lifetime separately."""
    started = world / "started.txt"
    finished = world / "finished.txt"
    cursor = len(c.events)
    turn = c.call(
        "turn/start",
        {
            "threadId": tid,
            "input": [
                {
                    "type": "text",
                    "text": (
                        f"Run exactly one foreground shell command: echo $$ > {started}; "
                        f"sleep 30; echo FINISHED > {finished}. Do not background it or use other tools."
                    ),
                }
            ],
        },
    )["turn"]["id"]
    while not started.exists():
        if time.monotonic() >= c.deadline:
            raise TimeoutError("native command never created its PID marker")
        time.sleep(0.05)
    pid = int(started.read_text().strip())
    c.fixture_pids.add(pid)
    os.kill(pid, 0)
    if cleanup:
        print("native terminals before interrupt", c.call("thread/backgroundTerminals/list", {"threadId": tid}), flush=True)
    acknowledgement = c.call("turn/interrupt", {"threadId": tid, "turnId": turn})
    print("interrupt acknowledgement", acknowledgement, flush=True)
    while True:
        if cursor == len(c.events):
            c.next()
        event = c.events[cursor]
        cursor += 1
        if (
            event.get("method") == "turn/completed"
            and event["params"].get("threadId") == tid
            and event["params"]["turn"]["id"] == turn
        ):
            status = event["params"]["turn"]["status"]
            assert status == "interrupted", status
            break
    if cleanup:
        print("native terminals after interrupt", c.call("thread/backgroundTerminals/list", {"threadId": tid}), flush=True)
        print("native terminal cleanup", c.call("thread/backgroundTerminals/clean", {"threadId": tid}), flush=True)
    deadline = time.monotonic() + 3
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        if time.monotonic() >= deadline:
            raise AssertionError(
                "native terminal event preceded a surviving child: "
                + subprocess.run(
                    ["ps", "-p", str(pid), "-o", "pid=,ppid=,pgid=,state=,command="],
                    check=False,
                    capture_output=True,
                    text=True,
                ).stdout
            )
        time.sleep(0.05)
    assert not finished.exists()
    print("native interrupted state and stopped child verified", flush=True)
    followup(
        c,
        tid,
        "The previous command was intentionally interrupted. Do not repeat it or use tools. Reply RECOVERED.",
    )
    assert not finished.exists()
    print("same-session continuation after native interruption passed", flush=True)


def fanout_probe(c, tid, world, git, *, write=False):
    """Exercise native delegation; fixture counts and tools are not product policy."""
    markers = {name: str(uuid.uuid4()) for name in ("child_alpha", "child_beta")}
    expected = "CHILD_ALPHA,CHILD_BETA"
    if write:
        inputs = world / "inputs"
        inputs.mkdir()
        for name, marker in markers.items():
            (inputs / f"{uuid.uuid4()}.txt").write_text(f"{name}={marker}\n")
        expected = ",".join(markers.values())
    prompt = (
        "Native fanout transport test. Spawn the two configured native subagents "
        "child_alpha and child_beta in parallel, wait for both, and aggregate their "
        "results yourself. Do not do either child's work in the parent. "
        + (
            "Each child must search inputs/ for its named marker and write its value "
            "to its own child_alpha.txt or child_beta.txt in this shared working directory. "
            "Give each the exact working directory path. Once they finish, read their "
            "two artifacts and write fanout.txt containing alpha's value, a comma, "
            "and beta's value, on one line. Use normal native file and shell tools. "
            if write
            else "After both results arrive, write fanout.txt containing exactly "
            "CHILD_ALPHA,CHILD_BETA on one line. "
        )
        + "All work belongs to this disposable workspace. Reply with the result."
    )
    start = len(c.events)
    turn = c.call(
        "turn/start",
        {
            "threadId": tid,
            "input": [
                {
                    "type": "text",
                    "text": prompt,
                }
            ],
        },
    )
    completed(c, start, tid, turn["turn"]["id"])

    events = c.events[start:]
    calls = [
        event["params"]["item"]
        for event in events
        if event.get("method") == "item/completed"
        and event.get("params", {}).get("item", {}).get("type")
        == "collabAgentToolCall"
        and event["params"]["item"].get("tool") == "spawnAgent"
    ]
    spawned_ids = {
        child
        for call in calls
        for child in call.get("receiverThreadIds", [])
    }
    activity = [
        event["params"]["item"]
        for event in events
        if event.get("params", {}).get("item", {}).get("type")
        == "subAgentActivity"
    ]
    completed_ids = {
        item["agentThreadId"] for item in activity if item.get("kind") == "completed"
    }
    print(
        "fanout event types",
        [
            event.get("params", {}).get("item", {}).get("type")
            for event in events
            if event.get("params", {}).get("item", {})
        ],
        flush=True,
    )
    print("fanout activity", activity, flush=True)
    child_ids = spawned_ids or {item["agentThreadId"] for item in activity}
    assert len(child_ids) == 2, {"calls": calls, "activity": activity}
    assert child_ids <= completed_ids, activity
    assert (world / "fanout.txt").read_text().strip() == expected
    if write:
        for name, marker in markers.items():
            assert (world / f"{name}.txt").read_text().strip() == marker
        for child in child_ids:
            history = c.call("thread/read", {"threadId": child, "includeTurns": True})
            items = [item for turn in history["thread"]["turns"] for item in turn["items"]]
            assert any(item["type"] in {"commandExecution", "fileChange"} for item in items)
        print("child tools, shared-workspace writes and parent aggregation verified", flush=True)
    print("native fanout spawned and completed", sorted(child_ids), flush=True)

    # The checkpoint is parent-owned: child IDs remain provider evidence, not
    # harness task identities or independently publishable candidates.
    git("add", "--all")
    git("commit", "-qm", "Checkpoint native parent fanout evidence")
    sha = git("rev-parse", "HEAD")
    assert git("show", "HEAD:fanout.txt") == expected
    evidence = c.call("thread/read", {"threadId": tid, "includeTurns": True})
    assert all(child in json.dumps(evidence) for child in child_ids)
    print("parent fanout checkpoint", sha, flush=True)


def runtime_probe(world, native_home, *, interrupt=False, fanout=False):
    """Exercise the shipped adapter, including live input and explicit resume."""
    from steward_harness.cognition import Cognition, CognitionRequest
    from steward_harness.runtime.contracts import RuntimeInput
    from steward_harness.runtime.providers.codex_app_server import CodexAppServerRuntime

    children = set()

    class ObservedController(ProcessController):
        def run(self, command, *, on_stdout_line, **kwargs):
            trace = []

            def observe(line):
                message = json.loads(line)
                params = message.get("params", {})
                item = params.get("item", {})
                if item.get("type") == "subAgentActivity" and item.get("kind") == "started":
                    children.add(item["agentThreadId"])
                if message.get("method") in {"item/started", "item/completed", "turn/completed"} or "error" in message:
                    trace.append({
                        "method": message.get("method"), "thread": params.get("threadId"),
                        "type": item.get("type"), "kind": item.get("kind"),
                        "status": params.get("turn", {}).get("status"),
                        "text": item.get("text", "")[-500:], "error": message.get("error"),
                    })
                on_stdout_line(line)

            try:
                return super().run(command, on_stdout_line=observe, **kwargs)
            except BaseException:
                print("production fixture recent native events", trace[-30:], flush=True)
                raise

    runtime = CodexAppServerRuntime(
        Path(shutil.which("codex")), native_home=native_home,
        controller=ObservedController(UntrustedExecutionBroker(UntrustedExecutionConfig())),
    )
    cognition = Cognition({"codex": runtime})
    if interrupt:
        started, finished = world / "started.txt", world / "finished.txt"
        session = None

        def cancel_once_running():
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline and not started.exists():
                time.sleep(0.1)
            cognition.cancel("native-runtime-cancel")

        threading.Thread(target=cancel_once_running, daemon=True).start()
        try:
            try:
                cognition.run(CognitionRequest(
                    execution_id="native-runtime-cancel", profile="fast", provider_order=("codex",),
                    prompt=(f"Run exactly one foreground shell command: echo $$ > {started}; "
                            f"sleep 30; echo FINISHED > {finished}. Do not background it or use other tools."),
                    cwd=world, timeout_seconds=150, sandbox_mode="workspace-write",
                ))
            except RuntimeExecutionError as error:
                assert "cancelled" in str(error), error
                session = error.session_id
            else:
                raise AssertionError("cancelled execution reported success")
            assert started.exists() and session is not None
            pid = int(started.read_text().strip())
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("native shell survived production cancellation")
            assert not finished.exists()
            cognition.run(CognitionRequest(
                execution_id="native-runtime-after-cancel", profile="fast", provider_order=("codex",),
                provider_session_id=session, session_provider="codex",
                prompt="The command was intentionally cancelled. Do not repeat it. Write RECOVERED to recovered.txt.",
                cwd=world, timeout_seconds=150, sandbox_mode="workspace-write",
            ))
            assert (world / "recovered.txt").read_text().strip() == "RECOVERED"
            assert not finished.exists()
            print("Production native cancellation stopped fixture shell and resumed the same session", session, flush=True)
        finally:
            if started.exists():
                pid = int(started.read_text().strip())
                try:
                    if os.getpgid(pid) == pid:
                        os.killpg(pid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        return
    if fanout:
        inputs = world / "inputs"
        inputs.mkdir()
        for name in ("child_alpha", "child_beta"):
            (inputs / f"{uuid.uuid4().hex}.txt").write_text(f"{name}={name.upper()}\n")
        cognition.run(CognitionRequest(
            execution_id="native-runtime-fanout", profile="fast", provider_order=("codex",),
            prompt=("Delegate to child_alpha and child_beta using their native agent configurations. "
                    "Wait for both to search and write their files, then read both and write "
                    "CHILD_ALPHA,CHILD_BETA to combined.txt. Do not do their assignments yourself."),
            cwd=world, timeout_seconds=150, sandbox_mode="workspace-write",
        ))
        for name in ("child_alpha", "child_beta"):
            assert (world / f"{name}.txt").read_text().strip() == name.upper()
        assert len(children) == 2, children
        assert (world / "combined.txt").read_text().strip() == "CHILD_ALPHA,CHILD_BETA"
        records = list((world / "artefacts/codex/sessions").rglob("*.jsonl"))
        assert len(records) == 3
        assert all(any(child in path.name for path in records) for child in children)
        print("Production native fanout returned artifacts and parent/child records after native terminal cleanup", flush=True)
        return
    receipts = []
    senders = []
    failures = []
    stopped = threading.Event()

    def ready(steer):
        def send_correction():
            try:
                while not (world / "started.txt").exists():
                    if stopped.wait(0.05):
                        return
                steer(RuntimeInput("fixture-correction", "Correction: marker.txt must contain CORRECTED when you finish."))
            except BaseException as error:  # noqa: BLE001 - return sender-thread failures to the probe
                failures.append(error)
        sender = threading.Thread(target=send_correction)
        senders.append(sender)
        sender.start()

    try:
        result = cognition.run(CognitionRequest(
            execution_id="native-runtime-probe", profile="fast", provider_order=("codex",),
            prompt="Run a shell command: echo STARTED > started.txt; sleep 3; echo ORIGINAL > marker.txt. Then report the marker.",
            cwd=world, timeout_seconds=150, sandbox_mode="workspace-write",
            on_input_ready=ready, on_input_result=receipts.append,
        ))
    finally:
        stopped.set()
        for sender in senders:
            sender.join(5)
            assert not sender.is_alive()
    assert not failures, failures
    assert [(r.source_id, r.disposition) for r in receipts] == [("fixture-correction", "accepted")], receipts
    assert (world / "marker.txt").read_text().strip() == "CORRECTED"
    print("production adapter active steering and artifact verified", result.provider_session_id, flush=True)
    resumed = cognition.run(CognitionRequest(
        execution_id="native-runtime-resume", profile="fast", provider_order=("codex",),
        provider_session_id=result.provider_session_id, session_provider="codex",
        prompt="Without reading files, write resumed.txt with the corrected marker from our prior exchange.",
        cwd=world, timeout_seconds=150, sandbox_mode="workspace-write",
    ))
    assert resumed.provider_session_id == result.provider_session_id
    assert (world / "resumed.txt").read_text().strip() == "CORRECTED"
    records = list((world / "artefacts/codex/sessions").rglob(f"*{resumed.provider_session_id}.jsonl"))
    assert len(records) == 1
    snapshot = records[0]
    record = snapshot.read_text()
    assert "CORRECTED" in snapshot.read_text()
    assert not (native_home / "sessions").is_symlink()
    from steward_harness.world.git_world import GitWorld
    GitWorld(world, execution_broker=runtime._controller.broker).finish(
        "turn_" + uuid.uuid4().hex, "Native runtime verification", "Recorded native continuity",
    )
    saved = subprocess.run(
        ["git", "show", f"HEAD:{snapshot.relative_to(world)}"],
        cwd=world, capture_output=True, text=True, check=True,
    )
    assert saved.stdout == record
    assert not (world / "auth.json").exists()
    print("production adapter explicit resume and Git-checkpointed original native transcript verified", flush=True)


def disconnect_probe(c, tid, world, env):
    started = world / "started.txt"
    turn = c.call(
        "turn/start",
        {
            "threadId": tid,
            "input": [
                {
                    "type": "text",
                    "text": (
                        f"Run one foreground shell command: echo $$ > {started}; sleep 30. "
                        "Do not background it or use other tools."
                    ),
                }
            ],
        },
    )["turn"]["id"]
    while not started.exists():
        if time.monotonic() >= c.deadline:
            raise TimeoutError("native command never created its PID marker")
        time.sleep(0.05)
    c.fixture_pids.add(int(started.read_text().strip()))
    source_id = str(uuid.uuid4())
    marker = f"SYNTHETIC_RECEIPT_{source_id}"
    c.send(
        "turn/steer",
        {
            "threadId": tid,
            "expectedTurnId": turn,
            "clientUserMessageId": source_id,
            "input": [
                {
                    "type": "text",
                    "text": f"Record this synthetic input: {marker}. Do not use any new tools.",
                }
            ],
        },
    )
    # Inspect the transport buffer only to distinguish queued from written.
    # Do not consume the response queue: acknowledgement processing is the
    # deliberate crash boundary in this fixture.
    while True:
        with c.input._lock:
            assert not c.input._finished, "transport ended before flush was observed"
            pending = bool(c.input._pending)
        if not pending:
            break
        if time.monotonic() >= c.deadline:
            raise TimeoutError("input did not leave the local buffer")
        time.sleep(0.005)
    assert c.failure is None, c.failure
    c.close()
    resumed = Connection(env, world)
    try:
        resumed.call(
            "initialize",
            {"clientInfo": {"name": "steward_native_probe", "version": "0.1.0"}},
        )
        resumed.send("initialized", {}, request=False)
        resumed.call(
            "thread/resume",
            {
                "threadId": tid,
                "cwd": str(world),
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
            },
        )
        history = resumed.call("thread/read", {"threadId": tid, "includeTurns": True})
        present = marker in json.dumps(history)
        print(
            "source ID", source_id, "native history contains input", present, flush=True
        )
        print(
            "delivery evidence",
            "recorded in native history"
            if present
            else "unresolved; absence does not prove non-acceptance",
            flush=True,
        )
        print("No input was resubmitted and no completion was inferred", flush=True)
    finally:
        resumed.close()


if __name__ == "__main__":
    main()
