"""The replacement kernel has one current schema and concrete state shapes."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from state_fixtures import (
    prepare_turn,
    accept_conversation_turn,
    admit_task,
    close_task_slice,
)
from steward_harness.state import (
    SCHEMA_EPOCH,
    CheckpointDisposition,
    ConversationBusy,
    ConversationId,
    OpaqueId,
    StateDatabase,
    TaskId,
    TaskSpec,
    TaskStatus,
    TurnId,
)


DIGEST = "d" * 64
NOW = "2026-09-01T00:00:00+00:00"


def _conversation(
    connection: sqlite3.Connection, transport_key: str = "chat:topic"
) -> tuple[ConversationId, TurnId]:
    conversation_id = ConversationId.for_transport("telegram", transport_key)
    turn_id = TurnId.new()
    connection.execute(
        "INSERT INTO turns("
        "turn_id, conversation_id, source_event_key, operator_id, state, input_text, "
        "provider, generation, provider_session_id, started_at, "
        "completed_at, reply_text) VALUES (?, ?, ?, 'operator', 'completed', 'request', "
        "'codex', 1, 'session', ?, ?, 'done')",
        (str(turn_id), str(conversation_id), str(turn_id), NOW, NOW),
    )
    return conversation_id, turn_id


def test_opaque_ids_are_runtime_distinct_and_validate_their_prefix() -> None:
    turn = TurnId.new()

    assert isinstance(turn, OpaqueId)
    assert type(turn) is TurnId
    assert str(turn).startswith("turn_")
    # `TurnId` is the last `OpaqueId` there is, so the cross-type distinctness
    # half of this went with `DeploymentId`. Prefix validation is what remains
    # and it is the half that keeps a foreign id out.
    with pytest.raises(ValueError, match="invalid TurnId"):
        TurnId("rhythm_" + str(turn).removeprefix("turn_"))


def test_a_conversation_is_named_by_its_owner_not_by_a_minted_id() -> None:
    """Uniqueness by construction: one topic cannot hold two conversations."""
    assert not isinstance(ConversationId.for_task(TaskId("refresh-tokens")), OpaqueId)
    assert not hasattr(ConversationId, "new")
    assert str(ConversationId.for_task(TaskId("refresh-tokens"))) == "task:refresh-tokens"
    assert ConversationId.for_transport("telegram", "10:4") == ConversationId(
        "telegram:10:4"
    )

    topic = ConversationId.for_transport("telegram", "10:4")
    assert (topic.owner_kind, topic.transport, topic.transport_key) == (
        "conversation",
        "telegram",
        "10:4",
    )
    assert ConversationId.for_task(TaskId("refresh-tokens")).owner_kind == "task"

    assert ConversationId("rhythm:dawn").workspace == "rhythm-dawn"
    assert ConversationId("rhythm:dawn").owner_kind == "rhythm"

    for rejected in ("", "telegram:", "topic-7", "mailbox:7", ":7"):
        with pytest.raises(ValueError, match="invalid ConversationId"):
            ConversationId(rejected)


def test_task_identity_is_a_cleartext_slug_not_an_opaque_id() -> None:
    """A task is the one identity an operator types and reads in a branch list."""
    assert not isinstance(TaskId("refresh-oauth-tokens"), OpaqueId)
    assert not hasattr(TaskId, "new")

    # The grammar is what a ref component, a directory and a URL segment share.
    for rejected in ("Refresh", "refresh_tokens", "-refresh", "refresh-", "a--b", ""):
        with pytest.raises(ValueError, match="invalid TaskId"):
            TaskId(rejected)


def test_opening_an_unknown_database_preserves_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE _schema_migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL
        );
        INSERT INTO _schema_migrations VALUES (56, 'historical machinery');
        CREATE TABLE tasks (task_id TEXT PRIMARY KEY, phase TEXT);
        INSERT INTO tasks VALUES ('legacy', 'gating');
        CREATE VIEW legacy_tasks AS SELECT * FROM tasks;
        """
    )
    connection.close()

    with pytest.raises(ValueError, match="database preserved"):
        StateDatabase(path)
    with sqlite3.connect(path) as current:
        assert current.execute("SELECT * FROM tasks").fetchall() == [
            ("legacy", "gating")
        ]
        assert current.execute("SELECT * FROM _schema_migrations").fetchall() == [
            (56, "historical machinery")
        ]


