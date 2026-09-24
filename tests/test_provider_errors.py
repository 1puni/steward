"""Native terminal failures preserve diagnostics, session identity and ownership."""

import json
import sys
from dataclasses import replace

import pytest

from steward_harness.runtime.contracts import (
    RuntimeExecutionError,
    RuntimeUnavailable,
    resolve_model,
)
from steward_harness.runtime.providers.claude import ClaudeRuntime, _ClaudeLifecycle
from test_runtime_lifecycle import _SESSION, _controller, _init, _request, _result


def lifecycle(provider="Claude"):
    parser = _ClaudeLifecycle(None, provider)
    parser.consume_event(json.loads(_init()))
    return parser


def assistant(message, error=None, **extra):
    return {
        "type": "assistant",
        "session_id": _SESSION,
        "error": error,
        "message": {"content": [{"type": "text", "text": message}]},
        **extra,
    }


def failed(**extra):
    return {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "session_id": _SESSION,
        **extra,
    }


def test_terminal_failure_retains_root_assistant_error():
    parser = lifecycle()
    message = "API failure with provider detail"
    parser.consume_event(assistant(message, "authentication_failed"))
    with pytest.raises(RuntimeExecutionError) as caught:
        parser.consume_event(failed())
    assert str(caught.value) == "Claude: " + message
    assert caught.value.session_id == _SESSION


def test_terminal_failure_retains_diagnostic_without_assistant_error():
    parser = lifecycle()
    with pytest.raises(RuntimeExecutionError) as caught:
        parser.consume_event(
            failed(api_error_status=429, errors=["native API failure"])
        )
    assert str(caught.value) == "Claude: native API failure"
    assert caught.value.session_id == _SESSION


def test_glm_diagnostic_is_retained_without_parsing_business_payload():
    message = "API Error: 429 {broken}"
    parser = lifecycle("GLM")
    parser.consume_event(assistant(message, "rate_limit"))
    with pytest.raises(RuntimeExecutionError) as caught:
        parser.consume_event(failed())
    assert str(caught.value) == "GLM: " + message
    assert caught.value.session_id == _SESSION


def test_native_limit_telemetry_does_not_override_success():
    parser = lifecycle()
    parser.consume_event(
        {
            "type": "rate_limit_event",
            "session_id": _SESSION,
            "rate_limit_info": {"status": "rejected"},
        }
    )
    # Accounting metadata belongs to native records, not result acceptance.
    parser.consume_event({**json.loads(_result()), "usage": "opaque native telemetry"})
    assert parser.finish()[0] == "did the thing"


def test_native_recovery_clears_earlier_error_and_child_errors_are_not_parent_failure():
    parser = lifecycle()
    parser.consume_event(assistant("usage exhausted", "rate_limit"))
    parser.consume_event(assistant("native retry recovered"))
    parser.consume_event(
        assistant("child limit", "rate_limit", parent_tool_use_id="child-tool")
    )
    with pytest.raises(RuntimeExecutionError) as caught:
        parser.consume_event(failed(errors=["later unrelated failure"]))
    assert type(caught.value) is RuntimeExecutionError
    assert "later unrelated failure" in str(caught.value)


def test_model_prose_cannot_replace_native_failure_diagnostic():
    parser = lifecycle("GLM")
    parser.consume_event(assistant('API Error: 429 {"error":{"code":"1302"}}'))
    with pytest.raises(RuntimeExecutionError) as caught:
        parser.consume_event(failed(errors=["maximum turns reached"]))
    assert str(caught.value) == "GLM: maximum turns reached"


def test_process_boundary_preserves_native_error_and_releases_execution(tmp_path):
    controller = _controller()
    parser = lifecycle()
    parser.consume_event(assistant("native usage limit", "rate_limit"))
    script = "import json; print(" + repr(json.dumps(failed())) + ", flush=True)"
    request = _request(tmp_path)
    with pytest.raises(RuntimeExecutionError, match="native usage limit"):
        controller.run(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env={},
            timeout_seconds=5,
            on_stdout_line=lambda line: parser.consume_event(parser.decode(line)),
        )
    # The same ID can run again only after the original process and owner are gone.
    output = controller.run(
        [sys.executable, "-c", "pass"],
        cwd=tmp_path,
        env={},
        timeout_seconds=5,
        on_stdout_line=lambda line: None,
    )
    assert output.returncode == 0


