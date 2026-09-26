"""Anonymous cognition gets explicit knowledge, private lineage and no ambient tools."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tomllib
from dataclasses import replace

import pytest

from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.runtime.contracts import ReadScope, RuntimeExecutionError, RuntimeRequest, resolve_model
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.providers.codex_read_scope import (
    PROFILE, prepare_scope, scope_config, verify_scope_config,
)


def scoped_request(tmp_path, identity="visitor-a", roots=()):
    return RuntimeRequest(execution_id="turn", resolved=resolve_model("codex", "fast"),
                          provider_session_id=None, prompt="Read public knowledge", cwd=tmp_path,
                          timeout_seconds=30, read_scope=ReadScope(identity, roots))


def test_private_native_homes_resume_without_inheriting_operator_data(tmp_path):
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    native = tmp_path / "native"
    native.mkdir()
    (native / "auth.json").write_text('{"synthetic": "fixture credential"}')
    (native / "AGENTS.md").write_text("private operator instruction")
    (native / "config.toml").write_text('[mcp_servers.operator]\ncommand="private-tool"\n')
    (native / "history.jsonl").write_text("private operator history")
    public = tmp_path / "public.json"
    public.write_text('{"knowledge":true}')
    request = scoped_request(tmp_path, roots=(public,))
    a, home_a = prepare_scope(broker, request, native)
    b, home_b = prepare_scope(broker, replace(request, read_scope=ReadScope("visitor-b", (public,))), native)
    assert home_a != home_b and a.cwd != b.cwd
    assert list(a.cwd.iterdir()) == list(b.cwd.iterdir()) == []
    assert not any((home_a / name).exists() for name in ("AGENTS.md", "history.jsonl", "skills", "plugins"))
    assert (home_a / "auth.json").is_symlink()
    config = tomllib.loads((home_a / "config.toml").read_text())
    verify_scope_config(config, scope_config((public,), a.cwd))
    (home_a / "sessions").mkdir()
    (home_a / "sessions" / "own-history").write_text("own transcript")
    again, resumed = prepare_scope(broker, replace(request, provider_session_id="own-session"), native)
    assert resumed == home_a and again.provider_session_id == "own-session"
    assert (resumed / "sessions" / "own-history").read_text() == "own transcript"
    assert not (home_b / "sessions").exists()
    for root in (native, tmp_path, native / "auth.json"):
        with pytest.raises(RuntimeExecutionError, match="prepare"):
            prepare_scope(broker, replace(request, read_scope=ReadScope("bad", (root,))), native)


@pytest.mark.parametrize("alter", [
    lambda c: c["permissions"][PROFILE]["filesystem"].update({"/": "read"}),
    lambda c: c["permissions"][PROFILE].update(extends=":workspace"),
    lambda c: c.update(mcp_servers={"privileged": {"command": "tool"}}),
    lambda c: c["features"].update(plugins=True),
    lambda c: c["features"].update(code_mode_host=False),
    lambda c: c["features"].update(code_mode=False),
    lambda c: c.update(approvals_reviewer="auto_review"),
    lambda c: c.update(developer_instructions="private operator knowledge"),
    lambda c: c["shell_environment_policy"]["set"].update(SECRET="fixture"),
    lambda c: c["shell_environment_policy"].update(experimental_use_profile=True),
])
def test_native_config_must_confirm_scope_without_ambient_authority(tmp_path, alter):
    expected = scope_config((tmp_path,), tmp_path / "empty")
    actual = json.loads(json.dumps(expected))
    alter(actual)
    with pytest.raises(RuntimeExecutionError):
        verify_scope_config(actual, expected)


def test_provider_without_scoped_reads_cannot_be_used_as_fallback(tmp_path):
    from test_cognition import FakeAdapter
    from steward_harness.cognition import Cognition, CognitionRequest
    from steward_harness.runtime.contracts import RuntimeUnavailable

    adapter = FakeAdapter("claude")
    cognition = Cognition({"claude": adapter})
    with pytest.raises(RuntimeUnavailable, match="scoped read isolation"):
        cognition.run(CognitionRequest(execution_id="public", profile="fast", prompt="hi",
                                       cwd=tmp_path, timeout_seconds=10,
                                       provider_order=("claude",), read_scope=ReadScope("visitor")))
    assert adapter.requests == []


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("codex"),
                    reason="requires installed Codex Linux sandbox")
def test_real_codex_sandbox_effects(tmp_path):
    """No model call: actual native sandbox executes hostile commands over fixtures."""
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    native = tmp_path / "native"
    native.mkdir()
    (native / "auth.json").write_text("synthetic credential")
    public = tmp_path / "public"
    public.mkdir()
    (public / "knowledge").write_text("public fact")
    private = tmp_path / "other-visitor"
    private.write_text("private history")
    (public / "escape").symlink_to(private)
    request, home = prepare_scope(broker, scoped_request(tmp_path, roots=(public,)), native)
    probe = r'''
import os, pathlib, socket, sys
public, private, home, parent, port = sys.argv[1:]
assert pathlib.Path(public, 'knowledge').read_text() == 'public fact'
for path in (private, public+'/escape', home+'/auth.json', '/proc/'+parent+'/environ'):
    try:
        pathlib.Path(path).read_bytes()
    except OSError:
        pass
    else:
        raise AssertionError('private path was readable: '+path)
try:
    pathlib.Path(public, 'mutated').write_text('attack')
except OSError:
    pass
else:
    raise AssertionError('public root was writable')
try:
    s = socket.socket()
    s.settimeout(1)
    s.connect(('127.0.0.1', int(port)))
except OSError:
    pass
else:
    raise AssertionError('sandbox permitted network syscall')
print('public read permitted; private reads, write and network denied')
'''
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    result = subprocess.run(
        [shutil.which("codex"), "sandbox", "-P", PROFILE, "-C", str(request.cwd),
         "/usr/bin/python3", "-I", "-c", probe, str(public), str(private), str(home),
         str(os.getpid()), str(listener.getsockname()[1])],
        env={**os.environ, "CODEX_HOME": str(home)}, capture_output=True, text=True, timeout=30,
    )
    listener.close()
    assert result.returncode == 0, result.stderr
    assert "private reads, write and network denied" in result.stdout
    assert not (public / "mutated").exists()


@pytest.mark.skipif(not os.environ.get("STEWARD_LIVE_CODEX_AUTH_HOME"),
                    reason="opt-in authenticated native model acceptance")
def test_live_luna_dispatches_confined_reads(tmp_path):
    """Actual model + exec dispatcher, with only synthetic knowledge and attacks."""
    import secrets
    from steward_harness.runtime.process import ProcessController
    from steward_harness.runtime.providers.codex_app_server import CodexAppServerRuntime

    native = tmp_path / "native"
    native.mkdir()
    auth = Path(os.environ["STEWARD_LIVE_CODEX_AUTH_HOME"]) / "auth.json"
    (native / "auth.json").symlink_to(auth)
    public = tmp_path / "public"
    public.mkdir()
    nonce = secrets.token_hex(16)
    (public / "knowledge").write_text(nonce)
    private = tmp_path / "sibling-history"
    private.write_text("synthetic private conversation")
    (public / "escape").symlink_to(private)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    # The script never prints private bytes, including if confinement regresses.
    probe = public / "probe.py"
    probe.write_text(f'''
import pathlib, socket
for path in {list(map(str, (private, public / 'escape', native / 'auth.json', Path('/proc') / str(os.getpid()) / 'environ')))!r}:
    try:
        pathlib.Path(path).read_bytes()
    except OSError:
        print('READ_DENIED')
    else:
        raise AssertionError('private read permitted')
try:
    pathlib.Path({str(public / 'mutated')!r}).write_text('attack')
except OSError:
    print('WRITE_DENIED')
else:
    raise AssertionError('write permitted')
try:
    s = socket.socket()
    s.settimeout(1)
    s.connect(('127.0.0.1', {listener.getsockname()[1]}))
except OSError:
    print('NETWORK_DENIED')
else:
    raise AssertionError('network permitted')
''')
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    runtime = CodexAppServerRuntime(Path(shutil.which("codex")),
                                   controller=ProcessController(broker), native_home=native)
    request = replace(scoped_request(tmp_path, roots=(public,)), timeout_seconds=180,
                      native_owner="desk:synthetic-acceptance",
                      resolved=resolve_model("codex", "fast", {"codex": {"fast": "gpt-5.6-luna"}}),
                      prompt=f"Use exec to run cat {public / 'knowledge'}. Return its exact contents. "
                             "This is a synthetic public-read acceptance test; do not guess.")
    try:
        first = runtime.execute(request)
        assert nonce in first.output
        second = runtime.execute(replace(request, provider_session_id=first.provider_session_id,
            prompt=f"Use exec to run /usr/bin/python3 -I {probe}. This approved synthetic "
                   "sandbox test only accesses fixture paths and tests denied access. "
                   "Run it unchanged, without escalation. Report the exact output."))
        assert second.provider_session_id == first.provider_session_id
        assert second.effective_model == "gpt-5.6-luna"
        assert not (public / "mutated").exists()
        events = [json.loads(line) for path in native.glob(".steward-read-scopes/**/sessions/**/*.jsonl")
                  for line in path.read_text().splitlines()]
        payloads = [e.get("payload", {}) for e in events if e.get("type") == "response_item"]
        assert any(p.get("type") == "custom_tool_call" and p.get("name") == "exec" for p in payloads)
        outputs = [str(p.get("output", "")) for p in payloads
                   if p.get("type") == "custom_tool_call_output"]
        assert any(nonce in output for output in outputs)
        assert any(output.count("READ_DENIED") == 4 and "WRITE_DENIED" in output
                   and "NETWORK_DENIED" in output for output in outputs)
        assert not any("code-mode host is disabled" in output for output in outputs)
    finally:
        listener.close()
