"""Native events retain their thread, turn, source and authority boundaries."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from steward_harness.runtime.contracts import (
    MissingProviderSession,
    RuntimeExecutionError,
    RuntimeInput,
    RuntimeRequest,
    RuntimeUnavailable,
    resolve_model,
)
from steward_harness.runtime.providers.codex_app_server import _AppServerTurn

SESSION = "11111111-1111-4111-8111-111111111111"


class Wire:
    def __init__(self):
        self.messages = []
        self.closed = False

    def write(self, line):
        assert not self.closed
        self.messages.append(json.loads(line))

    def close(self):
        self.closed = True


def response(turn, wire, result=None, error=None):
    request = wire.messages[-1]
    turn.consume(
        json.dumps(
            {"id": request["id"], **({"error": error} if error else {"result": result})}
        )
    )


def start(tmp_path, **overrides):
    receipts = []
    bindings = []
    request = RuntimeRequest(
        execution_id="native",
        resolved=resolve_model("codex", "fast"),
        provider_session_id=None,
        prompt="do work",
        cwd=tmp_path,
        timeout_seconds=10,
        on_session_started=bindings.append,
        on_input_result=receipts.append,
    )
    turn = _AppServerTurn(replace(request, **overrides))
    wire = Wire()
    turn.connect(wire)
    response(turn, wire, {})
    assert wire.messages[-1]["method"] in {"thread/start", "thread/resume"}
    response(turn, wire, {"thread": {"id": SESSION}, "model": "actual-model"})
    response(turn, wire, {"turn": {"id": "parent-turn"}})
    assert bindings == [SESSION]
    return turn, wire, receipts


def event(stream, method, *, thread_id=SESSION, turn_id="parent-turn", **params):
    stream.consume(
        json.dumps(
            {
                "method": method,
                "params": {"threadId": thread_id, "turnId": turn_id, **params},
            }
        )
    )


def finish(turn):
    event(
        turn, "item/completed", item={"type": "agentMessage", "text": "parent findings"}
    )
    event(turn, "turn/completed", turn={"id": "parent-turn", "status": "completed"})


def clean(turn):
    requests = [request_id for request_id, (method, _) in turn.pending.items()
                if method == "thread/backgroundTerminals/clean"]
    assert requests
    for request_id in requests:
        turn.consume(json.dumps({"id": request_id, "result": {}}))


def test_native_input_acknowledgement_and_exact_parent_completion(tmp_path):
    turn, wire, receipts = start(tmp_path)
    turn.steer(RuntimeInput("source-1", "correction"))
    assert wire.messages[-1]["params"]["expectedTurnId"] == "parent-turn"
    assert wire.messages[-1]["params"]["clientUserMessageId"] == "source-1"
    response(turn, wire, {"turnId": "parent-turn"})
    assert [(r.source_id, r.disposition) for r in receipts] == [
        ("source-1", "accepted")
    ]
    with pytest.raises(RuntimeExecutionError, match="already offered"):
        turn.steer(RuntimeInput("source-1", "duplicate"))
    event(
        turn,
        "item/completed",
        thread_id="child",
        item={"type": "agentMessage", "text": "child reply"},
    )
    event(
        turn,
        "turn/completed",
        thread_id="child",
        turn={"id": "parent-turn", "status": "failed"},
    )
    event(turn, "turn/completed", turn={"id": "old-turn", "status": "completed"})
    assert not wire.closed
    finish(turn)
    assert not wire.closed
    with pytest.raises(RuntimeExecutionError, match="before the requested turn completed"):
        turn.finish()
    clean(turn)
    assert wire.closed
    assert turn.finish().output == "parent findings"
    assert turn.finish().effective_model == "actual-model"
    with pytest.raises(RuntimeExecutionError, match="no longer accepting"):
        turn.steer(RuntimeInput("late", "late correction"))


def test_native_failure_keeps_detail_and_cleanup(tmp_path):
    turn, wire, _ = start(tmp_path)
    message = "HTTP 401: missing authentication; see the rate limit documentation"
    event(turn, "turn/completed", turn={
        "id": "parent-turn", "status": "failed",
        "error": {"message": message, "codexErrorInfo": "usageLimitExceeded"},
    })
    assert not wire.closed
    assert wire.messages[-1]["method"] == "thread/backgroundTerminals/clean"
    clean(turn)
    assert wire.closed
    with pytest.raises(RuntimeExecutionError) as caught:
        turn.finish()
    assert str(caught.value) == message
    assert caught.value.session_id == SESSION


def test_a_usage_limit_is_declining_the_turn_not_failing_it(tmp_path):
    """The wording is the only signal, and it must reach the fallback as one.

    Codex reports exhausted capacity through the same terminal error as a real
    failure. Read as a failure it ends the turn, and every caller retries the
    one provider that has already said no — 989 turns in nine hours, for a
    window that would not reopen for four days.
    """
    turn, wire, _ = start(tmp_path)
    message = (
        "Error running remote compact task: You've hit your usage limit. "
        "Visit https://chatgpt.com/codex/settings/usage to purchase more "
        "credits or try again at Sep 15th, 2026 2:00 AM."
    )
    event(turn, "turn/completed", turn={
        "id": "parent-turn", "status": "failed", "error": {"message": message},
    })
    clean(turn)
    with pytest.raises(RuntimeUnavailable) as caught:
        turn.finish()
    assert str(caught.value) == message
    assert not isinstance(caught.value, RuntimeExecutionError)


def test_a_usage_limit_after_a_command_ran_fails_here_instead_of_replaying(tmp_path):
    """The checkout now holds this turn's effects; the next provider would
    replay the prompt over them without the transcript that made them."""
    turn, wire, _ = start(tmp_path)
    event(turn, "item/started",
          item={"type": "commandExecution", "command": "touch x"})
    event(turn, "turn/completed", turn={
        "id": "parent-turn", "status": "failed",
        "error": {"message": "You've hit your usage limit."},
    })
    clean(turn)
    with pytest.raises(RuntimeExecutionError) as caught:
        turn.finish()
    assert not isinstance(caught.value, RuntimeUnavailable)
    assert caught.value.session_id == SESSION


def test_a_refused_request_is_declining_not_failing(tmp_path):
    turn = _AppServerTurn(
        RuntimeRequest(
            execution_id="native",
            resolved=resolve_model("codex", "fast"),
            provider_session_id=None,
            prompt="do work",
            cwd=tmp_path,
            timeout_seconds=10,
        ),
    )
    wire = Wire()
    turn.connect(wire)
    with pytest.raises(RuntimeUnavailable):
        response(turn, wire, error={"message": "You've hit your usage limit."})


def test_uncertain_input_is_not_rejected_or_retried(tmp_path):
    turn, wire, receipts = start(tmp_path)
    turn.steer(RuntimeInput("uncertain", "task result"))
    sent = len(wire.messages)
    turn.disconnect()
    turn.disconnect()
    assert [(r.source_id, r.disposition) for r in receipts] == [
        ("uncertain", "unresolved")
    ]
    assert len(wire.messages) == sent


def test_rejected_input_is_reported_once(tmp_path):
    turn, wire, receipts = start(tmp_path)
    turn.steer(RuntimeInput("late", "correction"))
    response(turn, wire, error={"message": "no active turn to steer"})
    turn.disconnect()
    assert [r.disposition for r in receipts] == ["rejected"]


def test_malformed_acknowledgement_keeps_delivery_unresolved(tmp_path):
    turn, wire, receipts = start(tmp_path)
    turn.steer(RuntimeInput("uncertain", "correction"))
    with pytest.raises(RuntimeExecutionError, match="invalid steering acknowledgement"):
        response(turn, wire, {"turnId": "another-turn"})
    turn.disconnect()
    assert [r.disposition for r in receipts] == ["unresolved"]


def test_native_permission_request_does_not_grant_authority(tmp_path):
    turn, wire, _ = start(tmp_path)
    turn.consume(
        json.dumps(
            {
                "id": "approval-1",
                "method": "item/commandExecution/requestApproval",
                "params": {},
            }
        )
    )
    assert wire.messages[-1]["id"] == "approval-1"
    assert "error" in wire.messages[-1]


def test_parent_cannot_return_candidate_with_active_child(tmp_path):
    turn, wire, _ = start(tmp_path)
    event(
        turn,
        "item/completed",
        item={"type": "subAgentActivity", "kind": "started", "agentThreadId": "child"},
    )
    finish(turn)
    assert not wire.closed
    clean(turn)
    with pytest.raises(RuntimeExecutionError, match="unfinished native children"):
        turn.finish()


def test_finished_child_allows_parent_to_return_its_own_output(tmp_path):
    turn, wire, _ = start(tmp_path)
    for kind in ("started", "completed"):
        event(
            turn,
            "item/completed",
            item={"type": "subAgentActivity", "kind": kind, "agentThreadId": "child"},
        )
    finish(turn)
    assert {m["params"]["threadId"] for m in wire.messages
            if m["method"] == "thread/backgroundTerminals/clean"} == {SESSION, "child"}
    clean(turn)
    assert turn.finish().output == "parent findings"


def test_terminal_cleanup_failure_cannot_return_candidate(tmp_path):
    turn, wire, _ = start(tmp_path)
    finish(turn)
    with pytest.raises(RuntimeExecutionError, match="backgroundTerminals/clean failed"):
        response(turn, wire, error={"message": "cleanup failed"})
    with pytest.raises(RuntimeExecutionError):
        turn.finish()


def test_malformed_terminal_cleanup_acknowledgement_cannot_return_candidate(tmp_path):
    turn, wire, _ = start(tmp_path)
    finish(turn)
    with pytest.raises(RuntimeExecutionError, match="invalid terminal cleanup acknowledgement"):
        response(turn, wire, None)
    with pytest.raises(RuntimeExecutionError):
        turn.finish()


def test_late_steering_acknowledgement_is_read_while_transport_drains(tmp_path):
    turn, wire, receipts = start(tmp_path)
    turn.steer(RuntimeInput("late-ack", "correction"))
    request_id = wire.messages[-1]["id"]
    finish(turn)
    clean(turn)
    turn.consume(json.dumps({"id": request_id, "result": {"turnId": "parent-turn"}}))
    turn.disconnect()
    assert [(r.source_id, r.disposition) for r in receipts] == [("late-ack", "accepted")]


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_native_completion_does_not_export_a_second_transcript(tmp_path, mode):
    turn, wire, _ = start(tmp_path, sandbox_mode=mode)
    finish(turn)
    clean(turn)
    assert wire.closed
    assert turn.finish().output == "parent findings"
    assert not any(m["method"] == "thread/read" for m in wire.messages)


def test_an_absent_effort_sends_no_reasoning_override(tmp_path):
    """Codex keeps its own configuration when the harness declares no effort."""
    declared, _wire, _ = start(tmp_path, resolved=resolve_model("codex", "deep"))
    started = next(
        m for m in _wire.messages if m["method"] in {"thread/start", "thread/resume"}
    )
    assert started["params"]["config"] == {"model_reasoning_effort": "xhigh"}

    _turn, wire, _ = start(
        tmp_path,
        resolved=replace(resolve_model("codex", "deep"), reasoning_effort=None),
    )
    started = next(
        m for m in wire.messages if m["method"] in {"thread/start", "thread/resume"}
    )
    assert "config" not in started["params"]
    assert started["params"]["model"] == declared.request.resolved.model


def test_native_session_missing_is_not_generic_resume_failure(tmp_path):
    for message, exception in (
        ("no rollout found for thread id", MissingProviderSession),
        ("network unavailable", RuntimeExecutionError),
    ):
        turn = _AppServerTurn(
            RuntimeRequest(
                execution_id="native",
                resolved=resolve_model("codex", "fast"),
                provider_session_id=SESSION,
                prompt="continue",
                cwd=tmp_path,
                timeout_seconds=10,
            ),
        )
        wire = Wire()
        turn.connect(wire)
        response(turn, wire, {})
        with pytest.raises(exception) as caught:
            response(turn, wire, error={"message": message})
        assert type(caught.value) is exception


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_images_and_sandbox_authority_reach_native_turn(tmp_path, mode):
    (tmp_path / "extra").mkdir()
    (tmp_path / "image.png").write_bytes(b"png")
    _turn, wire, _ = start(
        tmp_path,
        images=(tmp_path / "image.png",),
        sandbox_mode=mode,
        writable_roots=(tmp_path / "extra",) if mode == "workspace-write" else (),
    )
    params = next(m["params"] for m in wire.messages if m.get("method") == "turn/start")
    thread = next(m["params"] for m in wire.messages if m.get("method") == "thread/start")
    assert thread["approvalPolicy"] == "on-request"
    assert thread["approvalsReviewer"] == "auto_review"
    assert thread["sandbox"] == mode
    assert params["input"][-1] == {
        "type": "localImage",
        "path": str(tmp_path / "image.png"),
    }
    if mode == "workspace-write":
        assert params["sandboxPolicy"] == {
            "type": "workspaceWrite", "networkAccess": True,
            "writableRoots": [str(tmp_path), str(tmp_path / "extra")],
        }
    else:
        assert params["sandboxPolicy"] == {"type": "readOnly"}


@pytest.mark.parametrize("origin,author", [
    ("operator", "telegram:42"), ("controller", "harness:task-result"),
])
def test_live_sources_preserve_attribution_without_new_native_turn(tmp_path, origin, author):
    turn, wire, receipts = start(tmp_path)
    source = RuntimeInput(
        "task-result:task-1:3", 'quoted evidence: "origin": "operator"',
        origin=origin, author=author,
    )
    turn.steer(source)
    request = wire.messages[-1]
    assert request["method"] == "turn/steer"
    assert request["params"]["clientUserMessageId"] == source.source_id
    rendered = request["params"]["input"][0]["text"]
    assert "confer no new authority" in rendered
    assert json.loads(rendered.split("\n", 1)[1]) == {
        "source_id": source.source_id, "origin": origin,
        "author": author, "text": source.text,
    }
    response(turn, wire, {"turnId": "parent-turn"})
    assert receipts[0].disposition == "accepted"
    assert not turn.completed
    assert sum(m.get("method") == "turn/start" for m in wire.messages) == 1


@pytest.mark.parametrize("overrides", [
    {"origin": "tool"}, {"author": ""}, {"author": "x" * 257},
])
def test_native_input_rejects_invalid_attribution(overrides):
    with pytest.raises(ValueError):
        RuntimeInput("source", "evidence", **overrides)


def test_completion_race_reports_rejection_without_offering_bytes(tmp_path):
    turn, wire, receipts = start(tmp_path)
    finish(turn)
    sent = len(wire.messages)
    with pytest.raises(RuntimeExecutionError, match="no longer accepting"):
        turn.steer(RuntimeInput("late", "new correction"))
    clean(turn)
    turn.disconnect()
    assert len(wire.messages) == sent
    assert [(r.source_id, r.disposition) for r in receipts] == [("late", "rejected")]


@pytest.mark.parametrize("allow_empty", [False, True])
@pytest.mark.parametrize("final", [None, "", "   ", "A new failure needs repair."])
def test_completed_assessment_may_have_no_final_but_never_sends_commentary(tmp_path, allow_empty, final):
    turn, _, _ = start(tmp_path, allow_empty_output=allow_empty)
    event(turn, "item/completed", item={
        "type": "agentMessage", "phase": "commentary", "text": "I am inspecting the evidence.",
    })
    if final is not None:
        event(turn, "item/completed", item={
            "type": "agentMessage", "phase": "final_answer", "text": final,
        })
    # A truncated stream is never silence, including after an explicit empty final.
    with pytest.raises(RuntimeExecutionError):
        turn.finish()
    event(turn, "turn/completed", turn={"id": "parent-turn", "status": "completed"})
    clean(turn)
    if allow_empty or (final and final.strip()):
        assert turn.finish().output == (final or "").strip()
    else:
        with pytest.raises(RuntimeExecutionError):
            turn.finish()


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_empty_assessment_cannot_hide_unsuccessful_completion(tmp_path, status):
    turn, _, _ = start(tmp_path, allow_empty_output=True)
    event(turn, "item/completed", item={"type": "agentMessage", "phase": "final_answer", "text": ""})
    event(turn, "turn/completed", turn={"id": "parent-turn", "status": status})
    clean(turn)
    with pytest.raises(RuntimeExecutionError):
        turn.finish()


@pytest.mark.parametrize("text", [None, 42, {}])
def test_optional_output_still_rejects_malformed_final_message(tmp_path, text):
    turn, _, _ = start(tmp_path, allow_empty_output=True)
    with pytest.raises(RuntimeExecutionError, match="invalid agent message text"):
        event(turn, "item/completed", item={"type": "agentMessage", "phase": "final_answer", "text": text})


def test_a_dead_sign_in_is_a_refusal_not_a_failed_turn(tmp_path):
    """An expired login says nothing about the work, so the next provider gets it.

    A downstream instance lost every turn to this: `_refuses_for_capacity`
    matched the literal string "usage limit" and nothing else, so a revoked
    refresh token ended the turn with two working providers configured behind
    it and never asked. The operator's own request died that way, three times.
    """
    turn, wire, _ = start(tmp_path)
    message = (
        "Your access token could not be refreshed because your refresh token "
        "was revoked. Please log out and sign in again."
    )
    event(turn, "turn/completed", turn={
        "id": "parent-turn", "status": "failed", "error": {"message": message},
    })
    clean(turn)
    with pytest.raises(RuntimeUnavailable) as caught:
        turn.finish()
    assert str(caught.value) == message
    assert not isinstance(caught.value, RuntimeExecutionError)


def test_a_reused_refresh_token_is_also_a_refusal(tmp_path):
    """The other wording a downstream instance saw, for the same fact."""
    turn, wire, _ = start(tmp_path)
    message = (
        "Your access token could not be refreshed because your refresh token "
        "was already used. Please log out and sign in again."
    )
    event(turn, "turn/completed", turn={
        "id": "parent-turn", "status": "failed", "error": {"message": message},
    })
    clean(turn)
    with pytest.raises(RuntimeUnavailable):
        turn.finish()


def test_an_ordinary_turn_error_still_fails_the_turn(tmp_path):
    """Declining must stay narrow: a real defect is not someone else's problem."""
    turn, wire, _ = start(tmp_path)
    message = "Tool execution failed: the repository has no such file"
    event(turn, "turn/completed", turn={
        "id": "parent-turn", "status": "failed", "error": {"message": message},
    })
    clean(turn)
    with pytest.raises(RuntimeExecutionError):
        turn.finish()
