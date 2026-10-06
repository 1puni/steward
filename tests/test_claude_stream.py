"""Native Claude command identity fences folded and queued input acceptance."""
import json
from types import SimpleNamespace

import pytest

from steward_harness.runtime.contracts import (
    NativeInputClosed, RuntimeExecutionError, RuntimeInput, RuntimeRequest, RuntimeUnavailable,
    resolve_model,
)
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


def notice(stream, task="a6f4db4b8a5408eb"):
    emit(stream, type="system", subtype="task_notification", task_id=task, status="completed")


def unattributed(stream, text):
    emit(stream, type="result", subtype="success", terminal_reason="completed", result=text, usage={})


def test_background_notice_turn_after_the_offered_result_continues_the_execution(tmp_path):
    # gg, 2026-09-27: the parent's turn ended while its background agent ran.
    # The queue closed, a late offer reply was refused, and the agent's notice
    # opened a native turn whose result named nothing the controller offered.
    stream, wire, root, receipts = start(tmp_path)
    result(stream, root, text="first account")
    command(stream, root, "completed")
    assert wire.closed
    with pytest.raises(NativeInputClosed):
        stream.send(RuntimeInput("late", "reply to a late offer"))
    notice(stream)
    emit(stream, type="assistant", message={"content": [{"type": "text", "text": "Folding in the agent."}]})
    unattributed(stream, "final account")
    stream.finish()
    assert stream.lifecycle.finish()[0] == "final account"
    assert [(r.source_id, r.disposition) for r in receipts] == [("late", "rejected")]


def test_second_result_needs_a_background_notice_since_the_last(tmp_path):
    stream, _, root, _ = start(tmp_path)
    notice(stream)  # during the parent's own turn: its result consumes it
    result(stream, root)
    command(stream, root, "completed")
    with pytest.raises(RuntimeExecutionError, match="no offered command identity"):
        unattributed(stream, "stray")


def test_background_notice_cannot_repeat_an_offered_result(tmp_path):
    stream, _, root, _ = start(tmp_path)
    result(stream, root)
    command(stream, root, "completed")
    notice(stream)
    with pytest.raises(RuntimeExecutionError, match="no offered command identity"):
        result(stream, root, text="again")


def test_background_notice_cannot_complete_an_unfinished_offered_command(tmp_path):
    stream, _, _, _ = start(tmp_path)
    notice(stream)
    with pytest.raises(RuntimeExecutionError, match="no offered command identity"):
        result(stream, "33333333-3333-4333-8333-333333333333")


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


def progress_stream(tmp_path, sink):
    """A started stream whose lifecycle narrates activity into `sink`."""
    request = RuntimeRequest(
        execution_id="native", resolved=resolve_model("glm", "fast"),
        provider_session_id=None, prompt="work", cwd=tmp_path, timeout_seconds=5,
        on_progress=sink,
    )
    lifecycle = _ClaudeLifecycle(None, "glm", on_progress=request.on_progress)
    stream = ClaudeInputStream(request, lifecycle)
    wire = Wire()
    stream.connect(wire)
    root = wire.messages[-1]["uuid"]
    command(stream, root, "queued")
    command(stream, root, "started")
    emit(stream, type="system", subtype="init", model="glm")
    return stream, root


def assistant(stream, *content):
    emit(stream, type="assistant", parent_tool_use_id=None,
         message={"role": "assistant", "content": list(content)})


