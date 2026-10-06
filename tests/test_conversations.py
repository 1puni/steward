"""Focused tests for the replacement ordinary-conversation boundary."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from state_fixtures import (
    admit_task,
    close_task_slice,

)
from steward_harness.conversations import ConversationService
from steward_harness.runtime.contracts import ResolvedModel, RuntimeResult
from steward_harness.state import StateDatabase, TaskId, TaskSpec, TaskStatus


class FakeCognition:
    def __init__(self, replies: list[RuntimeResult | BaseException]) -> None:
        self.replies = replies
        self.requests = []

    def cancel(self, _execution):
        return True

    def run(self, request, *, execution_id=None):
        if callable(request):
            request = request()
        self.requests.append(request)
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, BaseException):
            raise reply
        if reply.provider_session_id is not None:
            request.on_session_started(reply.resolved.provider, reply.provider_session_id)
        return reply


def _task_reply(payload, text="On it."):
    """Exercise the native MCP client and its real controller endpoint."""
    import subprocess
    import sys
    from steward_harness.runtime.task_call_mcp import CLIENT
    def reply(request):
        import os
        request.on_process_started(os.getpid(), None)
        result = subprocess.run([sys.executable, '-c', CLIENT, request.task_call_socket],
            input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                              'params': {'name': 'task', 'arguments': payload}}) + '\n',
            capture_output=True, text=True, check=True)
        receipt = json.loads(json.loads(result.stdout)['result']['content'][0]['text'])
        reply.receipts.append(receipt)
        return _reply(text + "\n" + json.dumps(receipt))
    reply.receipts = []
    return reply


class StartedThenFailedCognition(FakeCognition):
    def __init__(self, session: str = "uncommitted-session") -> None:
        super().__init__([])
        self.session = session

    def run(self, request, *, execution_id=None):
        if callable(request):
            request = request()
        self.requests.append(request)
        request.on_session_started("codex", self.session)
        raise RuntimeError("interrupted after session start")


def _reply(
    text: str,
    *,
    provider: str = "codex",
    session: str = "session-1",
) -> RuntimeResult:
    return RuntimeResult(
        output=text,
        resolved=ResolvedModel(provider, "balanced", "gpt-5.6-sol" if provider == "codex" else "claude-sonnet-4-6", "medium"),
        effective_model="gpt-5.6-sol" if provider == "codex" else "claude-sonnet-4-6",
        provider_session_id=session,
    )


def _service(
    tmp_path: Path,
    cognition: FakeCognition,
    *,
    allowed=("app",),
    delivery_roots: tuple[str, ...] = (),
):
    state = StateDatabase(tmp_path / "state.db")
    state.tasks.repositories = set(allowed)
    return ConversationService(
        state,
        cognition,  # type: ignore[arg-type]
        provider_order=("codex", "claude"),
        profile="balanced",
        workspace=tmp_path,
        timeout_seconds=30,
        delivery_roots=delivery_roots,
    )


def _turn(
    service: ConversationService,
    event: str,
    text: str = "Please help",
    *,
    operator_id: str = "operator",
):
    return service.run_turn(
        transport="telegram",
        transport_key="chat:topic",
        source_event_key=event,
        operator_id=operator_id,
        text=text,
    )


def test_turn_replays_without_reinvoking_cognition(tmp_path: Path) -> None:
    cognition = FakeCognition([_reply("Done")])
    service = _service(tmp_path, cognition)

    first = _turn(service, "update-1")
    replay = _turn(service, "update-1")

    assert replay == first
    assert len(cognition.requests) == 1
    request = cognition.requests[0]
    assert request.provider_order == ("codex", "claude")
    assert "native steward_tasks task tool" in request.prompt
    assert "## Request\nPlease help" in request.prompt


def test_turn_prompt_scopes_delivery_to_telegram(tmp_path: Path) -> None:
    cognition = FakeCognition([_reply("Done"), _reply("Desk reply", session="desk-session")])
    delivery_root = tmp_path / "world" / "artifacts" / "delivery"
    service = _service(
        tmp_path,
        cognition,
        delivery_roots=(str(delivery_root),),
    )

    _turn(service, "update-delivery")

    assert str(delivery_root) in cognition.requests[0].prompt
    service.run_turn(
        transport="desk",
        transport_key="desk-topic",
        source_event_key="desk-delivery",
        operator_id="operator",
        text="Review this",
    )
    desk_prompt = cognition.requests[1].prompt
    assert str(delivery_root) not in desk_prompt
    assert "[[send_image:" not in desk_prompt
    assert "telegram_pin_" not in desk_prompt
    assert "native steward_tasks task tool" in desk_prompt


def test_current_delivery_contract_preserves_an_existing_persistent_session(
    tmp_path: Path,
) -> None:
    cognition = FakeCognition(
        [
            _reply("Before", session="session-before-delivery-contract"),
            _reply("After", session="session-before-delivery-contract"),
        ]
    )
    original = _service(tmp_path, cognition)
    _turn(original, "before-delivery-contract")

    delivery_root = tmp_path / "world" / "artifacts" / "delivery"
    updated = _service(
        tmp_path,
        cognition,
        delivery_roots=(str(delivery_root),),
    )
    _turn(updated, "after-delivery-contract")

    replacement = cognition.requests[1]
    assert replacement.provider_session_id == "session-before-delivery-contract"
    assert updated._state.find_conversation("telegram", "chat:topic").generation == 1
    assert str(delivery_root) in replacement.prompt
    assert "[[send_image:/absolute/path/to/image.png]]" in replacement.prompt


def test_provider_switch_is_fresh_then_resumes_new_provider(tmp_path: Path) -> None:
    cognition = FakeCognition(
        [
            _reply("First", session="codex-private"),
            _reply("Second", provider="claude", session="claude-private"),
            _reply("Third", provider="claude", session="claude-private"),
        ]
    )
    service = _service(tmp_path, cognition)
    first = _turn(service, "update-1")

    service.switch_provider(first.conversation_id, "claude")
    _turn(service, "update-2")
    _turn(service, "update-3")

    switched = cognition.requests[1]
    assert switched.provider_order == ("claude", "codex")
    assert switched.provider_session_id is None
    assert switched.session_provider is None
    assert "native steward_tasks task tool" in switched.prompt
    resumed = cognition.requests[2]
    assert resumed.provider_session_id == "claude-private"
    assert "native steward_tasks task tool" in resumed.prompt
    assert "## Request\nPlease help" in resumed.prompt


def test_missing_session_callbacks_rotate_once_before_binding_replacement(
    tmp_path: Path,
) -> None:
    cognition = FakeCognition([_reply("First", session="stale")])
    service = _service(tmp_path, cognition)
    first = _turn(service, "update-1")
    request = cognition.requests[0]

    request.on_session_invalidated("codex")
    invalidated = service._state.find_conversation("telegram", "chat:topic")
    assert invalidated is not None
    assert invalidated.generation == 2
    assert invalidated.provider_session_id is None

    request.on_session_started("codex", "replacement")
    rebound = service._state.find_conversation("telegram", "chat:topic")
    assert rebound is not None
    assert rebound.conversation_id == first.conversation_id
    assert rebound.generation == 2
    assert rebound.provider_session_id == "replacement"


def test_final_marker_is_refused_without_replay_admission(tmp_path):
    cognition = FakeCognition([_reply('Old reply.\nTASK_PROPOSAL: {"repository":"app","title":"Fix parser","brief":"Repair it."}')])
    service = _service(tmp_path, cognition)
    first = _turn(service, "update-1")
    replay = _turn(service, "update-1")
    assert first == replay
    assert first.task_admission is None
    assert "markers are retired" in first.task_rejection
    assert service._state.tasks.all() == []
    assert len(cognition.requests) == 1


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("TASK_PROPOSAL: not-json", "markers are retired"),
        ("TASK_ACTION: not-json\nMore narration.", "markers are retired"),
        ("TASK_ACTION: {}\nTASK_PROPOSAL: {}", "markers are retired"),
        (
            'TASK_PROPOSAL: {"repository":"other","title":"X","brief":"Y"}',
            "markers are retired",
        ),
    ],
)
def test_malformed_or_unauthorized_marker_admits_nothing(
    tmp_path: Path, marker: str, expected: str
) -> None:
    service = _service(tmp_path, FakeCognition([_reply(f"Answer\n{marker}")]))

    result = _turn(service, "update-1")

    assert result.task_admission is None
    assert result.task_rejection is not None
    assert expected in result.reply_text
    with service._state.connect() as connection:
        assert service._state.tasks.all() == []


def test_failed_cognition_interrupts_the_durable_turn(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeCognition([RuntimeError("offline")]))

    with pytest.raises(RuntimeError, match="offline"):
        _turn(service, "update-1")

    with service._state.connect() as connection:
        row = connection.execute("SELECT state, status_reason FROM turns").fetchone()
    assert tuple(row) == ("interrupted", "offline")
    conversation = service._state.find_conversation("telegram", "chat:topic")
    assert conversation is not None
    assert conversation.generation == 1
    assert conversation.provider_session_id is None


def test_failed_started_session_is_not_resumed_without_an_accepted_turn(
    tmp_path: Path,
) -> None:
    failed = StartedThenFailedCognition()
    service = _service(tmp_path, failed)

    with pytest.raises(RuntimeError, match="interrupted after session start"):
        _turn(service, "update-1")

    interrupted = service._state.find_conversation("telegram", "chat:topic")
    assert interrupted is not None
    assert interrupted.generation == 2
    assert interrupted.provider_session_id is None

    recovered = FakeCognition([_reply("Recovered", session="committed-session")])
    replacement = _service(tmp_path, recovered)
    _turn(replacement, "update-2")

    request = recovered.requests[0]
    assert request.provider_session_id is None
    assert "[[send_image:" in request.prompt
    assert "native steward_tasks task tool" in request.prompt


def test_failed_resumed_turn_preserves_an_accepted_session(
    tmp_path: Path,
) -> None:
    original = _service(
        tmp_path,
        FakeCognition([_reply("Committed", session="committed-session")]),
    )
    _turn(original, "update-1")

    failed = StartedThenFailedCognition(session="committed-session")
    resumed = _service(tmp_path, failed)
    with pytest.raises(RuntimeError, match="interrupted after session start"):
        _turn(resumed, "update-2")

    request = failed.requests[0]
    assert request.provider_session_id == "committed-session"
    assert "[[send_image:" in request.prompt
    conversation = resumed._state.find_conversation("telegram", "chat:topic")
    assert conversation is not None
    assert conversation.generation == 1
    assert conversation.provider_session_id == "committed-session"


def test_a_failed_prompt_build_leaves_the_conversation_able_to_speak_again(
    tmp_path: Path, monkeypatch
) -> None:
    """Building the prompt is inside the turn, so failing it releases the turn.

    `start_turn` writes the `running` row before anything reads the world or
    the task snapshot. A raise in that window used to leave that row forever,
    and the partial `one_running_turn_per_conversation` index then refused
    every later turn on the conversation -- one transient SQLite read failure
    and the operator's steward never answers them again.

    So the assertion that matters is the second turn, not the first
    exception: the first turn is only how the window is entered.
    """
    cognition = FakeCognition([_reply("Recovered")])
    service = _service(tmp_path, cognition)

    def unreadable(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("steward_harness.conversations.build_turn_prompt", unreadable)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        _turn(service, "update-1")

    conversation = service._state.find_conversation("telegram", "chat:topic")
    assert conversation is not None
    assert service._state.active_turn(conversation.conversation_id) is None

    monkeypatch.undo()
    recovered = _turn(service, "update-2")

    assert recovered.reply_text == "Recovered"
    assert len(cognition.requests) == 1


def _waiting_rhythm_task(service: ConversationService) -> TaskId:
    """Admit a task the way a rhythm does, then let its slice stop to ask."""
    task = admit_task(
        service._state,
        TaskSpec("app", "Refresh the index", "The nightly pass found it stale."),
    )
    close_task_slice(service._state, task.task_id, "ask", detail="Which index?")
    return task.task_id


def _answer(task_id: TaskId, text: str):
    return _task_reply({"operation": "answer", "key": "answer", "task_id": str(task_id), "text": text})


def test_an_operator_turn_can_steer_a_task_its_rhythm_created(tmp_path: Path) -> None:
    """A task with no owning conversation answers to whoever is really speaking.

    The lookup matched `conversation_id = ?` and bound the caller's, which for
    a rhythm-origin task is the `NULL` the schema CHECK requires. `= NULL` is
    never true in SQL, so no conversation could steer one — not even by
    passing the same `None` the row holds. Every task a rhythm ever admitted
    was unanswerable, and the operator was told they did not own it.
    """
    cognition = FakeCognition([])
    service = _service(tmp_path, cognition)
    task_id = _waiting_rhythm_task(service)
    cognition.replies.append(_answer(task_id, "The package index."))

    result = _turn(service, "steer-1")

    assert '"accepted": true' in result.reply_text
    assert service._state.tasks.get(task_id).status is TaskStatus.QUEUED


def test_an_automated_result_review_cannot_steer_the_task_it_reviews(
    tmp_path: Path,
) -> None:
    """`harness:task-result` assesses a finished task; assessing is not deciding.

    The review runs `run_turn` on the owning conversation, so nothing about
    the conversation distinguishes it from the operator typing. What does is
    `turns.operator_id`, which the harness already stamps with a `harness:`
    prefix on every turn it starts for itself. Without that gate, opening
    rhythm work to steering would let a steward's own review answer the
    question that review exists to bring to a person.
    """
    cognition = FakeCognition([])
    service = _service(tmp_path, cognition)
    task_id = _waiting_rhythm_task(service)
    cognition.replies.append(_answer(task_id, "Approving my own work."))

    result = _turn(service, "steer-1", operator_id="harness:task-result")

    assert "does not own" in result.reply_text
    # The refusal is the steward's own bookkeeping: the turn keeps it, and the
    # operator, who asked for nothing, is not handed it as news.
    assert "rejected" not in result.reply_text
    assert "Task answered" not in result.reply_text
    assert service._state.tasks.get(task_id).status is TaskStatus.WAITING


def test_an_operator_turn_still_sees_its_own_rejected_action(tmp_path: Path) -> None:
    cognition = FakeCognition([])
    service = _service(tmp_path, cognition)
    cognition.replies.append(_answer("task-" + "0" * 32, "Resume it."))

    result = _turn(service, "steer-2")

    assert '"accepted": false' in result.reply_text


def test_native_owner_generation_survives_restart_fallback_and_clear(tmp_path):
    from steward_harness.cognition import Cognition
    from test_cognition import FakeAdapter

    codex, claude = FakeAdapter('codex'), FakeAdapter('claude')
    service = _service(tmp_path, Cognition({'codex': codex, 'claude': claude}))
    first = _turn(service, 'first')
    owner = str(first.conversation_id)
    assert (codex.requests[-1].native_owner, codex.requests[-1].native_generation) == (owner, 1)
    service = _service(tmp_path, Cognition({'codex': codex, 'claude': claude}))
    _turn(service, 'after-restart')
    assert (codex.requests[-1].native_owner, codex.requests[-1].native_generation) == (owner, 1)
    codex.is_available = False
    _turn(service, 'fallback')
    assert (claude.requests[-1].native_owner, claude.requests[-1].native_generation) == (owner, 2)
    _turn(service, 'resumed-fallback')
    assert claude.requests[-1].native_generation == 2
    service._state.bind_conversation_provider(first.conversation_id, 'claude', None)
    _turn(service, 'after-clear')
    assert (claude.requests[-1].native_owner, claude.requests[-1].native_generation) == (owner, 3)


@pytest.mark.parametrize("selection", [{}, {"desk_provider": "claude", "desk_profile": "fast"}])
def test_desk_policy_selected_before_first_native_call_and_retains_lineage(tmp_path, selection):
    cognition = FakeCognition([_reply("one", provider=selection.get("desk_provider", "codex")),
                               _reply("two", provider=selection.get("desk_provider", "codex"))])
    state = StateDatabase(tmp_path / "state.db")
    service = ConversationService(state, cognition, provider_order=("codex", "claude"),
                                  profile="balanced", workspace=tmp_path, timeout_seconds=30,
                                  **selection)
    for index in range(2):
        service.run_turn(transport="desk", transport_key="123", source_event_key=str(index),
                         operator_id="desk", text="hello")
    assert cognition.requests[0].provider_order[0] == selection.get("desk_provider", "codex")
    assert cognition.requests[0].profile == selection.get("desk_profile", "balanced")
    assert cognition.requests[0].provider_session_id is None
    assert cognition.requests[1].provider_session_id == "session-1"
    other = service.conversation_for("desk", "456")
    assert other.provider_session_id is None
    operator = service.conversation_for("telegram", "7")
    assert (operator.provider, operator.profile) == ("codex", "balanced")


def test_operator_final_delivery_instruction(tmp_path):
    cognition = FakeCognition([_reply('One answer.')])
    service = _service(tmp_path, cognition)
    result = _turn(service, 'delivery')
    assert 'Your final reply is delivered to the operator' in cognition.requests[-1].prompt
    assert 'they will not be sent' not in cognition.requests[-1].prompt
    assert result.transport_reply == 'One answer.'
