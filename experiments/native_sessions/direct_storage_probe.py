"""Prove native writes and Git-only transcript resume with private runtime homes.

Uses provider quota. No production configuration or runtime is changed.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from codex_probe import Connection, completed
from glm_probe import result, send

from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.world.git_world import GitWorld


def mapped_home(root, world, provider, auth_file):
    """Each live world owns its own mapping; never retarget a shared home."""
    root.mkdir(mode=0o700)
    if provider == "codex":
        shutil.copyfile(auth_file, root / "auth.json")
        (root / "auth.json").chmod(0o600)
        (root / "config.toml").write_text(
            'model = "gpt-5.6-sol"\napproval_policy = "never"\nsandbox_mode = "workspace-write"\n'
            '[features]\napps = false\n'
        )
        mappings = {"sessions": world / "artefacts/codex/sessions",
                    "memories": world / "memories/codex"}
    else:
        mappings = {"projects": world / "artefacts/glm/projects"}
    for name, target in mappings.items():
        target.mkdir(parents=True, exist_ok=True)
        (root / name).symlink_to(target, target_is_directory=True)
    return root


def connect(home, world, provider, credential):
    environment = {name: value for name, value in os.environ.items() if name in {
        "HOME", "PATH", "TMPDIR", "LANG", "USER", "LOGNAME", "SSL_CERT_FILE",
        "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
    }}
    if provider == "codex":
        environment["CODEX_HOME"] = str(home)
        connection = Connection(environment, world)
        connection.call("initialize", {"clientInfo": {"name": "direct_storage_probe", "version": "1"},
                                       "capabilities": {"experimentalApi": True}})
        connection.send("initialized", {}, request=False)
        return connection
    environment.update(CLAUDE_CONFIG_DIR=str(home), ANTHROPIC_AUTH_TOKEN=credential,
                       ANTHROPIC_BASE_URL="https://api.z.ai/api/anthropic")
    return environment


def claude_command(world, resume=None):
    command = [shutil.which("claude"), "-p", "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose", "--replay-user-messages",
               "--setting-sources", "", "--strict-mcp-config", "--settings",
               json.dumps({"autoMemoryDirectory": str(world / "memories/glm"),
                           "autoMemoryEnabled": True,
                           "sandbox": {"enabled": True, "failIfUnavailable": True,
                                       "allowUnsandboxedCommands": False,
                                       "network": {"allowAllUnixSockets": True}}}),
               "--permission-mode", "acceptEdits", "--allowed-tools", "Bash,Read,Write,Edit",
               "--model", "glm-5.3", "--effort", "low", "--max-budget-usd", "1.00"]
    if resume:
        command.extend(["--resume", str(resume)])
    return command


def stop(connection, provider, session):
    if provider == "codex":
        connection.call("thread/backgroundTerminals/clean", {"threadId": session})
    connection.input.close()
    connection.thread.join(10)
    if connection.thread.is_alive():
        connection.close()
        raise RuntimeError("native process did not exit after EOF")
    if connection.failure:
        raise connection.failure


def git(world, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                          cwd=world, capture_output=True, text=True, check=True).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("codex", "glm"))
    parser.add_argument("--auth-file", type=Path, default=Path.home() / ".codex/auth.json")
    parser.add_argument("--login-shell", action="store_true")
    args = parser.parse_args()
    credential = ""
    if args.provider == "glm":
        credential = os.environ.get("ZAI_AUTH_TOKEN", "").strip()
        if args.login_shell:
            credential = subprocess.run(
                ["/bin/sh", "-c", 'env -u ZAI_AUTH_TOKEN /bin/zsh -lic \'printf "%s" "${ZAI_AUTH_TOKEN:-}" >&3\' 3>&1 >/dev/null 2>/dev/null'],
                capture_output=True, text=True, check=True, timeout=15,
            ).stdout.strip()
        if not credential or not credential.isascii() or any(c.isspace() for c in credential):
            raise RuntimeError("GLM fixture credential unavailable")
    with tempfile.TemporaryDirectory(prefix="steward-direct-native-") as directory:
        root = Path(directory).resolve()
        world = root / "world"
        world.mkdir()
        git(world, "init", "-q")
        git(world, "config", "user.name", "Native storage probe")
        git(world, "config", "user.email", "probe@localhost")
        git(world, "commit", "--allow-empty", "-qm", "Initialize direct storage fixture")
        home = mapped_home(root / "runtime", world, args.provider, args.auth_file)
        connection = connect(home, world, args.provider, credential)
        session = None
        try:
            marker = "DIRECT_" + uuid.uuid4().hex
            started, finished = world / "started", world / "finished"
            shell = f"echo started > {shlex.quote(str(started))}; sleep 8; echo finished > {shlex.quote(str(finished))}"
            prompt = (f"Remember this session marker: {marker}. Run exactly this foreground shell command: {shell}. "
                      "Do not delegate, background it, or use the network. Then reply with the marker.")
            if args.provider == "codex":
                session = connection.call("thread/start", {"cwd": str(world), "approvalPolicy": "never",
                                                           "sandbox": "workspace-write"})["thread"]["id"]
                turn = connection.call("turn/start", {"threadId": session,
                                                      "input": [{"type": "text", "text": prompt}]})["turn"]["id"]
            else:
                connection = Connection(connection, world, claude_command(world))
                send(connection, prompt)
            while not started.exists():
                if time.monotonic() >= connection.deadline or connection.failure:
                    raise RuntimeError("native shell never reached storage observation")
                time.sleep(0.05)
            originals = list((world / "artefacts" / args.provider).rglob("*.jsonl"))
            matching = [path for path in originals if marker in path.read_text()]
            assert len(matching) == 1, "native transcript must be visible during execution"
            original = matching[0]
            assert not finished.exists(), "observation must precede tool completion"
            prefix = original.read_bytes()
            print(args.provider, "original transcript visible while native shell is active", flush=True)
            if args.provider == "codex":
                completed(connection, 0, session, turn)
            else:
                session = result(connection)["session_id"]
            stop(connection, args.provider, session)
            assert original.read_bytes().startswith(prefix)
            assert original.stat().st_size > len(prefix)
        finally:
            if isinstance(connection, Connection):
                connection.close()
        relative = original.relative_to(world)
        broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
        GitWorld(world, execution_broker=broker).finish(
            "turn_" + uuid.uuid4().hex, "Checkpoint original native transcript", "Native append observed",
        )
        restored = root / "restored-world"
        git(root, "clone", "--quiet", "--no-local", str(world), str(restored))
        assert (restored / relative).read_bytes() == original.read_bytes()
        old_bytes = original.read_bytes()
        # Fresh private runtime: restore credentials, but no prior DB, cache or index.
        new_home = mapped_home(root / "fresh-runtime", restored, args.provider, args.auth_file)
        assert not list(new_home.glob("*.sqlite*"))
        if args.provider == "codex":
            connection = connect(new_home, restored, args.provider, credential)
        else:
            connection = Connection(connect(new_home, restored, args.provider, credential), restored,
                                    claude_command(restored, restored / relative))
        try:
            prompt = "Without tools, repeat only the session marker I gave you earlier."
            if args.provider == "codex":
                resumed = connection.call("thread/resume", {"threadId": session, "path": str(restored / relative),
                                                            "cwd": str(restored), "approvalPolicy": "never",
                                                            "sandbox": "workspace-write"})["thread"]["id"]
                assert resumed == session
                turn = connection.call("turn/start", {"threadId": session,
                                                      "input": [{"type": "text", "text": prompt}]})["turn"]["id"]
                completed(connection, 0, session, turn)
                replies = [event["params"]["item"].get("text", "") for event in connection.events
                           if event.get("method") == "item/completed"
                           and event.get("params", {}).get("threadId") == session
                           and event["params"].get("turnId") == turn
                           and event["params"]["item"].get("type") == "agentMessage"]
                assert any(marker in reply for reply in replies)
            else:
                send(connection, prompt)
                reply = result(connection)
                assert reply["session_id"] == session and marker in reply["result"]
            stop(connection, args.provider, session)
        finally:
            connection.close()
        assert original.read_bytes() == old_bytes, "fresh mapping cannot redirect the old world's writer"
        assert credential.encode() not in (restored / relative).read_bytes() if credential else True
        print(args.provider, "Git-only original transcript restored and resumed with fresh private runtime", session, flush=True)


if __name__ == "__main__":
    main()