def test_reopening_the_current_epoch_preserves_state(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = StateDatabase(path)
    with first.connect(write=True) as connection:
        assert connection.execute(
            "SELECT epoch FROM steward_schema WHERE singleton=1"
        ).fetchone()[0] == SCHEMA_EPOCH
        conversation_id, _turn_id = _conversation(connection)
    assert path.stat().st_mode & 0o777 == 0o600

    reopened = StateDatabase(path)

    with reopened.connect() as connection:
        assert connection.execute(
            "SELECT epoch FROM steward_schema WHERE singleton=1"
        ).fetchone()[0] == SCHEMA_EPOCH
        stored = connection.execute("SELECT conversation_id FROM turns").fetchone()
    assert stored["conversation_id"] == str(conversation_id)
    assert path.stat().st_mode & 0o777 == 0o600


def test_turn_shape_and_single_running_turn_are_database_invariants(
    tmp_path: Path,
) -> None:
    state = StateDatabase(tmp_path / "state.db")
    with state.connect(write=True) as connection:
        conversation_id = ConversationId.for_transport("desk", "topic")
        connection.execute(
            "INSERT INTO turns("
            "turn_id, conversation_id, source_event_key, operator_id, state, "
            "input_text, started_at) VALUES (?, ?, 'one', 'operator', "
            "'running', 'one', ?)",
            (str(TurnId.new()), str(conversation_id), NOW),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO turns("
                "turn_id, conversation_id, source_event_key, operator_id, state, "
                "input_text, started_at) VALUES (?, ?, 'two', 'operator', "
                "'running', 'two', ?)",
                (str(TurnId.new()), str(conversation_id), NOW),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO turns("
                "turn_id, conversation_id, source_event_key, operator_id, state, "
                "input_text, started_at, completed_at) VALUES "
                "(?, ?, 'bad', 'operator', 'completed', 'bad', ?, ?)",
                (str(TurnId.new()), str(conversation_id), NOW, NOW),
            )


def test_conversation_turns_are_idempotent_and_record_actual_provenance(
    tmp_path: Path,
) -> None:
    state = StateDatabase(tmp_path / "state.db")
    conversation = state.get_or_create_conversation(
        "telegram",
        "10:4",
        provider="claude",
        profile="balanced",
    )
    same = state.get_or_create_conversation(
        "telegram",
        "10:4",
        provider="codex",
        profile="deep",
    )
    turn, created = state.start_turn(
        conversation.conversation_id, "update-1", "operator-7", "Fix it."
    )
    replay, replay_created = state.start_turn(
        conversation.conversation_id, "update-1", "operator-7", "Fix it."
    )

    assert same == conversation
    assert created is True
    assert replay_created is False
    assert replay.turn_id == turn.turn_id

    receipt = accept_conversation_turn(
        state,
        turn.turn_id,
        reply_text="Done.",
        provider="codex",
        model="gpt",
        provider_session_id=None,
    )
    assert receipt["state"] == "completed"
    assert receipt["reply_text"] == "Done."
    assert receipt["output"] == "Done."
    assert receipt["provider"] == "codex"
    assert receipt["model"] == "gpt"
    assert state.active_turn(conversation.conversation_id) is None

    lineage = state.bind_conversation_provider(
        conversation.conversation_id, "claude", None
    )
    assert lineage.provider == "claude"
    assert lineage.generation == 3
    assert lineage.provider_session_id is None


def test_startup_interrupts_only_abandoned_running_turns(tmp_path: Path) -> None:
    state = StateDatabase(tmp_path / "state.db")
    conversation = state.get_or_create_conversation(
        "telegram",
        "10:4",
        provider="codex",
        profile="balanced",
    )
    abandoned, _created = state.start_turn(
        conversation.conversation_id, "update-1", "operator", "First"
    )
    state.bind_conversation_provider(
        conversation.conversation_id, "codex", "uncommitted-session"
    )
    other = state.get_or_create_conversation(
        "desk",
        "thread",
        provider="codex",
        profile="balanced",
    )
    completed, _created = state.start_turn(
        other.conversation_id, "message-1", "operator", "Second"
    )
    accept_conversation_turn(
        state,
        completed.turn_id,
        reply_text="Done",
        provider="codex",
        model="gpt",
        provider_session_id="session",
    )
    mature = state.get_or_create_conversation(
        "telegram",
        "10:5",
        provider="codex",
        profile="balanced",
    )
    mature_first, _created = state.start_turn(
        mature.conversation_id, "update-2", "operator", "Third"
    )
    state.bind_conversation_provider(
        mature.conversation_id, "codex", "mature-session"
    )
    accept_conversation_turn(
        state,
        mature_first.turn_id,
        reply_text="Done",
        provider="codex",
        model="gpt",
        provider_session_id="mature-session",
    )
    mature_abandoned, _created = state.start_turn(
        mature.conversation_id, "update-3", "operator", "Fourth"
    )

    assert state.interrupt_abandoned_turns("controller restarted") == 2
    assert state.interrupt_abandoned_turns("controller restarted") == 0

    with state.connect() as connection:
        abandoned_row = connection.execute(
            "SELECT state, status_reason, completed_at FROM turns WHERE turn_id = ?",
            (str(abandoned.turn_id),),
        ).fetchone()
        completed_row = connection.execute(
            "SELECT state FROM turns WHERE turn_id = ?",
            (str(completed.turn_id),),
        ).fetchone()
        mature_abandoned_row = connection.execute(
            "SELECT state, status_reason FROM turns WHERE turn_id = ?",
            (str(mature_abandoned.turn_id),),
        ).fetchone()
    assert abandoned_row["state"] == "interrupted"
    assert abandoned_row["status_reason"] == "controller restarted"
    assert abandoned_row["completed_at"] is not None
    assert completed_row["state"] == "completed"
    assert state.prepared_turn(str(completed.turn_id))["reply_text"] == "Done"
    interrupted_lineage = state.find_conversation("telegram", "10:4")
    assert interrupted_lineage is not None
    assert interrupted_lineage.generation == 2
    assert interrupted_lineage.provider_session_id is None
    completed_lineage = state.find_conversation("desk", "thread")
    assert completed_lineage is not None
    assert completed_lineage.generation == 1
    assert completed_lineage.provider_session_id == "session"
    assert tuple(mature_abandoned_row) == ("interrupted", "controller restarted")
    mature_lineage = state.find_conversation("telegram", "10:5")
    assert mature_lineage is not None
    assert mature_lineage.generation == 1
    assert mature_lineage.provider_session_id == "mature-session"


def test_tasks_cannot_use_synthetic_or_incomplete_conversation_identity(
    tmp_path: Path,
) -> None:
    for rejected in ("Refresh", "refresh_tokens", "refresh-", "a--b"):
        with pytest.raises(ValueError):
            TaskId(rejected)


def test_git_task_survives_sql_rollback_and_replay_admits_once(
    tmp_path: Path, monkeypatch,
) -> None:
    """The ref is the task; the row is bookkeeping about it, in that order.

    A crash between the two leaves a branch nothing has a row for, and that is
    a legal state — a ref under `refs/heads/tasks/` *is* a task. So what has to
    hold is not "nothing happened" but "an accepted turn never admits twice".
    """
    state = StateDatabase(tmp_path / "state.db")
    conversation = state.get_or_create_conversation(
        "telegram", "topic", provider="claude", profile="balanced",
    )
    turn, _ = state.start_turn(conversation.conversation_id, "request", "operator", "Fix it.")
    event_id = str(turn.turn_id)
    prepare_turn(state, event_id, world_root=None, base_sha=None,
        candidate_sha=None, output="Repair proposed.", provider="claude", model="model",
        provider_session_id=None, profile="balanced",
    )
    spec = TaskSpec("repo", "Repair parser", "Fix the failing parser.")
    insert = state._insert_task

    def fail_after_insert(*args, **kwargs):
        insert(*args, **kwargs)
        raise RuntimeError("crash after task insertion")

    with monkeypatch.context() as fault:
        fault.setattr(state, "_insert_task", fail_after_insert)
        with pytest.raises(RuntimeError, match="crash after task insertion"):
            state.accept_turn(
                event_id, visible_reply="Repair proposed.", spec=spec,
                rejection=None,
            )
    # The branch went first, so it survives the row that did not.
    assert len(state.tasks.all()) == 1
    accepted_id = state.tasks.all()[0].task_id
    assert state.active_turn(conversation.conversation_id) == turn
    assert state.prepared_turn(event_id)["state"] == "running"

    first = state.accept_turn(
        event_id, visible_reply="Repair proposed.", spec=spec, rejection=None,

    )
    replay = state.accept_turn(
        event_id, visible_reply="Changed policy", spec=None, rejection="Now denied",

    )

    # An accepted turn is finished: the replay opens no second branch.
    assert TaskId(first["task_id"]) == accepted_id
    assert dict(replay) == dict(first)
    assert state.active_turn(conversation.conversation_id) is None
    assert len(state.tasks.all()) == 1
    assert state.tasks.read(accepted_id)[1].owner == str(conversation.conversation_id)


def test_task_provider_switch_keeps_task_lineage_and_drops_private_session(
    tmp_path: Path,
) -> None:
    state = StateDatabase(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("repo", "Repair parser", "Fix it."),
        provider="claude",
        profile="balanced",
    )

    session_id = state.tasks.get(admitted.task_id).session_id
    claude = state.bind_conversation_provider(session_id, "claude", "claude-private")
    # A switch is the same bind with nothing left to resume.
    codex = state.bind_conversation_provider(session_id, "codex", None)

    assert claude.generation == 1
    assert claude.provider_session_id == "claude-private"
    assert codex.conversation_id == session_id
    assert codex.owner_kind == "task"
    assert codex.provider == "codex"
    assert codex.generation == 2
    assert codex.provider_session_id is None
    with pytest.raises(ConversationBusy, match="lineage changed"):
        state.bind_conversation_provider(
            session_id, "claude", None, expected_generation=claude.generation
        )
    assert state.get_conversation(session_id) == codex

    state.bind_conversation_provider(session_id, "codex", "codex-stale")
    # An invalidation is that same bind again: this provider has no session.
    invalidated = state.bind_conversation_provider(
        session_id, "codex", None, expected_generation=codex.generation
    )
    replacement = state.bind_conversation_provider(
        session_id, "codex", "codex-replacement",
        expected_generation=invalidated.generation,
    )
    assert invalidated.generation == 3
    assert invalidated.provider_session_id is None
    assert replacement.generation == 3
    assert replacement.provider_session_id == "codex-replacement"
    reopened = StateDatabase(tmp_path / "state.db")
    assert reopened.get_conversation(session_id) == replacement
    assert reopened.get_conversation(reopened.tasks.get(admitted.task_id).session_id).provider_session_id == "codex-replacement"


def test_a_stale_generation_loses_the_lineage_it_thinks_it_owns(
    tmp_path: Path,
) -> None:
    """The fence that stops a rotated session being reinstated by its own turn.

    It was a compare in the `WHERE` of an atomic `UPDATE ... RETURNING`; it is
    a compare-and-swap under the lineage lock now, and it has to hold across a
    reopen because the value is no longer in the database at all.
    """
    state = StateDatabase(tmp_path / "state.db")
    admitted = admit_task(
        state,
        TaskSpec("repo", "Repair parser", "Fix it."),
        provider="claude",
        profile="balanced",
    )
    session_id = state.tasks.get(admitted.task_id).session_id

    # A turn opens on generation 1 and holds it.
    running = state.get_conversation(session_id)
    assert running.generation == 1

    # The operator clears underneath it; the fence moves.
    state.bind_conversation_provider(session_id, "codex", None)
    fenced_out = state.get_conversation(session_id)
    assert fenced_out.generation == 2

    for reader in (state, StateDatabase(tmp_path / "state.db")):
        with pytest.raises(ConversationBusy, match="lineage changed"):
            reader.bind_conversation_provider(
                session_id, "claude", "stale-session",
                expected_generation=running.generation,
            )
        assert reader.get_conversation(session_id) == fenced_out
        assert reader.get_conversation(reader.tasks.get(admitted.task_id).session_id).provider_session_id is None


