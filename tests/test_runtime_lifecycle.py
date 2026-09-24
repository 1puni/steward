"""Claude/GLM stream lifecycle: init gating, background-agent notices, results."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.runtime.contracts import (
    RuntimeExecutionError,
    RuntimeRequest,
    RuntimeUnavailable,
    SandboxMode,
    resolve_model,
)
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.process import ProcessController
from steward_harness.runtime.providers.claude import ClaudeRuntime, _ClaudeLifecycle
from steward_harness.runtime.providers.codex_app_server import CodexAppServerRuntime

_ZAI = "https://api.z.ai/api/anthropic"

_SESSION = "11111111-1111-1111-1111-111111111111"


def _controller() -> ProcessController:
    return ProcessController(UntrustedExecutionBroker(UntrustedExecutionConfig()))


def test_process_cancel_terminates_provider_process_group(tmp_path: Path) -> None:
    controller = _controller()
    child_pids: list[int] = []
    errors: list[RuntimeExecutionError] = []
    ready = threading.Event()
    stoppers: list = []
    script = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "print(p.pid,flush=True); time.sleep(60)"
    )

    def run() -> None:
        try:
            controller.run(
                [sys.executable, "-c", script],
                cwd=tmp_path,
                env=os.environ,
                timeout_seconds=30,
                on_stdout_line=lambda line: (child_pids.append(int(line)), ready.set()),
                on_started=stoppers.append,
            )
        except RuntimeExecutionError as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5), "provider child did not start"
    stoppers[0]()
    thread.join(5)

    assert not thread.is_alive()
    assert errors and "cancelled" in str(errors[0])
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pids[0], 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("provider child survived process-group cancellation")


def _lifecycle() -> _ClaudeLifecycle:
    return _ClaudeLifecycle(None, "GLM")


def _line(event: dict) -> str:
    return json.dumps(event)


def _init() -> str:
    return _line(
        {"type": "system", "subtype": "init", "session_id": _SESSION, "model": "glm-5.3"}
    )


def _task_notification() -> str:
    # As emitted for a background agent: no session_id anywhere in the event.
    return _line(
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "ac85f6ea14c0dcce2",
            "status": "stopped",
            "output_file": "/tmp/x/tasks/ac85f6ea14c0dcce2.output",
            "summary": "No completion record was found for background agent.",
        }
    )


def _result() -> str:
    return _line(
        {
            "type": "result",
            "subtype": "success",
            "terminal_reason": "completed",
            "session_id": _SESSION,
            "result": "did the thing",
            "usage": {"input_tokens": 1},
        }
    )


def test_notification_before_init_is_tolerated() -> None:
    """A resumed session reports orphaned background agents before init."""
    lifecycle = _lifecycle()
    lifecycle.consume_event(json.loads(_task_notification()))
    lifecycle.consume_event(json.loads(_init()))
    lifecycle.consume_event(json.loads(_result()))
    assert lifecycle.finish() == ("did the thing", _SESSION, "glm-5.3")


def _dev_intent() -> dict:
    # Synthetic envelope from Claude Code 2.1.281's project scanner.
    return {"type": "system", "subtype": "dev_intent",
            "kind": "android_app", "trigger": "project_scan"}


@pytest.mark.parametrize("provider", ["Claude", "GLM"])
@pytest.mark.parametrize("resumed", [False, True])
def test_dev_intent_is_informational_throughout_turn(provider, resumed):
    lifecycle = _ClaudeLifecycle(_SESSION if resumed else None, provider)
    assert lifecycle.consume_event(_dev_intent()) is None
    assert lifecycle.session_id is None and lifecycle.effective_model is None
    assert lifecycle.consume_event(json.loads(_init())) == _SESSION
    lifecycle.consume_event(_dev_intent())
    lifecycle.consume_event(json.loads(_result()))
    lifecycle.consume_event(_dev_intent())
    assert lifecycle.finish() == ("did the thing", _SESSION, "glm-5.3")


@pytest.mark.parametrize("initialized", [False, True])
def test_dev_intent_cannot_complete_a_turn(initialized):
    lifecycle = _lifecycle()
    if initialized:
        lifecycle.consume_event(json.loads(_init()))
    lifecycle.consume_event(_dev_intent())
    with pytest.raises(RuntimeExecutionError, match="before a successful terminal result"):
        lifecycle.finish()


@pytest.mark.parametrize("event, match", [
    ({"type": "system", "subtype": "unknown"}, "did not start with system init"),
    ({"type": "assistant", "subtype": "dev_intent"}, "did not start with system init"),
    ({"type": "error", "error": {"message": "synthetic error"}}, "did not start with system init"),
    (json.loads(_result()), "did not start with system init"),
    ({"type": "system", "subtype": "init", "session_id": "bad", "model": "model"},
     "invalid persistent session identity"),
    ({"type": "system", "subtype": "init", "session_id": _SESSION}, "omitted its effective model"),
])
def test_dev_intent_preserves_initialization_gate(event, match):
    lifecycle = _lifecycle()
    lifecycle.consume_event(_dev_intent())
    with pytest.raises(RuntimeExecutionError, match=match):
        lifecycle.consume_event(event)


def test_dev_intent_preserves_resume_identity():
    lifecycle = _ClaudeLifecycle("22222222-2222-4222-8222-222222222222", "Claude")
    lifecycle.consume_event(_dev_intent())
    with pytest.raises(RuntimeExecutionError, match="resumed a different"):
        lifecycle.consume_event(json.loads(_init()))


@pytest.mark.parametrize("event, match", [
    ({"type": "error", "session_id": _SESSION, "error": {"message": "synthetic failure"}},
     "synthetic failure"),
    ({**json.loads(_result()), "is_error": True, "result": "synthetic failure"}, "synthetic failure"),
    ({**json.loads(_result()), "terminal_reason": "interrupted"}, "did not finish"),
    ({**json.loads(_result()), "session_id": "22222222-2222-4222-8222-222222222222"},
     "different persistent session identity"),
    ({**json.loads(_result()), "session_id": None}, "invalid persistent session identity"),
])
def test_dev_intent_preserves_turn_failures(event, match):
    lifecycle = _lifecycle()
    lifecycle.consume_event(_dev_intent())
    lifecycle.consume_event(json.loads(_init()))
    lifecycle.consume_event(_dev_intent())
    with pytest.raises(RuntimeExecutionError, match=match):
        lifecycle.consume_event(event)


@pytest.mark.parametrize("line", ['{"type":"system","subtype":"dev_intent"', '[]', '{"subtype":"dev_intent"}'])
def test_malformed_dev_intent_stream_is_rejected(line):
    with pytest.raises(RuntimeExecutionError, match="malformed event stream"):
        _lifecycle().decode(line)


def test_notification_mid_turn_is_tolerated() -> None:
    """A background agent finishing mid-turn reports after init, without a session id."""
    lifecycle = _lifecycle()
    lifecycle.consume_event(json.loads(_init()))
    lifecycle.consume_event(json.loads(_task_notification()))
    lifecycle.consume_event(json.loads(_result()))
    assert lifecycle.finish()[0] == "did the thing"


def test_non_init_first_event_still_rejected() -> None:
    from steward_harness.runtime.contracts import RuntimeExecutionError

    lifecycle = _lifecycle()
    with pytest.raises(RuntimeExecutionError, match="did not start with system init"):
        lifecycle.consume_event({"type": "assistant", "session_id": _SESSION})


def test_late_native_task_notice_does_not_replace_terminal_result():
    lifecycle = _lifecycle()
    lifecycle.consume_event(json.loads(_init()))
    lifecycle.consume_event(json.loads(_result()))
    lifecycle.consume_event(json.loads(_task_notification()))
    assert lifecycle.finish()[0] == "did the thing"


def _request(
    tmp_path: Path, sandbox_mode: SandboxMode = "read-only"
) -> RuntimeRequest:
    return RuntimeRequest(
        execution_id="turn-1",
        resolved=resolve_model("claude", "balanced"),
        provider_session_id=None,
        prompt="inspect the repository",
        cwd=tmp_path,
        timeout_seconds=30,
        sandbox_mode=sandbox_mode,
    )


def test_claude_command_enforces_native_sandbox_and_scoped_tools(
    tmp_path: Path,
) -> None:
    runtime = ClaudeRuntime(controller=_controller(), native_home=tmp_path)
    read_only = runtime._command(_request(tmp_path), None)
    writable = runtime._command(_request(tmp_path, "workspace-write"), None)

    assert "--dangerously-skip-permissions" not in read_only
    settings = json.loads(read_only[read_only.index("--settings") + 1])
    assert settings["sandbox"] == {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "network": {"allowAllUnixSockets": True},
    }
    assert read_only[read_only.index("--permission-mode") + 1] == "dontAsk"
    assert "Write" not in read_only
    assert "Bash(git status:*)" in read_only
    assert writable[writable.index("--permission-mode") + 1] == "acceptEdits"
    assert "Write" in writable
    assert "Bash" in writable
    for command in (read_only, writable):
        assert command[-4:] == ["-p", "--input-format", "stream-json", "--replay-user-messages"]
        assert _request(tmp_path).prompt not in command


def test_dropped_identity_grants_unrestricted_workspace(tmp_path: Path) -> None:
    """A dropped OS identity is what makes unrestricted workspace execution safe.

    This used to require a container specifically, which made unrestricted
    execution a reason to run Docker rather than a consequence of having a
    boundary at all.
    """
    controller = ProcessController(UntrustedExecutionBroker(
        UntrustedExecutionConfig(user="steward", group="steward",
                                 home="/var/lib/steward-agent")))
    runtime = ClaudeRuntime(controller=controller, native_home=tmp_path)
    writable = runtime._command(_request(tmp_path, "workspace-write"), None)

    assert writable[writable.index("--permission-mode") + 1] == "bypassPermissions"
    settings = json.loads(writable[writable.index("--settings") + 1])
    assert settings["sandbox"] == {"enabled": False}
    # Read-only turns are unaffected: the boundary is not a reason to widen them.
    read_only = runtime._command(_request(tmp_path), None)
    assert read_only[read_only.index("--permission-mode") + 1] == "dontAsk"


def test_absent_boundary_keeps_the_provider_asking(tmp_path: Path) -> None:
    runtime = ClaudeRuntime(controller=_controller(), native_home=tmp_path)
    writable = runtime._command(_request(tmp_path, "workspace-write"), None)
    assert writable[writable.index("--permission-mode") + 1] == "acceptEdits"


@pytest.mark.parametrize("writable", [False, True])
def test_native_memory_enablement_reaches_provider_environment(tmp_path, monkeypatch, writable):
    runtime = ClaudeRuntime(controller=_controller(), native_home=tmp_path)
    captured = {}

    def capture(command, *, env, **kwargs):
        captured.update(env)
        raise RuntimeError("captured launch")

    monkeypatch.setattr(runtime._controller, "run", capture)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    request = _request(tmp_path, "workspace-write" if writable else "read-only")
    with pytest.raises(RuntimeError, match="captured launch"):
        runtime.execute(request)
    assert captured["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == ("0" if writable else "1")


def test_provider_children_receive_only_their_provider_credentials(
    tmp_path: Path, monkeypatch,
) -> None:
    inherited = {
        "ANTHROPIC_API_KEY": "anthropic",
        "GEMINI_API_KEY": "gemini",
        "GOOGLE_API_KEY": "google",
        "OPENAI_API_KEY": "openai",
        "ZAI_AUTH_TOKEN": "zai-ambient",
    }
    glm_token = tmp_path / "zai-token"
    glm_token.write_text("zai-file", encoding="utf-8")
    monkeypatch.setenv("ZAI_AUTH_TOKEN", "unrelated-controller-token")

    claude = ClaudeRuntime(controller=_controller(), native_home=tmp_path).environment(inherited)
    codex = CodexAppServerRuntime(controller=_controller(), native_home=tmp_path).environment(inherited)
    glm_runtime = ClaudeRuntime(
        Path(sys.executable), controller=_controller(), native_home=tmp_path,
        family="glm", base_url=_ZAI, credential_path=glm_token,
    )
    assert glm_runtime.available().available
    glm = glm_runtime.environment(inherited)

    assert claude["ANTHROPIC_API_KEY"] == "anthropic"
    assert "OPENAI_API_KEY" not in claude
    assert codex["OPENAI_API_KEY"] == "openai"
    assert "ANTHROPIC_API_KEY" not in codex
    assert glm["ANTHROPIC_AUTH_TOKEN"] == "zai-file"
    assert glm["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert "ANTHROPIC_API_KEY" not in glm
    assert "OPENAI_API_KEY" not in glm
    assert "ZAI_AUTH_TOKEN" not in glm

    glm_token.unlink()
    assert not glm_runtime.available().available
    with pytest.raises(RuntimeUnavailable, match="GLM credential is unavailable"):
        glm_runtime.environment(inherited)


def test_claude_explicit_routing_replaces_ambient_endpoint_identity(tmp_path: Path) -> None:
    inherited = {
        "ANTHROPIC_API_KEY": "ambient-key",
        "ANTHROPIC_BASE_URL": "https://ambient.invalid",
        "ANTHROPIC_AUTH_TOKEN": "ambient-token",
        "ZAI_AUTH_TOKEN": "zai-ambient",
    }
    router_token = tmp_path / "router-token"
    router_token.write_text("ci_live_router", encoding="utf-8")
    runtime = ClaudeRuntime(
        Path(sys.executable),
        controller=_controller(),
        native_home=tmp_path,
        base_url="https://api.cheaperinference.com",
        credential_path=router_token,
    )
    assert runtime.available().available
    environment = runtime.environment(inherited)
    assert environment["ANTHROPIC_BASE_URL"] == "https://api.cheaperinference.com"
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "ci_live_router"
    assert "ANTHROPIC_API_KEY" not in environment
    assert "ZAI_AUTH_TOKEN" not in environment

    router_token.unlink()
    assert not runtime.available().available
    with pytest.raises(RuntimeUnavailable, match="Claude credential is unavailable"):
        runtime.environment(inherited)


def test_claude_explicit_routing_needs_base_url_and_credential_together(tmp_path: Path) -> None:
    token = tmp_path / "router-token"
    token.write_text("ci_live_router", encoding="utf-8")
    with pytest.raises(ValueError, match="both base URL and credential path"):
        ClaudeRuntime(
            Path(sys.executable),
            controller=_controller(),
            native_home=tmp_path,
            base_url="https://api.cheaperinference.com",
        )
    with pytest.raises(ValueError, match="both base URL and credential path"):
        ClaudeRuntime(
            Path(sys.executable),
            controller=_controller(),
            native_home=tmp_path,
            credential_path=token,
        )


def test_glm_base_url_is_configurable_and_defaults_to_zai(tmp_path: Path) -> None:
    token = tmp_path / "zai-token"
    token.write_text("zai-file", encoding="utf-8")
    default = ClaudeRuntime(
        Path(sys.executable), controller=_controller(), native_home=tmp_path,
        family="glm", base_url=_ZAI, credential_path=token,
    )
    assert default.environment()["ANTHROPIC_BASE_URL"] == _ZAI
    routed = ClaudeRuntime(
        Path(sys.executable), controller=_controller(), native_home=tmp_path,
        family="glm", base_url="https://api.cheaperinference.com", credential_path=token,
    )
    assert routed.environment()["ANTHROPIC_BASE_URL"] == "https://api.cheaperinference.com"
    assert routed.environment()["ANTHROPIC_AUTH_TOKEN"] == "zai-file"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_provider_adapter_does_not_hide_session_persistence_defects(tmp_path, provider):
    resolved = resolve_model(provider, "balanced")

    def fail(session):
        raise ValueError("broken persistence invariant")

    executable = tmp_path / "provider"
    if provider == "codex":
        script = (
            "import json,sys\n"
            "for line in sys.stdin:\n"
            " event=json.loads(line)\n"
            " if event.get('method')=='initialize': result={}\n"
            f" elif event.get('method')=='thread/start': result={{'thread':{{'id':{_SESSION!r}}}}}\n"
            " else: continue\n"
            " print(json.dumps({'id':event['id'],'result':result}),flush=True)\n"
        )
    else:
        event = {"type": "system", "subtype": "init", "session_id": _SESSION, "model": resolved.model}
        script = "import time\nprint(" + repr(json.dumps(event)) + ",flush=True)\ntime.sleep(60)\n"
    executable.write_text(f"#!{sys.executable}\n" + script)
    executable.chmod(0o755)
    runtime = (CodexAppServerRuntime if provider == "codex" else ClaudeRuntime)(
        executable, controller=_controller(), native_home=tmp_path,
    )
    request = RuntimeRequest(
        execution_id="persistence", resolved=resolved, provider_session_id=None,
        prompt="Inspect current evidence.", cwd=tmp_path, timeout_seconds=5,
        sandbox_mode="read-only", on_session_started=fail,
    )
    with pytest.raises(ValueError, match="broken persistence invariant"):
        runtime.execute(request)


@pytest.mark.parametrize("reason", ["cancel", "timeout"])
@pytest.mark.parametrize("cooperates", [True, False])
def test_codex_native_interrupt_reaches_process_and_bounds_cleanup(tmp_path, monkeypatch, reason, cooperates):
    """Exercise adapter wiring over real pipes, including a refusing provider."""
    from textwrap import dedent
    from steward_harness.runtime.process import ProcessTimeout

    if not cooperates:
        monkeypatch.setattr("steward_harness.runtime.process._COOPERATIVE_GRACE_SECONDS", .2)
        monkeypatch.setattr("steward_harness.runtime.process._TERMINATION_GRACE_SECONDS", .2)
    transcript, checkpoint = tmp_path / "requests.jsonl", tmp_path / "checkpoint"
    executable = tmp_path / "codex"
    executable.write_text(f"#!{sys.executable}\n" + dedent(f'''
        import json, pathlib, signal, sys
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        for line in sys.stdin:
            event = json.loads(line)
            with open({str(transcript)!r}, 'a') as log:
                log.write(line)
            method = event.get('method')
            if method == 'initialize':
                result = {{}}
            elif method == 'thread/start':
                result = {{'thread': {{'id': {_SESSION!r}}}}}
            elif method == 'turn/start':
                result = {{'turn': {{'id': 'working'}}}}
            elif method == 'turn/interrupt':
                assert event['params'] == {{'threadId': {_SESSION!r}, 'turnId': 'working'}}
                if not {cooperates!r}:
                    continue
                pathlib.Path({str(checkpoint)!r}).write_text('final native write')
                print(json.dumps({{'id': event['id'], 'result': {{}}}}), flush=True)
                print(json.dumps({{'method': 'turn/completed', 'params': {{
                    'threadId': {_SESSION!r}, 'turn': {{'id': 'working', 'status': 'interrupted'}}
                }}}}), flush=True)
                continue
            elif method == 'thread/backgroundTerminals/clean':
                result = {{}}
            else:
                continue
            print(json.dumps({{'id': event['id'], 'result': result}}), flush=True)
    '''))
    executable.chmod(0o755)
    stoppers = []
    runtime = CodexAppServerRuntime(executable, controller=_controller(), native_home=tmp_path)
    request = RuntimeRequest(
        execution_id="interrupted", resolved=resolve_model("codex", "fast"),
        provider_session_id=None, prompt="do work", cwd=tmp_path,
        timeout_seconds=1 if reason == "timeout" else 30,
        on_started=stoppers.append,
        on_input_ready=lambda _send: stoppers[0]() if reason == "cancel" else None,
    )
    started = time.monotonic()
    with pytest.raises(RuntimeExecutionError, match="cancelled" if reason == "cancel" else "timed out") as raised:
        runtime.execute(request)
    assert time.monotonic() - started < 5, "interruption retained the original 30s deadline"
    assert raised.value.session_id == _SESSION
    assert isinstance(raised.value, ProcessTimeout) == (reason == "timeout")
    methods = [json.loads(line).get("method") for line in transcript.read_text().splitlines()]
    assert methods.count("turn/interrupt") == 1
    assert ("thread/backgroundTerminals/clean" in methods) == cooperates
    assert checkpoint.exists() == cooperates
    if cooperates:
        assert checkpoint.read_text() == "final native write"
