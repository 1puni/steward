"""Native Claude command identity fences folded and queued input acceptance."""
import json
from types import SimpleNamespace

import pytest

from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeInput, RuntimeRequest, resolve_model
from steward_harness.runtime.providers.claude import ClaudeInputStream, _ClaudeLifecycle

SESSION = "11111111-1111-4111-8111-111111111111"


class Wire:
    def __init__(self):
        self.messages = []
        self.closed = False

    def write(self, text):
        assert not self.closed
        self.messages.append(json.loads(text))

    def close(self):
        self.closed = True


def start(tmp_path, provider="glm", *, ongoing_input=True, allow_empty_output=False):
    receipts, senders = [], []
    request = RuntimeRequest(
        execution_id="native", resolved=resolve_model(provider, "fast"),
        provider_session_id=None, prompt="work", cwd=tmp_path, timeout_seconds=5,
        on_input_ready=senders.append if ongoing_input else None,
        on_input_result=receipts.append,
        allow_empty_output=allow_empty_output,
    )
    lifecycle = _ClaudeLifecycle(None, provider, allow_empty_output=request.allow_empty_output)
    stream = ClaudeInputStream(request, lifecycle)
    wire = Wire()
    stream.connect(wire)
    root = wire.messages[-1]["uuid"]
    command(stream, root, "queued")
    command(stream, root, "started")
    emit(stream, type="system", subtype="init", model="glm")
    assert len(senders) == int(ongoing_input)
    return stream, wire, root, receipts


def emit(stream, **event):
    stream.consume(json.dumps({"session_id": SESSION, **event}))


def command(stream, identity, state):
    emit(stream, type="command_lifecycle", command_uuid=identity, state=state)


def result(stream, identity, *, terminal="completed", text="findings"):
    emit(stream, type="result", user_message_uuid=identity, subtype="success",
         terminal_reason=terminal, result=text, usage={})


@pytest.mark.parametrize("provider", ["claude", "glm"])
def test_initial_command_without_live_input_waits_for_native_completion(tmp_path, provider):
    stream, wire, root, receipts = start(tmp_path, provider, ongoing_input=False)
    assert wire.messages[0]["message"]["content"] == "work"
    assert stream.commands == {root: None}
    result(stream, root)
    assert not wire.closed
    with pytest.raises(RuntimeExecutionError, match="before correlated"):
        stream.finish()
    command(stream, root, "completed")
    stream.finish()
    assert wire.closed
    assert not receipts, "the initial prompt is not a separately delivered input source"


def test_folded_source_waits_for_consuming_result_and_root_completion(tmp_path):
    stream, wire, root, receipts = start(tmp_path)
    stream.send(RuntimeInput("task:3", "controller evidence", origin="controller", author="harness"))
    correction = wire.messages[-1]["uuid"]
    assert '"origin": "controller"' in wire.messages[-1]["message"]["content"]
    emit(stream, type="user", uuid=correction, message={"role": "user", "content": "echo"})
    assert not receipts, "an input echo is not delivery acknowledgement"
    for state in ("queued", "started", "completed"):
        command(stream, correction, state)
    assert [(r.source_id, r.disposition) for r in receipts] == [("task:3", "accepted")]
    assert not wire.closed
    result(stream, root)
    assert not wire.closed
    command(stream, root, "completed")
    assert wire.closed
    stream.finish()
    assert stream.lifecycle.finish()[0] == "findings"


def test_source_queued_at_result_boundary_requires_its_own_result(tmp_path):
    stream, wire, root, _ = start(tmp_path)
    result(stream, root, text="first")
    stream.send(RuntimeInput("late", "next work"))
    late = wire.messages[-1]["uuid"]
    command(stream, root, "completed")
    assert not wire.closed
    for state in ("queued", "started"):
        command(stream, late, state)
    assert not wire.closed
    with pytest.raises(RuntimeExecutionError, match="before correlated"):
        stream.finish()
    result(stream, late, text="final")
    assert not wire.closed
    command(stream, late, "completed")
    assert wire.closed
    stream.finish()
    assert stream.lifecycle.finish()[0] == "final"


def test_source_without_native_acknowledgement_remains_unresolved(tmp_path):
    stream, _, _, receipts = start(tmp_path)
    stream.send(RuntimeInput("uncertain", "correction"))
    stream.disconnect()
    stream.disconnect()
    assert [(r.source_id, r.disposition) for r in receipts] == [("uncertain", "unresolved")]
    with pytest.raises(RuntimeExecutionError):
        stream.finish()