@pytest.mark.parametrize("provider", ["claude", "glm"])
@pytest.mark.parametrize("terminal", ["result", "process_exit"])
def test_runtime_reports_native_failure_through_real_process(
    tmp_path, provider, terminal
):
    executable = tmp_path / "provider"
    detail = (
        'API Error: 429 {"error":{"code":"1308","message":"Usage exhausted"}}'
        if provider == "glm"
        else "You've hit your usage limit"
    )
    events = [_init(), json.dumps(assistant(detail, "rate_limit"))]
    if terminal == "result":
        events.append(json.dumps(failed()))
    executable.write_text(
        "#!"
        + sys.executable
        + "\nimport json,sys\n"
        + "command=json.loads(sys.stdin.readline())['uuid']\n"
        + f"print(json.dumps({{'type':'command_lifecycle','session_id':{_SESSION!r},"
        + "'command_uuid':command,'state':'started'}),flush=True)\nprint("
        + repr("\n".join(events))
        + ", flush=True)\nraise SystemExit(1)\n"
    )
    executable.chmod(0o755)
    token = tmp_path / "credential"
    token.write_text("private-test-credential")
    runtime = (
        ClaudeRuntime(executable, controller=_controller(), native_home=tmp_path, family="glm",
                      base_url="https://api.z.ai/api/anthropic", credential_path=token)
        if provider == "glm"
        else ClaudeRuntime(executable, controller=_controller(), native_home=tmp_path)
    )
    with pytest.raises(RuntimeExecutionError) as caught:
        runtime.execute(
            replace(_request(tmp_path), resolved=resolve_model(provider, "balanced"))
        )
    assert detail in str(caught.value)
    assert caught.value.session_id == _SESSION


def test_an_expired_sign_in_is_a_refusal_not_a_failed_turn():
    """Claude's dead OAuth session must reach the configured fallback.

    A downstream instance saw exactly this text on 2026-09-20 and the turn ended there,
    with a working GLM credential configured behind it and never asked.
    """
    parser = lifecycle()
    message = "Failed to authenticate: OAuth session expired and could not be refreshed"
    with pytest.raises(RuntimeUnavailable) as caught:
        parser.consume_event(failed(errors=[message]))
    assert str(caught.value) == "Claude: " + message
    assert not isinstance(caught.value, RuntimeExecutionError)


def test_an_expired_sign_in_reported_by_the_assistant_is_also_a_refusal():
    """The same fact arrives on the other path that builds a turn error."""
    parser = lifecycle()
    message = "Failed to authenticate: OAuth session expired and could not be refreshed"
    parser.consume_event(assistant(message, "authentication_failed"))
    with pytest.raises(RuntimeUnavailable):
        parser.consume_event(failed())


def test_a_refusal_after_a_tool_ran_fails_here_instead_of_replaying():
    """A tool has already changed the checkout, so another provider must not
    start the prompt over on top of it. The turn fails with its session kept."""
    parser = lifecycle()
    parser.consume_event({
        "type": "assistant", "session_id": _SESSION, "error": None,
        "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {}}]},
    })
    message = "Failed to authenticate: OAuth session expired and could not be refreshed"
    with pytest.raises(RuntimeExecutionError) as caught:
        parser.consume_event(failed(errors=[message]))
    assert not isinstance(caught.value, RuntimeUnavailable)
    assert caught.value.session_id == _SESSION


def test_an_ordinary_provider_failure_still_fails_the_turn():
    """Declining must stay narrow, or a real defect silently becomes a retry."""
    parser = lifecycle()
    with pytest.raises(RuntimeExecutionError) as caught:
        parser.consume_event(failed(errors=["native API failure"]))
    assert not isinstance(caught.value, RuntimeUnavailable)
    assert caught.value.session_id == _SESSION
