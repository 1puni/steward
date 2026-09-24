"""Exercise native Claude streaming/memory through GLM, without provider fallback."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from codex_probe import Connection


def send(c, text):
    message = {
        "type": "user",
        "uuid": str(uuid.uuid4()),
        "session_id": "",
        "parent_tool_use_id": None,
        "message": {"role": "user", "content": text},
    }
    c.write(json.dumps(message) + "\n")
    return message["uuid"]


def result(c):
    while True:
        event = c.next()
        if event.get("type") == "result":
            print(
                "result",
                {k: event.get(k) for k in ("subtype", "is_error", "result")},
                flush=True,
            )
            if event.get("is_error") or event.get("subtype") != "success":
                raise RuntimeError("provider failed")
            return event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interrupt-only", action="store_true")
    parser.add_argument("--correlation-only", action="store_true")
    parser.add_argument("--live-sources", action="store_true")
    parser.add_argument("--runtime-only", action="store_true")
    parser.add_argument("--memory-only", action="store_true")
    parser.add_argument(
        "--zaude-login-shell",
        action="store_true",
        help="Load ZAI_AUTH_TOKEN using Zaude's clean zsh login-shell procedure",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="steward-claude-live-") as temp:
        root = Path(temp)
        world = root / "world"
        world.mkdir()

        def git(*args):
            return subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgsign=false",
                    *args,
                ],
                cwd=world,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()

        git("init", "-q")
        git("config", "user.name", "Native session probe")
        git("config", "user.email", "probe@localhost")
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        native = world / "native/claude"
        if not (args.runtime_only or args.memory_only or args.live_sources):
            (native / "projects").mkdir(parents=True)
            (native / "memory").mkdir()
            (runtime / "projects").symlink_to(native / "projects", target_is_directory=True)
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            in {
                "HOME",
                "PATH",
                "TMPDIR",
                "LANG",
                "USER",
                "LOGNAME",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "HTTPS_PROXY",
                "HTTP_PROXY",
                "NO_PROXY",
            }
        }
        env["CLAUDE_CONFIG_DIR"] = str(runtime)
        credential = os.environ.get("ZAI_AUTH_TOKEN", "").strip()
        if args.zaude_login_shell:
            credential = subprocess.run(
                [
                    "/bin/sh",
                    "-c",
                    'env -u ZAI_AUTH_TOKEN /bin/zsh -lic \'printf "%s" "${ZAI_AUTH_TOKEN:-}" >&3\' 3>&1 >/dev/null 2>/dev/null',
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            ).stdout.strip()
        if (
            not credential
            or not credential.isascii()
            or any(ch.isspace() for ch in credential)
        ):
            raise RuntimeError("Zaude GLM credential unavailable")
        env["ANTHROPIC_AUTH_TOKEN"] = credential
        env["ANTHROPIC_BASE_URL"] = "https://api.z.ai/api/anthropic"
        env["ANTHROPIC_MODEL"] = "glm-5.3"
        if args.live_sources:
            from task_journey_probe import task_journey_probe
            git("commit", "--allow-empty", "-qm", "Initialize native live-source fixture")
            token = runtime / "fixture-token"
            token.write_text(credential)
            token.chmod(0o600)
            task_journey_probe(world, runtime, live_sources=True, family="glm", credential_path=token)
            return
        if args.memory_only:
            git("commit", "--allow-empty", "-qm", "Initialize memory fixture")
            memory_probe(world, runtime, credential)
            return
        if args.runtime_only:
            runtime_probe(world, runtime, credential)
            return
        sid = str(uuid.uuid4())
        command = [
            shutil.which("claude"),
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--replay-user-messages",
            "--include-partial-messages",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--settings",
            json.dumps(
                {
                    "autoMemoryEnabled": True,
                    "autoMemoryDirectory": str(native / "memory"),
                    "sandbox": {
                        "enabled": True,
                        "failIfUnavailable": True,
                        "allowUnsandboxedCommands": False,
                        "network": {"allowAllUnixSockets": True},
                    },
                }
            ),
            "--permission-mode",
            "acceptEdits",
            "--allowed-tools",
            "Bash,Read,Write,Edit",
            "--model",
            "glm-5.3",
            "--effort",
            "low",
            "--max-budget-usd",
            "1.00",
        ]
        c = Connection(env, world, command + ["--session-id", sid])
        try:
            if args.correlation_only:
                from claude_input_correlation_probe import correlation_probe
                correlation_probe(c, world)
                return
            if args.interrupt_only:
                interrupt_probe(c, world)
                return
            original = send(
                c,
                f"Bounded streaming-input test. Run a shell command that sleeps for 3 seconds, then writes ORIGINAL to {world}/marker.txt. Use this exact absolute path, not TMPDIR. No network or other files. Reply with the marker.",
            )
            correction = None
            results = []
            while True:
                event = c.next()
                if event.get("type") == "system" and event.get("subtype") == "init":
                    assert event["session_id"] == sid
                    print("session initialized", sid, flush=True)
                if event.get("type") == "user" and event.get("uuid") in {
                    original,
                    correction,
                }:
                    print("input echo", event["uuid"], flush=True)
                if event.get("type") == "assistant":
                    blocks = event.get("message", {}).get("content", [])
                    for block in blocks:
                        if (
                            block.get("type") == "tool_use"
                            and block.get("name") == "Bash"
                            and correction is None
                        ):
                            correction = send(
                                c,
                                f"Correction: the final contents of {world}/marker.txt must be CORRECTED, replacing ORIGINAL. Use this exact absolute path, not TMPDIR. Confirm CORRECTED in your final reply.",
                            )
                            print(
                                "correction sent during Bash tool use",
                                correction,
                                flush=True,
                            )
                if event.get("type") == "result":
                    print(
                        "result",
                        {k: event.get(k) for k in ("subtype", "is_error", "result")},
                        flush=True,
                    )
                    assert not event.get("is_error") and event["subtype"] == "success"
                    results.append(event)
                    if (
                        correction
                        and (world / "marker.txt").exists()
                        and (world / "marker.txt").read_text().strip() == "CORRECTED"
                    ):
                        break
                    if len(results) >= 2 or not (world / "marker.txt").exists():
                        raise AssertionError("correction not applied")
            print("correction applied; result count", len(results), flush=True)
            send(
                c,
                "Do not use tools. Give a one-line commit subject for the marker correction just completed.",
            )
            result(c)
            send(
                c,
                "For all future work in this disposable probe project, use CORRECTED as the verification marker. Save this project convention using your native auto memory so another session can find it. No network.",
            )
            result(c)
            assert any(
                "CORRECTED" in p.read_text() for p in (native / "memory").glob("*.md")
            )
            assert list((native / "projects").rglob("*.jsonl"))
            print("native memory and session records written into world", flush=True)
            git("add", "--all")
            git("commit", "-qm", "Checkpoint native memory and session evidence")
            assert git("show", "HEAD:marker.txt") == "CORRECTED"
            assert (
                "native/claude/memory/MEMORY.md"
                in git("ls-tree", "-r", "--name-only", "HEAD").splitlines()
            )
            print("native memory checkpoint", git("rev-parse", "HEAD"), flush=True)
            send(
                c,
                f"Keep marker.txt unchanged. Write {world}/continued.txt containing the corrected marker from our prior exchange. Use that exact absolute path. No network or other files.",
            )
            result(c)
            assert (world / "continued.txt").read_text().strip() == "CORRECTED"
            print("same-process continuation passed", flush=True)
            c.close()
            c = Connection(env, world, command + ["--resume", sid])
            send(
                c,
                f"Without reading files, write {world}/resumed.txt containing the corrected marker from our earlier exchange. Use that exact absolute path. No network or other files.",
            )
            result(c)
            assert (world / "resumed.txt").read_text().strip() == "CORRECTED"
            print("reconnect and explicit resume passed", flush=True)
            c.close()
            c = Connection(env, world, command + ["--session-id", str(uuid.uuid4())])
            send(
                c,
                f"Use the native project memory to find this project's verification marker. Write {world}/peer.txt containing only that marker. Use that exact absolute path. No network or other file changes.",
            )
            result(c)
            assert (world / "peer.txt").read_text().strip() == "CORRECTED"
            print("fresh-session shared memory read passed", flush=True)
        finally:
            c.close()
            print(
                "diagnostics",
                "".join(c.errors)[-1200:].replace(credential, "<redacted>"),
                flush=True,
            )


def memory_probe(world, native_home, credential):
    """Restore only accepted Git files, then recall memory with a fresh native home."""
    from steward_harness.cognition import Cognition, CognitionRequest
    from steward_harness.config.schema import UntrustedExecutionConfig
    from steward_harness.runtime.execution import UntrustedExecutionBroker
    from steward_harness.runtime.process import ProcessController
    from steward_harness.runtime.providers.claude import ClaudeRuntime
    from steward_harness.world.git_world import GitWorld

    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())

    def cognition(home):
        home.mkdir(exist_ok=True, mode=0o700)
        token = home / "fixture-token"
        token.write_text(credential)
        token.chmod(0o600)
        return Cognition({"glm": ClaudeRuntime(
            Path(shutil.which("claude")),
            base_url="https://api.z.ai/api/anthropic", credential_path=token,
            native_home=home, controller=ProcessController(broker), family="glm",
        )})

    marker = "MEMORY_" + uuid.uuid4().hex
    cognition(native_home).run(CognitionRequest(
        execution_id="native-memory-save", profile="balanced", provider_order=("glm",),
        prompt=(
            f"For this disposable project's future work, the verification marker is {marker}. "
            "Remember that project convention using your native auto memory, including its index. "
            "Write only memory files. Do not delegate or access the network."
        ), cwd=world, timeout_seconds=150, sandbox_mode="workspace-write",
    ))
    memory = world / "memories/glm"
    assert (memory / "MEMORY.md").is_file()
    assert any(marker in path.read_text() for path in memory.glob("*.md"))
    assert not (native_home / "projects").is_symlink()
    GitWorld(world, execution_broker=broker).finish(
        "turn_" + uuid.uuid4().hex,
        "Save native project convention", "Native memory recorded",
    )
    restored = world.parent / "restored-world"
    subprocess.run(["git", "clone", "--quiet", "--no-local", str(world), str(restored)], check=True)
    # Remove native originals from this memory-only recall proof. The marker
    # must be recalled from memories, not from a previous prompt in the records.
    shutil.rmtree(restored / "artefacts")
    assert not (restored / "native/claude").exists()
    assert not (restored / "native/glm/projects").exists()
    recalled = cognition(world.parent / "fresh-runtime").run(CognitionRequest(
        execution_id="native-memory-recall", profile="balanced", provider_order=("glm",),
        prompt=(
            "Use your native project memory to recall the verification marker for this project. "
            f"Write only that marker to {restored}/recalled.txt. Do not delegate or access the network."
        ), cwd=restored, timeout_seconds=150, sandbox_mode="workspace-write",
    ))
    assert (restored / "recalled.txt").read_text().strip() == marker
    print("Native GLM memory saved, Git-checkpointed, cloned and recalled with fresh home/session", recalled.provider_session_id, flush=True)


def runtime_probe(world, native_home, credential):
    """Use the shipped GLM adapter with its own native config and tool workers."""
    from steward_harness.cognition import Cognition, CognitionRequest
    from steward_harness.config.schema import UntrustedExecutionConfig
    from steward_harness.runtime.execution import UntrustedExecutionBroker
    from steward_harness.runtime.process import ProcessController
    from steward_harness.runtime.providers.claude import ClaudeRuntime

    credential_path = native_home / "fixture-token"
    credential_path.write_text(credential)
    credential_path.chmod(0o600)

    class ObservedRuntime(ClaudeRuntime):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.task_ids = set()

        def _execute(self, request, session_id, environment, *, resume=None):
            original_run = self._controller.run

            def observed_run(command, *, on_stdout_line, **kwargs):
                def observe(line):
                    event = json.loads(line)
                    if event.get("subtype") == "task_started":
                        self.task_ids.add(event["task_id"])
                        print("native runtime child started", event["task_id"], flush=True)
                    on_stdout_line(line)
                return original_run(command, on_stdout_line=observe, **kwargs)

            self._controller.run = observed_run
            try:
                return super()._execute(request, session_id, environment, resume=resume)
            finally:
                self._controller.run = original_run

    runtime = ObservedRuntime(
        Path(shutil.which("claude")),
        base_url="https://api.z.ai/api/anthropic", credential_path=credential_path,
        native_home=native_home,
        controller=ProcessController(UntrustedExecutionBroker(UntrustedExecutionConfig())),
        family="glm",
    )
    cognition = Cognition({"glm": runtime})
    first = cognition.run(CognitionRequest(
        execution_id="glm-native-runtime", profile="balanced", provider_order=("glm",),
        on_input_ready=lambda _send: None,
        prompt=(
            f"In this disposable workspace {world}, delegate to two native agents. "
            f"Ask one to write ALPHA to {world}/alpha.txt and the other to write BETA "
            f"to {world}/beta.txt. Each should use tools directly and not delegate further. "
            f"Wait for both, read their files, then write ALPHA,BETA to {world}/combined.txt."
        ), cwd=world, timeout_seconds=150, sandbox_mode="workspace-write",
    ))
    for name, value in (("alpha", "ALPHA"), ("beta", "BETA"), ("combined", "ALPHA,BETA")):
        assert (world / f"{name}.txt").read_text().strip() == value
    assert len(runtime.task_ids) == 2, runtime.task_ids
    print("GLM production native config and shared-write artifacts verified", first.provider_session_id, flush=True)
    second = cognition.run(CognitionRequest(
        execution_id="glm-native-resume", profile="balanced", provider_order=("glm",),
        on_input_ready=lambda _send: None,
        provider_session_id=first.provider_session_id, session_provider="glm",
        prompt=f"Without reading files, write the combined marker from our earlier exchange to {world}/resumed.txt.",
        cwd=world, timeout_seconds=150, sandbox_mode="workspace-write",
    ))
    assert second.provider_session_id == first.provider_session_id
    assert (world / "resumed.txt").read_text().strip() == "ALPHA,BETA"
    print("GLM production native explicit resume verified", flush=True)


def interrupt_probe(c, world):
    started = world / "started.txt"
    finished = world / "finished.txt"
    send(
        c,
        f"Run exactly one foreground shell command: echo $$ > {started}; sleep 30; echo FINISHED > {finished}. Do not background it or use other tools.",
    )
    while not started.exists():
        if time.monotonic() >= c.deadline:
            raise TimeoutError("native command never created its PID marker")
        time.sleep(0.05)
    pid = int(started.read_text().strip())
    c.fixture_pids.add(pid)
    os.kill(pid, 0)
    request_id = str(uuid.uuid4())
    c.write(
        json.dumps(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {"subtype": "interrupt"},
            }
        )
        + "\n"
    )
    acknowledged = terminal = False
    while not (acknowledged and terminal):
        event = c.next()
        if (
            event.get("type") == "control_response"
            and event.get("response", {}).get("request_id") == request_id
        ):
            assert event["response"]["subtype"] == "success", event
            acknowledged = True
            print("native interrupt acknowledgement", event, flush=True)
        if event.get("type") == "result":
            terminal = True
            print(
                "native interrupted result",
                {key: event.get(key) for key in ("subtype", "is_error", "result")},
                flush=True,
            )
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
    print("native terminal result and stopped child verified", flush=True)
    send(
        c,
        "The previous command was intentionally interrupted. Do not repeat it or use tools. Reply RECOVERED.",
    )
    result(c)
    assert not finished.exists()
    print("same-session continuation after native interruption passed", flush=True)


if __name__ == "__main__":
    main()