@pytest.mark.parametrize("provider", ["claude", "glm"])
def test_native_failure_preserves_unresolved_live_input(tmp_path, provider):
    stream, wire, root, receipts = start(tmp_path, provider)
    stream.send(RuntimeInput("correction", "Operator correction"))
    message = ('API Error: 429 {"error":{"code":"1302","message":"Native limit"}}'
               if provider == "glm" else "Native usage limit")
    emit(stream, type="assistant", error="rate_limit",
         message={"content": [{"type": "text", "text": message}]})
    with pytest.raises(RuntimeExecutionError) as caught:
        emit(stream, type="result", user_message_uuid=root,
             subtype="error_during_execution", is_error=True)
    assert caught.value.session_id == SESSION
    assert message in str(caught.value)
    stream.disconnect()
    assert [(r.source_id, r.disposition) for r in receipts] == [("correction", "unresolved")]
    assert len(wire.messages) == 2, "failure must not replay the live input"


@pytest.mark.parametrize("terminal", ["max_turns", "hook_stopped", "tool_deferred", "aborted_streaming", None])
def test_noncompleted_native_reason_never_returns_candidate(tmp_path, terminal):
    stream, wire, root, _ = start(tmp_path)
    with pytest.raises(RuntimeExecutionError, match="did not finish"):
        result(stream, root, terminal=terminal)
    assert not wire.closed


@pytest.mark.parametrize("state", ["cancelled", "discarded"])
def test_explicit_unaccepted_source_failure_is_rejected(tmp_path, state):
    stream, wire, _, receipts = start(tmp_path)
    stream.send(RuntimeInput("rejected", "correction"))
    with pytest.raises(RuntimeExecutionError, match="did not complete"):
        command(stream, wire.messages[-1]["uuid"], state)
    stream.disconnect()
    assert [r.disposition for r in receipts] == ["rejected"]


def test_unowned_or_wrong_session_events_cannot_complete_execution(tmp_path):
    stream, _, root, _ = start(tmp_path)
    with pytest.raises(RuntimeExecutionError, match="unowned"):
        command(stream, "native-other", "completed")
    with pytest.raises(RuntimeExecutionError, match="session identity"):
        emit(stream, type="command_lifecycle", command_uuid=root, state="completed",
             session_id="22222222-2222-4222-8222-222222222222")
    with pytest.raises(RuntimeExecutionError, match="no offered command identity"):
        result(stream, "native-other")


def test_completion_race_is_rejected_before_any_bytes_are_offered(tmp_path):
    stream, wire, root, receipts = start(tmp_path)
    result(stream, root)
    command(stream, root, "completed")
    sent = len(wire.messages)
    with pytest.raises(RuntimeExecutionError, match="no longer accepting"):
        stream.send(RuntimeInput("late", "new correction"))
    stream.disconnect()
    assert len(wire.messages) == sent
    assert [(r.source_id, r.disposition) for r in receipts] == [("late", "rejected")]


def test_result_without_optional_timing_uuid_is_fenced_by_native_root(tmp_path):
    stream, wire, root, _ = start(tmp_path)
    emit(stream, type="result", subtype="success", terminal_reason="completed", result="native agents finished")
    assert not wire.closed
    command(stream, root, "completed")
    stream.finish()
    assert wire.closed


def test_native_enqueued_work_shares_writer_without_controller_source(tmp_path):
    stream, wire, root, receipts = start(tmp_path)
    internal = "33333333-3333-4333-8333-333333333333"
    command(stream, internal, "started")
    command(stream, internal, "completed")
    result(stream, root)
    assert not wire.closed
    command(stream, root, "completed")
    stream.finish()
    assert wire.closed and not receipts


@pytest.mark.parametrize("value", [[], {}, None, 42])
def test_malformed_native_fields_are_operational_errors(tmp_path, value):
    stream, _, root, _ = start(tmp_path)
    with pytest.raises(RuntimeExecutionError, match="malformed"):
        command(stream, value, "queued")
    with pytest.raises(RuntimeExecutionError, match="malformed"):
        command(stream, root, value)
    with pytest.raises(RuntimeExecutionError, match="identity"):
        result(stream, value)


def test_root_completion_requires_prior_result(tmp_path):
    stream, _, root, _ = start(tmp_path)
    with pytest.raises(RuntimeExecutionError, match="before its result"):
        command(stream, root, "completed")