def test_tool_use_is_narrated_with_its_most_telling_argument(tmp_path):
    seen = []
    stream, _root = progress_stream(tmp_path, seen.append)

    assistant(
        stream,
        {"type": "text", "text": "let me look"},
        {"type": "tool_use", "name": "WebSearch", "input": {"query": "bitcoin price"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/notes.md"}},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
    )

    assert seen == [
        "WebSearch bitcoin price",
        "Read /tmp/notes.md",
        "Bash ls -la",
    ]


def test_a_nameless_or_argumentless_tool_still_narrates_safely(tmp_path):
    seen = []
    stream, _root = progress_stream(tmp_path, seen.append)

    assistant(
        stream,
        {"type": "tool_use", "input": {"query": "no name"}},
        {"type": "tool_use", "name": "Glob", "input": {}},
        {"type": "tool_use", "name": "Task", "input": "not-a-mapping"},
    )

    assert seen == ["Glob", "Task"]


def test_a_raising_consumer_cannot_break_the_stream(tmp_path):
    """Narration is advisory: a broken consumer must not fail a healthy turn."""
    def explode(_activity):
        raise RuntimeError("consumer is broken")

    stream, root = progress_stream(tmp_path, explode)

    assistant(stream, {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}})
    result(stream, root, text="done")
    command(stream, root, "completed")

    assert stream.lifecycle.finish()[0] == "done"


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


CLOSURE = "done\nCOMMIT: map the landscape\nDISPOSITION: idle\nQUESTION: NONE"


@pytest.mark.parametrize("provider", ["claude", "glm"])
def test_answer_to_a_receipt_after_the_closure_keeps_the_closure(tmp_path, provider):
    # gg, 2026-09-25, task-d2b1703a: the session closed with valid lines, the
    # understanding acceptance queued behind that result, and the session's
    # "Acknowledged" became the output. Five retries repeated it exactly.
    stream, wire, root, _ = start(tmp_path, provider)
    result(stream, root, text=CLOSURE)
    stream.send(RuntimeInput("understanding-abc-0", "Steward accepted understanding offer abc",
                             origin="controller", author="steward", receipt=True))
    ack = wire.messages[-1]["uuid"]
    command(stream, root, "completed")
    for state in ("queued", "started"):
        command(stream, ack, state)
    result(stream, ack, text="Acknowledged: offer abc is accepted. Nothing further is owed.")
    command(stream, ack, "completed")
    assert wire.closed
    stream.finish()
    assert stream.lifecycle.finish()[0] == CLOSURE


def test_empty_answer_to_a_receipt_is_not_a_missing_response(tmp_path):
    stream, wire, root, _ = start(tmp_path)
    result(stream, root, text=CLOSURE)
    stream.send(RuntimeInput("understanding-abc-0", "accepted", origin="controller",
                             author="steward", receipt=True))
    ack = wire.messages[-1]["uuid"]
    command(stream, root, "completed")
    for state in ("queued", "started"):
        command(stream, ack, state)
    result(stream, ack, text="")
    command(stream, ack, "completed")
    stream.finish()
    assert stream.lifecycle.finish()[0] == CLOSURE


def test_an_ordinary_late_input_still_owns_the_final_word(tmp_path):
    # Only a receipt keeps the earlier word: a note or correction the session
    # acted on is the execution going on, and its result is the latest.
    stream, wire, root, _ = start(tmp_path)
    result(stream, root, text=CLOSURE)
    stream.send(RuntimeInput("note:1", "also check the tests", origin="controller", author="harness"))
    late = wire.messages[-1]["uuid"]
    command(stream, root, "completed")
    for state in ("queued", "started"):
        command(stream, late, state)
    result(stream, late, text="checked\nCOMMIT: check tests\nDISPOSITION: idle\nQUESTION: NONE")
    command(stream, late, "completed")
    stream.finish()
    assert stream.lifecycle.finish()[0].startswith("checked")


def test_only_a_controller_notice_can_be_a_receipt():
    with pytest.raises(ValueError, match="receipt"):
        RuntimeInput("x", "y", receipt=True)


def partial(stream, native, agent=None):
    emit(stream, type="stream_event", parent_tool_use_id=agent, event=native)


def test_partial_messages_meter_final_output_per_message_and_agent(tmp_path):
    stream, _wire, root, _ = start(tmp_path)
    stream.meter.budget = 1000
    for identity, agent, used in (("msg_a", None, 400), ("msg_b", "toolu_1", 300), ("msg_c", None, 299)):
        partial(stream, {"type": "message_start", "message": {"id": identity, "usage": {"output_tokens": 4}}}, agent)
        partial(stream, {"type": "message_delta", "usage": {"output_tokens": used}}, agent)
    # The complete event repeats starting usage and must not be counted.
    emit(stream, type="assistant", parent_tool_use_id=None,
         message={"content": [{"type": "text", "text": "x"}], "usage": {"output_tokens": 4}})
    assert stream.meter.used == 999 and stream.meter.exhausted() is None
    partial(stream, {"type": "message_delta", "usage": {"output_tokens": 300}})
    assert "1000 output tokens of its 1000 budget" in stream.meter.exhausted()
    result(stream, root)
    command(stream, root, "completed")
    stream.finish()


def test_partial_message_from_another_session_is_refused(tmp_path):
    stream, *_ = start(tmp_path)
    with pytest.raises(RuntimeExecutionError, match="changed session identity"):
        stream.consume(json.dumps({"type": "stream_event", "session_id": "22222222-2222-4222-8222-222222222222",
                                   "event": {"type": "message_start", "message": {"id": "m"}}}))


def test_only_a_budgeted_request_asks_for_partial_messages(tmp_path):
    from steward_harness.runtime.providers.claude import ClaudeRuntime
    adapter = ClaudeRuntime.__new__(ClaudeRuntime)
    adapter._controller = SimpleNamespace(broker=SimpleNamespace(enabled=False))
    adapter.executable, adapter.family = "claude", "claude"
    def request(budget):
        return RuntimeRequest(execution_id="x", resolved=resolve_model("claude", "fast"),
                              provider_session_id=None, prompt="p", cwd=tmp_path,
                              timeout_seconds=5, token_budget=budget)
    assert "--include-partial-messages" not in adapter._command(request(None), None)
    assert "--include-partial-messages" in adapter._command(request(5000), None)

def bare(tmp_path, provider="glm"):
    """A CLI that predates the native command queue: no command_lifecycle at all."""
    request = RuntimeRequest(
        execution_id="native", resolved=resolve_model(provider, "fast"),
        provider_session_id=None, prompt="work", cwd=tmp_path, timeout_seconds=5,
        on_input_ready=None, on_input_result=lambda _result: None,
    )
    stream = ClaudeInputStream(request, _ClaudeLifecycle(None, provider))
    stream.connect(Wire())
    return stream


def test_a_cli_without_the_native_command_queue_is_named_as_too_old(tmp_path):
    stream = bare(tmp_path)
    emit(stream, type="system", subtype="init", model="glm")
    with pytest.raises(RuntimeExecutionError, match="predates the native command queue"):
        emit(stream, type="result", subtype="success", result="GLM_OK", usage={})


def test_a_failed_turn_reports_its_own_cause_before_any_identity(tmp_path):
    stream = bare(tmp_path)
    with pytest.raises(RuntimeExecutionError, match="sandbox is unavailable") as caught:
        emit(stream, type="result", subtype="error_during_execution", is_error=True,
             errors=["sandbox is unavailable: bwrap: No permissions to create new namespace"])
    assert "command identity" not in str(caught.value)


def test_a_declined_turn_still_reaches_a_fallback_without_command_identity(tmp_path):
    stream = bare(tmp_path, "claude")
    emit(stream, type="system", subtype="init", model="claude")
    with pytest.raises(RuntimeUnavailable):
        emit(stream, type="result", subtype="success", is_error=True,
             result="Failed to authenticate: OAuth session expired and could not be refreshed")