@pytest.mark.parametrize("state", ["queued", "started"])
def test_repeated_command_transitions_fail_closed(tmp_path, state):
    stream, _, root, _ = start(tmp_path)
    with pytest.raises(RuntimeExecutionError, match="repeated"):
        command(stream, root, state)


@pytest.mark.parametrize("allow_empty", [False, True])
@pytest.mark.parametrize("text", ["", "   "])
def test_empty_assessment_requires_successful_result_and_command_completion(tmp_path, allow_empty, text):
    stream, _, root, _ = start(tmp_path, allow_empty_output=allow_empty)
    emit(stream, type="assistant", message={"content": [{"type": "text", "text": "Inspecting evidence."}]})
    if not allow_empty:
        with pytest.raises(RuntimeExecutionError, match="without an agent response"):
            result(stream, root, text=text)
        return
    result(stream, root, text=text)
    with pytest.raises(RuntimeExecutionError, match="before correlated"):
        stream.finish()
    command(stream, root, "completed")
    stream.finish()
    assert stream.lifecycle.finish()[0] == ""


@pytest.mark.parametrize("text", [None, 42, {}])
def test_optional_output_rejects_missing_or_nonstring_native_result(tmp_path, text):
    stream, _, root, _ = start(tmp_path, allow_empty_output=True)
    with pytest.raises(RuntimeExecutionError, match="without an agent response"):
        result(stream, root, text=text)


@pytest.mark.parametrize("terminal", ["max_turns", "hook_stopped", "aborted_streaming", None])
def test_empty_assessment_cannot_hide_noncompleted_native_result(tmp_path, terminal):
    stream, _, root, _ = start(tmp_path, allow_empty_output=True)
    with pytest.raises(RuntimeExecutionError, match="did not finish"):
        result(stream, root, terminal=terminal, text="")

def test_interrupt_acknowledgement_waits_for_native_commands_to_stop(tmp_path):
    stream, wire, root, receipts = start(tmp_path)
    stream.stop()
    request = wire.messages[-1]
    assert request["type"] == "control_request"
    assert request["request"] == {"subtype": "interrupt"}
    stream.stop()
    assert wire.messages[-1] is request
    stream.consume(json.dumps({"type": "control_response", "response": {
        "subtype": "success", "request_id": request["request_id"], "response": {},
    }}))
    assert not wire.closed
    with pytest.raises(RuntimeExecutionError, match="no longer accepting"):
        stream.send(RuntimeInput("late", "more work"))
    assert receipts[-1].disposition == "rejected"
    command(stream, root, "cancelled")
    assert wire.closed



def test_interrupted_result_drains_completion_before_eof(tmp_path):
    stream, wire, root, _ = start(tmp_path)
    stream.stop()
    emit(stream, type="result", user_message_uuid=root, subtype="error_during_execution",
         terminal_reason="interrupted", is_error=True, errors=["interrupted"])
    assert not wire.closed
    command(stream, root, "completed")
    assert wire.closed


@pytest.mark.parametrize("provider", ["claude", "glm"])
def test_pre_init_dev_intent_does_not_open_input_or_complete_command(tmp_path, provider):
    sessions, senders = [], []
    request = RuntimeRequest(
        execution_id="synthetic-dev-intent", resolved=resolve_model(provider, "fast"),
        provider_session_id=None, prompt="synthetic check", cwd=tmp_path,
        timeout_seconds=5, on_session_started=sessions.append, on_input_ready=senders.append,
    )
    lifecycle = _ClaudeLifecycle(None, provider)
    stream = ClaudeInputStream(request, lifecycle)
    wire = Wire()
    stream.connect(wire)
    root = wire.messages[0]["uuid"]
    stream.consume(json.dumps({"type": "system", "subtype": "dev_intent",
                               "kind": "android_app", "trigger": "project_scan"}))
    assert not sessions and not senders and not wire.closed
    assert stream.session_id is None
    command(stream, root, "queued")
    command(stream, root, "started")
    emit(stream, type="system", subtype="init", model="synthetic-model")
    assert sessions == [SESSION] and len(senders) == 1
    result(stream, root)
    assert not wire.closed
    command(stream, root, "completed")
    stream.finish()
    assert wire.closed
    assert lifecycle.finish() == ("findings", SESSION, "synthetic-model")
