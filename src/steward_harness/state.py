"""One current, explicitly upgraded state schema for the steward kernel."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Self


from steward_harness.receipts import write_receipt

if TYPE_CHECKING:
    from steward_harness.config.schema import IncidentPolicy


SCHEMA_EPOCH = 50



_ID_VALUE = re.compile(r"[a-z][a-z0-9_]{1,31}_[0-9a-f]{32}")
# A slug a Git ref component, a directory and a URL segment all accept as-is.
_TASK_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,63}")


class ConversationBusy(RuntimeError):
    """An active turn owns this conversation; retain the incoming observation."""


@dataclass(frozen=True, slots=True)
class Lineage:
    """One conversation's current provider session and the fence around it.

    The generation is a fence, not a counter: it stops a turn that still
    believes it is running from installing a session into a lineage that has
    moved underneath it. It advances whenever what was there stops being
    resumable: the provider changed, or there is no session left to resume.
    """

    provider: str
    profile: str
    generation: int
    provider_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class OpaqueId:
    """Runtime-distinct opaque identity with an operator-readable prefix."""

    prefix: ClassVar[str]
    value: str

    def __post_init__(self) -> None:
        if not _ID_VALUE.fullmatch(self.value) or not self.value.startswith(
            f"{self.prefix}_"
        ):
            raise ValueError(f"invalid {type(self).__name__}: {self.value!r}")

    @classmethod
    def new(cls) -> Self:
        return cls(f"{cls.prefix}_{uuid.uuid4().hex}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ConversationId:
    """A conversation named by its owner: ``<kind>:<ref>``.

    Not an `OpaqueId`. A conversation has exactly one owner — a transport
    thread, a rhythm, a task — which already has a name, so minting a second
    one only created something to keep in step: three `UNIQUE` constraints and
    a polymorphic `CHECK` did nothing but that. A topic cannot have two
    conversations now, because they would be one string.

    The kind is the prefix, so `owner_kind` is a `GLOB` on the id rather than a
    join. The reference may itself contain colons and is never used as a path
    — the world checkpointer hashes a workspace name — so it needs no grammar
    beyond being nonblank.
    """

    value: str

    KINDS: ClassVar[frozenset[str]] = frozenset({"telegram", "desk", "task", "rhythm"})

    def __post_init__(self) -> None:
        kind, _, reference = self.value.partition(":")
        if kind not in self.KINDS or not reference:
            raise ValueError(f"invalid ConversationId: {self.value!r}")

    @classmethod
    def for_transport(cls, transport: str, transport_key: str) -> Self:
        if transport not in {"telegram", "desk"}:
            raise ValueError("conversation transport is invalid")
        if not transport_key.strip():
            raise ValueError("conversation transport key must be nonblank")
        return cls(f"{transport}:{transport_key}")

    @classmethod
    def for_task(cls, task_id: TaskId) -> Self:
        return cls(f"task:{task_id}")

    @property
    def kind(self) -> str:
        return self.value.partition(":")[0]

    @property
    def reference(self) -> str:
        return self.value.partition(":")[2]

    @property
    def owner_kind(self) -> str:
        """Current owners plus retained pre-task rhythm history."""
        return self.kind if self.kind in {"task", "rhythm"} else "conversation"

    @property
    def transport(self) -> str:
        return self.kind if self.owner_kind == "conversation" else ""

    @property
    def transport_key(self) -> str:
        return self.reference if self.owner_kind == "conversation" else ""

    @property
    def workspace(self) -> str:
        """The world checkout this conversation's turns run in, said once."""
        # Historical rhythm turns retain their original workspace identity;
        # new rhythms execute as tasks and never create this owner kind.
        return f"rhythm-{self.reference}" if self.kind == "rhythm" else f"conversation-{self.value}"

    def __str__(self) -> str:
        return self.value




@dataclass(frozen=True, slots=True)
class TurnId(OpaqueId):
    prefix: ClassVar[str] = "turn"


@dataclass(frozen=True, slots=True)
class TaskId:
    """A task's cleartext address: its ref name, branch name and worktree.

    The grammar is the intersection of what a Git ref component, a directory
    name and a URL path segment all accept without quoting. Uniqueness comes
    from the `refs/heads/tasks/<id>` ref, which cannot be created twice.
    """

    value: str

    def __post_init__(self) -> None:
        if not _TASK_SLUG.fullmatch(self.value):
            raise ValueError(f"invalid TaskId: {self.value!r}")

    def __str__(self) -> str:
        return self.value




class TaskStatus(StrEnum):
    PROPOSED = "proposed"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskOriginKind(StrEnum):
    CONVERSATION = "conversation"
    RHYTHM = "rhythm"
    INCIDENT_REPAIR = "incident_repair"
    INCIDENT_ESCALATION = "incident_escalation"


@dataclass(frozen=True, slots=True)
class IncidentId:
    """One failure-to-recovery cycle for a configured pipeline."""

    pipeline: str
    cycle: int

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.pipeline) is None:
            raise ValueError("incident pipeline must be a simple slug")
        if self.cycle < 1:
            raise ValueError("incident cycle must be positive")

    def __str__(self) -> str:
        return f"{self.pipeline}/{self.cycle}"


class IncidentStatus(StrEnum):
    HEALTHY = "healthy"
    FAILING = "failing"
    CONFIRMED = "confirmed"
    ESCALATED = "escalated"


@dataclass(frozen=True, slots=True)
class Incident:
    incident_id: IncidentId
    status: IncidentStatus
    consecutive_failures: int
    transient_rechecks_left: int
    repair_task_id: TaskId | None
    escalation_task_id: TaskId | None
    last_details: str
    last_observed_at: str
    last_recovered_at: str | None


@dataclass(frozen=True, slots=True)
class TaskSpec:
    repository: str
    title: str
    brief: str
    priority: int = 0

    def __post_init__(self) -> None:
        if not 1 <= len(self.repository) <= 128:
            raise ValueError("task repository must be nonblank")
        if not 1 <= len(self.title) <= 256:
            raise ValueError("task title must be nonblank")
        if not 1 <= len(self.brief) <= 8000:
            raise ValueError("task brief must contain between 1 and 8000 characters")
        if not -100 <= self.priority <= 100:
            raise ValueError("task priority is outside the supported range")




@dataclass(frozen=True, slots=True)
class TaskAdmission:
    task_id: TaskId
    created: bool



@dataclass(frozen=True, slots=True)
class Conversation:
    """One conversation's identity and the lineage currently running it.

    Transport, owner kind and rhythm are read off the identity rather than
    stored beside it; they were columns only because the identity used to be
    opaque.
    """

    conversation_id: ConversationId
    provider: str
    profile: str
    generation: int
    provider_session_id: str | None

    @property
    def transport(self) -> str:
        return self.conversation_id.transport

    @property
    def transport_key(self) -> str:
        return self.conversation_id.transport_key

    @property
    def owner_kind(self) -> str:
        return self.conversation_id.owner_kind



@dataclass(frozen=True, slots=True)
class Turn:
    turn_id: TurnId
    conversation_id: ConversationId
    source_event_key: str
    input_text: str
    execution_turn_id: TurnId | None = None
    input_disposition: str | None = None
    state: str | None = None
    status_reason: str | None = None
    started_at: str = ""
    completed_at: str | None = None



class CheckpointDisposition(StrEnum):
    CONTINUE = "continue"
    IDLE = "idle"
    ASK = "ask"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class TaskAction:
    """One bounded steering request against an already-owned task."""

    task_id: TaskId
    kind: Literal["answer", "retry", "note"]
    text: str

    def __post_init__(self) -> None:
        if self.kind not in {"answer", "retry", "note"}:
            raise ValueError("task action must be answer, retry, or note")
        if not self.text.strip() or len(self.text) > 8000:
            raise ValueError("task action text must contain 1 through 8000 characters")


@dataclass(frozen=True, slots=True)
class TaskCheckpointSummary:
    """One retained checkpoint's operator-visible state, newest first."""

    disposition: CheckpointDisposition
    question: str | None
    created_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


_DDL = (
    """
    CREATE TABLE steward_schema (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        epoch INTEGER NOT NULL,
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE turns (
        turn_id TEXT PRIMARY KEY CHECK (
            length(turn_id) = 37
            AND turn_id GLOB 'turn_[0-9a-f]*'
            AND substr(turn_id, 6) NOT GLOB '*[^0-9a-f]*'
        ),
        conversation_id TEXT NOT NULL CHECK (conversation_id GLOB '?*:?*'),
        source_event_key TEXT NOT NULL CHECK (length(source_event_key) > 0),
        operator_id TEXT NOT NULL CHECK (length(operator_id) > 0),
        state TEXT,
        input_text TEXT NOT NULL,
        execution_turn_id TEXT REFERENCES turns(turn_id),
        input_disposition TEXT CHECK (input_disposition IN ('unresolved', 'accepted', 'rejected')),
        status_reason TEXT,
        provider TEXT,
        generation INTEGER CHECK (generation IS NULL OR generation >= 1),
        provider_session_id TEXT,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        -- An execution's custody of its owner's checkout, claimed before the
        -- provider starts; then its retained output, captured world
        -- candidate, and accepted reply and effect. One row is one turn.
        episode_input TEXT,
        world_root TEXT,
        base_sha TEXT,
        output TEXT,
        model TEXT,
        profile TEXT CHECK (profile IS NULL OR profile IN ('fast', 'balanced', 'deep')),
        candidate_sha TEXT,
        reply_text TEXT,
        task_id TEXT,
        rejection TEXT,
        CHECK ((execution_turn_id IS NULL) = (input_disposition IS NULL)),
        CHECK ((state IS 'completed') = (reply_text IS NOT NULL)),
        CHECK (output IS NULL OR episode_input IS NOT NULL),
        FOREIGN KEY (execution_turn_id, conversation_id) REFERENCES turns(turn_id, conversation_id),
        UNIQUE (turn_id, conversation_id),
        UNIQUE (conversation_id, source_event_key),
        CHECK (
            (execution_turn_id IS NOT NULL AND execution_turn_id != turn_id
             AND state IS NULL AND status_reason IS NULL AND completed_at IS NULL
             AND provider IS NULL AND generation IS NULL AND provider_session_id IS NULL)
            OR (execution_turn_id IS NULL AND (
                (state IS 'completed'
                 AND status_reason IS NULL AND provider IS NOT NULL
                 AND generation IS NOT NULL AND completed_at IS NOT NULL)
                OR (state IS 'running'
                 AND status_reason IS NULL
                 AND completed_at IS NULL)
                OR (state IS 'interrupted'
                 AND status_reason IS NOT NULL
                 AND length(status_reason) > 0
                 AND completed_at IS NOT NULL)
            ))
        )
    ) STRICT
    """,
    """
    -- Chat and rhythm only. A task's exclusion is its `flock`, which this
    -- index could never have supplied: it excluded a second *row*, and two
    -- daemons over one state directory would each have taken their own.
    CREATE UNIQUE INDEX one_running_turn_per_conversation
    ON turns(conversation_id) WHERE state = 'running' AND execution_turn_id IS NULL
    """,
    """
    CREATE TABLE incidents (
        pipeline TEXT PRIMARY KEY CHECK (
            length(pipeline) BETWEEN 1 AND 64
            AND pipeline GLOB '[A-Za-z0-9]*'
            AND pipeline NOT GLOB '*[^A-Za-z0-9_-]*'
        ),
        cycle INTEGER NOT NULL CHECK (cycle >= 1),
        status TEXT NOT NULL CHECK (
            status IN ('healthy', 'failing', 'confirmed', 'escalated')
        ),
        consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
        transient_rechecks_left INTEGER NOT NULL CHECK (transient_rechecks_left >= 0),
        repair_task_id TEXT UNIQUE,
        escalation_task_id TEXT UNIQUE,
        last_details TEXT NOT NULL CHECK (length(last_details) BETWEEN 1 AND 1000),
        last_observed_at TEXT NOT NULL,
        last_recovered_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (
            (status = 'healthy' AND consecutive_failures = 0
             AND transient_rechecks_left = 0
             AND repair_task_id IS NULL AND escalation_task_id IS NULL)
            OR
            (status = 'failing' AND consecutive_failures > 0
             AND transient_rechecks_left = 0
             AND repair_task_id IS NULL AND escalation_task_id IS NULL)
            OR
            (status = 'confirmed' AND consecutive_failures > 0
             AND escalation_task_id IS NULL)
            OR
            (status = 'escalated' AND consecutive_failures > 0
             AND transient_rechecks_left = 0 AND repair_task_id IS NULL)
        )
    ) STRICT
    """,
)


#: Which provider session each conversation and task is on.
_LINEAGE_DDL = """
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY CHECK (conversation_id GLOB '?*:?*'),
    provider TEXT NOT NULL CHECK (length(provider) > 0),
    profile TEXT NOT NULL CHECK (length(profile) > 0),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    provider_session_id TEXT CHECK (provider_session_id IS NULL OR length(provider_session_id) > 0)
) STRICT
"""

_DDL = (*_DDL, _LINEAGE_DDL)


class StateDatabase:
    """Own the current schema; preserve incompatible state for an explicit upgrade."""

    def __init__(self, path: str | Path) -> None:
        given = Path(path)
        if given.is_symlink():
            raise ValueError(f"Database path must not be a symlink: {given}")
        self.path = given.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        from steward_harness.task_store import GitTaskStore
        self.tasks = GitTaskStore(self.path.with_name(self.path.name + ".tasks.git"))

    def _raw_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        # Inspect before changing journal mode or schema. Unknown databases stay intact.
        connection = sqlite3.connect(str(self.path), isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            tables = {
                r[0]
                for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if not tables:
                for statement in _DDL:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO steward_schema VALUES (1, ?, ?)",
                    (SCHEMA_EPOCH, _now()),
                )
            else:
                if "steward_schema" not in tables:
                    raise ValueError("Unknown state schema; database preserved")
                row = connection.execute(
                    "SELECT epoch FROM steward_schema WHERE singleton=1"
                ).fetchone()
                epoch = row[0] if row else None
                if epoch != SCHEMA_EPOCH:
                    raise ValueError(
                        f"State schema epoch {epoch} requires explicit upgrade to {SCHEMA_EPOCH}; "
                        "database preserved. Inspect the existing state before restarting."
                    )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ValueError("State has invalid foreign keys; database preserved")
            connection.execute("COMMIT")
            connection.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._raw_connection()
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            else:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
            yield connection
            if write:
                connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    #: Prepared: accepting it needs nothing run again. A world turn also needs
    #: its captured candidate; a worldless one only its retained output.
    _PREPARED = "output IS NOT NULL AND (world_root IS NULL OR candidate_sha IS NOT NULL)"

    def prepared_turn(self, event_id: str) -> sqlite3.Row | None:
        """This turn's receipt once it is prepared, else None."""
        with self.connect() as connection:
            return connection.execute(
                f"SELECT * FROM turns WHERE turn_id=? AND {self._PREPARED}", (event_id,)
            ).fetchone()

    def pending_turns(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                f"SELECT * FROM turns WHERE state != 'completed' AND {self._PREPARED} "
                "ORDER BY started_at, turn_id"
            ).fetchall()

    def claimed_turns(self) -> list[sqlite3.Row]:
        """Executions holding their owner's checkout that are not yet prepared."""
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM turns WHERE episode_input IS NOT NULL AND state != 'completed' "
                f"AND NOT ({self._PREPARED}) ORDER BY started_at, turn_id"
            ).fetchall()

    def claim_turn(self, turn_id: TurnId, *, episode_input: str, world_root: str | None,
                   base_sha: str | None) -> None:
        """Take custody of the owner's checkout before the provider starts.

        A claimed turn fences its conversation until it is prepared and
        accepted, or released by an ordinary provider failure. Startup does
        not interrupt it: its checkout may hold finished work.
        """
        if world_root is not None and not Path(world_root).is_absolute():
            raise ValueError("world root must be absolute")
        with self.connect(write=True) as connection:
            claimed = connection.execute(
                "UPDATE turns SET episode_input=?, world_root=?, base_sha=? "
                "WHERE turn_id=? AND state='running' AND output IS NULL",
                (episode_input, world_root, base_sha, str(turn_id)),
            )
            if claimed.rowcount != 1:
                raise RuntimeError("only a running, unfinished turn can claim its checkout")

    def retain_output(self, turn_id: TurnId, *, output: str, provider: str, model: str,
                      provider_session_id: str | None, profile: str, generation: int) -> None:
        """Keep the provider's completed output before Git can fail capturing it."""
        if not provider.strip() or not model.strip():
            raise ValueError("retained output provenance must be nonblank")
        with self.connect(write=True) as connection:
            retained = connection.execute(
                "UPDATE turns SET output=?, provider=?, model=?, provider_session_id=?, "
                "profile=?, generation=? WHERE turn_id=? AND state='running' "
                "AND episode_input IS NOT NULL AND output IS NULL",
                (output, provider, model, provider_session_id, profile, generation, str(turn_id)),
            )
            if retained.rowcount != 1:
                raise RuntimeError("only a claimed turn without output can retain one")

    def release_claim(self, turn_id: TurnId) -> None:
        """A provider failure with no output leaves its partial work to the next source."""
        with self.connect(write=True) as connection:
            connection.execute(
                "UPDATE turns SET episode_input=NULL, world_root=NULL, "
                "base_sha=NULL WHERE turn_id=? AND output IS NULL", (str(turn_id),),
            )

    def record_candidate(self, event_id: str, candidate_sha: str) -> None:
        """Record the captured world candidate of a turn whose output is retained."""
        if re.fullmatch(r"[0-9a-f]{40,64}", candidate_sha) is None:
            raise ValueError("world candidate is not an exact Git revision")
        with self.connect(write=True) as connection:
            prepared = connection.execute(
                "UPDATE turns SET candidate_sha=? WHERE turn_id=? "
                "AND state='running' AND output IS NOT NULL AND world_root IS NOT NULL",
                (candidate_sha, event_id),
            )
            if prepared.rowcount != 1:
                raise RuntimeError("world turn has no retained output to prepare")

    def accept_turn(
        self,
        event_id: str,
        *,
        visible_reply: str,
        spec: TaskSpec | None,
        rejection: str | None,
        action: TaskAction | None = None,
    ) -> sqlite3.Row:
        """Accept a prepared world turn. A task Git commit is independently durable; source identity makes receipt replay idempotent."""
        with self.connect(write=True) as connection:
            row = connection.execute(
                f"SELECT * FROM turns WHERE turn_id=? AND {self._PREPARED}", (event_id,)
            ).fetchone()
            if row is None:
                raise LookupError("world turn is not prepared")
            if row["state"] == "completed":
                return row
            turn_id = TurnId(event_id)
            owner = ConversationId(row["conversation_id"])
            # There is no cancelled branch here because no path can reach one.
            # `record_candidate` runs after `cognition.run` has returned, and
            # its `finally` has already popped `Cognition._active`, so by the
            # time this row exists the process `/cancel` would signal has
            # exited: cancellation finds no bound conversation and says so.
            if spec is not None and action is not None:
                raise ValueError("a turn may propose one task or steer one task")
            if (spec is not None or action is not None) and rejection is not None:
                raise ValueError("a rejected proposal cannot be admitted")
            if row["state"] != "running":
                raise RuntimeError("turn is not running")
            lineage = self._lineage_in(connection, row["conversation_id"])
            if lineage is None:
                raise RuntimeError("turn has no conversation lineage")
            # A stale completion keeps the generation it ran under; a fresh one
            # takes whatever the bind decides, which is the only writer of it.
            generation = row["generation"]
            if generation == lineage.generation:
                generation = self._bind_in(
                    connection, row["conversation_id"], row["provider"],
                    row["provider_session_id"], expected_generation=generation,
                ).generation
            task_id = None
            if spec is not None:
                # The ref first, then the row: the ref is the task, and a crash
                # between the two leaves a task with no bookkeeping, which is a
                # legal state rather than a corrupt one.
                admission = self._insert_task(
                    connection,
                    spec,
                    task_id=TaskId("task-" + uuid.uuid5(uuid.NAMESPACE_URL, event_id).hex),
                    source=event_id,
                    kind=TaskOriginKind.CONVERSATION,
                    origin_ref=None,
                    conversation_id=owner,
                    provider=row["provider"],
                    profile=row["profile"],
                )
                task_id = str(admission.task_id)
                notice = f"Task admitted: {task_id}"
            elif action is not None:
                notice, rejection = self._apply_task_action(
                    owner, row["operator_id"], action, source=event_id,
                )
                notice = rejection or notice
            else:
                notice = rejection
            reply = (
                f"{visible_reply}\n\n{notice}"
                if notice and visible_reply
                else notice or visible_reply
            )
            connection.execute(
                "UPDATE turns SET state='completed', generation=?, completed_at=?, "
                "reply_text=?, task_id=?, rejection=? WHERE turn_id=?",
                (generation, _now(), reply, task_id, rejection, event_id),
            )
            return connection.execute(
                "SELECT * FROM turns WHERE turn_id=?", (event_id,)
            ).fetchone()

    def _apply_task_action(self, owner, operator_id, action, *, source):
        """Enforce accepted ownership and configured repository authority before committing steering. Automated result assessment cannot grant itself operator authority over unowned work."""
        with self.tasks.lease:
            try:
                if self.tasks.has_source(action.task_id, source):
                    return f"Task {action.kind} already accepted: {action.task_id}.", None
                task = self.tasks.get(action.task_id)
            except PermissionError:
                return None, "Task action rejected: repository work is not authorized."
            except LookupError:
                task = None
            if operator_id == "harness:desk-watch" and action.kind != "note":
                return None, "Task action rejected: a desk observation cannot answer or retry held work."
            if task is None or task.owner not in {None, str(owner)}:
                # A conversation is told the same thing whether the task is
                # absent or someone else's, so the reply cannot be used to
                # learn that a task exists.
                return None, "Task action rejected: this conversation does not own that task."
            if task.owner is None and operator_id.startswith("harness:"):
                return None, (f"Task action rejected: only an operator turn may steer "
                              f"{task.origin_kind.value} work.")
            allowed = {
                "answer": {TaskStatus.WAITING},
                "retry": {TaskStatus.BLOCKED, TaskStatus.CANCELLED},
                "note": {TaskStatus.PROPOSED, TaskStatus.QUEUED, TaskStatus.RUNNING,
                         TaskStatus.BLOCKED, TaskStatus.WAITING},
            }
            if task.status not in allowed[action.kind]:
                return None, (f"Task action rejected: {action.task_id} is {task.status.value}; "
                              f"cannot {action.kind}.")
            if action.kind == "answer":
                self.tasks.answer(action.task_id, action.text, source=source)
                return f"Task answered: {action.task_id}; queued on its retained branch.", None
            if action.kind == "retry":
                self.tasks.retry(action.task_id, action.text, source=source)
                return f"Task retry queued: {action.task_id}; retained branch preserved.", None
            self.tasks.note(action.task_id, action.text, source=source)
            return f"Task note recorded: {action.task_id}; retained session context.", None

    @property
    def _pause_marker(self) -> Path:
        """The pause bit, beside the state it pauses, where an operator looks."""
        return self.path.parent / "paused"


    def paused(self) -> bool:
        """Whether operator-controlled scheduler work is durably paused.

        The fact is one bit, so the file's *existence* is the whole of it, and
        the reason it is a marker rather than a document is that this leaves no
        read-modify-write to serialize: `create` and `unlink` are each one
        atomic directory operation, so concurrent pausers and resumers settle
        exactly as the singleton `UPDATE` did, with no state in between for a
        crash to land in. When it paused is the mtime.
        """
        return self._pause_marker.exists()

    def set_paused(self, paused: bool) -> None:
        self._set_marker(self._pause_marker, paused)

    @staticmethod
    def _set_marker(marker: Path, present: bool) -> None:
        if present:
            # O_CREAT alone: setting an already-set bit is not an error, and
            # re-stamping the mtime would misreport when the operator asked.
            marker.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600))
        else:
            marker.unlink(missing_ok=True)
            if not marker.parent.is_dir():
                return
        # The directory entry is the fact; fsync it, or the crash this guards
        # against is exactly the one that loses it.
        directory = os.open(marker.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @staticmethod
    def _lineage_in(connection: sqlite3.Connection, conversation_id: str) -> Lineage | None:
        row = connection.execute(
            "SELECT provider, profile, generation, provider_session_id FROM conversations "
            "WHERE conversation_id=?", (conversation_id,),
        ).fetchone()
        return None if row is None else Lineage(*row)

    def _open_in(self, connection: sqlite3.Connection, conversation_id: str, *,
                 provider: str, profile: str) -> Lineage:
        """Start a lineage at generation 1; an existing one keeps its provider."""
        if not provider.strip() or not profile.strip():
            raise ValueError("conversation provider and profile must be nonblank")
        connection.execute(
            "INSERT OR IGNORE INTO conversations VALUES (?, ?, ?, 1, NULL)",
            (conversation_id, provider, profile),
        )
        return self._lineage_in(connection, conversation_id)

    def _bind_in(self, connection: sqlite3.Connection, conversation_id: str, provider: str,
                 provider_session_id: str | None, *, expected_generation: int | None = None) -> Lineage:
        """Install the session this conversation is now on, behind the fence."""
        if not provider.strip():
            raise ValueError("conversation provider must be nonblank")
        if provider_session_id is not None and not provider_session_id.strip():
            raise ValueError("conversation session must be nonblank when present")
        current = self._lineage_in(connection, conversation_id)
        if current is None:
            raise LookupError(f"unknown conversation {conversation_id}")
        if expected_generation is not None and current.generation != expected_generation:
            raise ConversationBusy("conversation lineage changed before native session start")
        bound = Lineage(provider, current.profile,
                        current.generation + (provider != current.provider or provider_session_id is None),
                        provider_session_id)
        connection.execute(
            "UPDATE conversations SET provider=?, generation=?, provider_session_id=? "
            "WHERE conversation_id=?",
            (bound.provider, bound.generation, bound.provider_session_id, conversation_id),
        )
        return bound

    def lineage(self, conversation_id: ConversationId) -> Lineage | None:
        with self.connect() as connection:
            return self._lineage_in(connection, str(conversation_id))

    def open_conversation(self, conversation_id: ConversationId, *, provider: str,
                          profile: str) -> Conversation:
        with self.connect(write=True) as connection:
            self._open_in(connection, str(conversation_id), provider=provider, profile=profile)
        return self.get_conversation(conversation_id)

    def get_conversation(self, conversation_id: ConversationId) -> Conversation:
        """Return one declared conversation or scheduled world-session owner."""
        lineage = self.lineage(conversation_id)
        if lineage is None and conversation_id.kind == "task":
            self.tasks.read(TaskId(conversation_id.reference))
            with self.connect(write=True) as connection:
                lineage = self._open_in(connection, str(conversation_id),
                                        provider=self.tasks.default_provider,
                                        profile=self.tasks.default_profile)
        if lineage is None:
            raise LookupError(f"unknown conversation {conversation_id}")
        return Conversation(
            conversation_id,
            lineage.provider,
            lineage.profile,
            lineage.generation,
            lineage.provider_session_id,
        )

    def find_conversation(
        self, transport: str, transport_key: str
    ) -> Conversation | None:
        conversation_id = ConversationId.for_transport(transport, transport_key)
        return None if self.lineage(conversation_id) is None else self.get_conversation(conversation_id)

    def get_or_create_conversation(
        self,
        transport: str,
        transport_key: str,
        *,
        provider: str,
        profile: str,
    ) -> Conversation:
        """Ensure one transport thread, which is its own identity, has a lineage."""
        if profile not in {"fast", "balanced", "deep"}:
            raise ValueError("conversation profile is invalid")
        return self.open_conversation(ConversationId.for_transport(transport, transport_key),
                                      provider=provider, profile=profile)

    def set_conversation_profile(
        self, conversation_id: ConversationId, profile: str
    ) -> Conversation:
        """Change the quality a conversation runs at, which is not a rotation."""
        if profile not in {"fast", "balanced", "deep"}:
            raise ValueError("conversation profile is invalid")
        self.get_conversation(conversation_id)
        with self.connect(write=True) as connection:
            connection.execute("UPDATE conversations SET profile=? WHERE conversation_id=?",
                               (profile, str(conversation_id)))
        return self.get_conversation(conversation_id)

    def bind_conversation_provider(
        self,
        conversation_id: ConversationId,
        provider: str,
        provider_session_id: str | None,
        *,
        expected_generation: int | None = None,
    ) -> Conversation:
        """Persist the session this conversation is now on, behind its fence.

        The one lineage transition. A started session, a fallback to another
        provider, an invalidated session, an operator's provider switch and a
        cleared topic are each this call with a different `(provider, session)`
        and a guard its caller already owns. `None` for the session says there
        is nothing left to resume.
        """
        self.get_conversation(conversation_id)
        with self.connect(write=True) as connection:
            self._bind_in(connection, str(conversation_id), provider, provider_session_id,
                          expected_generation=expected_generation)
        return self.get_conversation(conversation_id)

    def clear_conversation(self, conversation_id: ConversationId) -> Conversation:
        """Rotate one ordinary lineage and interrupt its active, unprepared turn."""
        if conversation_id.owner_kind != "conversation":
            raise LookupError(f"unknown conversation {conversation_id}")
        current = self.get_conversation(conversation_id)
        with self.connect(write=True) as connection:
            self._bind_in(connection, str(conversation_id), current.provider, None)
            # A turn that claimed its checkout keeps it: clearing rotates the
            # session, it does not undo work the turn may have finished.
            # Stopping work is `/cancel`, a signal to the process doing it.
            connection.execute(
                "UPDATE turns SET state = 'interrupted', "
                "status_reason = 'conversation cleared', completed_at = ? "
                "WHERE conversation_id = ? AND state = 'running' AND execution_turn_id IS NULL "
                "AND episode_input IS NULL",
                (_now(), str(conversation_id)),
            )
        return self.get_conversation(conversation_id)

    @staticmethod
    def _turn(row: sqlite3.Row) -> Turn:
        return Turn(
            TurnId(row["turn_id"]),
            ConversationId(row["conversation_id"]),
            row["source_event_key"],
            row["input_text"],
            TurnId(row["execution_turn_id"]) if row["execution_turn_id"] else None,
            row["input_disposition"],
            row["state"],
            row["status_reason"],
            row["started_at"],
            row["completed_at"],
        )

    def get_turn(self, turn_id: TurnId) -> Turn:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM turns WHERE turn_id=?", (str(turn_id),)
            ).fetchone()
        if row is None:
            raise LookupError(f"unknown turn {turn_id}")
        return self._turn(row)

    def turn_for_source(self, owner: ConversationId, source: str) -> Turn | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM turns WHERE conversation_id=? AND source_event_key=?",
                (str(owner), source),
            ).fetchone()
        return self._turn(row) if row is not None else None

    def start_turn(
        self,
        conversation_id: ConversationId,
        source_event_key: str,
        operator_id: str,
        input_text: str,
        execution_turn_id: TurnId | None = None,
    ) -> tuple[Turn, bool]:
        """Start one idempotent turn; replay returns the original row."""
        if not source_event_key.strip() or not operator_id.strip():
            raise ValueError("turn source and operator must be nonblank")
        if not input_text.strip():
            raise ValueError("turn input must be nonblank")
        with self.connect(write=True) as connection:
            return self._start_turn_in(
                connection, conversation_id, source_event_key, operator_id, input_text, execution_turn_id
            )

    def _start_turn_in(
        self,
        connection: sqlite3.Connection,
        conversation_id: ConversationId,
        source_event_key: str,
        operator_id: str,
        input_text: str,
        execution_turn_id: TurnId | None = None,
        *,
        started_at: str | None = None,
    ) -> tuple[Turn, bool]:
        row = connection.execute(
            "SELECT * FROM turns WHERE conversation_id = ? "
            "AND source_event_key = ?",
            (str(conversation_id), source_event_key),
        ).fetchone()
        if row is not None:
            if row["input_text"] != input_text or row["operator_id"] != operator_id:
                raise ValueError("replayed turn identity changed its input")
            if not self._release_rejected_native_input_in(connection, row):
                return self._turn(row), False
        if execution_turn_id is not None:
            # The live lineage is read rather than joined, and the comparison
            # travels into the statement as parameters: the turn side stays
            # atomic, and a lineage that rotates in between only makes this
            # refuse an input the rotation was already going to strand.
            live = self._lineage_in(connection, str(conversation_id))
            parent = live and connection.execute(
                "SELECT 1 FROM turns WHERE turn_id=? AND conversation_id=? "
                "AND state='running' AND execution_turn_id IS NULL "
                "AND generation=? AND provider=? "
                "AND provider_session_id IS NOT NULL AND provider_session_id IS ?",
                (
                    str(execution_turn_id),
                    str(conversation_id),
                    live.generation,
                    live.provider,
                    live.provider_session_id,
                ),
            ).fetchone()
            if parent is None:
                raise ConversationBusy("native execution is no longer accepting input")
            turn_id = TurnId.new()
            connection.execute(
                "INSERT INTO turns(turn_id, conversation_id, source_event_key, operator_id, "
                "input_text, started_at, execution_turn_id, input_disposition) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'unresolved')",
                (str(turn_id), str(conversation_id), source_event_key, operator_id,
                 input_text, started_at or _now(), str(execution_turn_id)),
            )
            return self._turn(connection.execute(
                "SELECT * FROM turns WHERE turn_id=?", (str(turn_id),)
            ).fetchone()), True
        if connection.execute(
            "SELECT 1 FROM turns WHERE conversation_id=? AND state='running' AND execution_turn_id IS NULL",
            (str(conversation_id),),
        ).fetchone():
            raise ConversationBusy("owning conversation is busy; input retained")
        turn_id = TurnId.new()
        timestamp = started_at or _now()
        connection.execute(
            "INSERT INTO turns("
            "turn_id, conversation_id, source_event_key, operator_id, state, "
            "input_text, started_at) "
            "VALUES (?, ?, ?, ?, 'running', ?, ?)",
            (
                str(turn_id),
                str(conversation_id),
                source_event_key,
                operator_id,
                input_text,
                timestamp,
            ),
        )
        return Turn(
            turn_id,
            conversation_id,
            source_event_key,
            input_text,
            None,
            None,
            "running",
            None,
            timestamp,
            None,
        ), True

    @staticmethod
    def _release_rejected_native_input_in(
        connection: sqlite3.Connection, source: sqlite3.Row
    ) -> bool:
        """A proved non-delivery native input can be retried once its execution stops."""
        if source["input_disposition"] != "rejected":
            return False
        if connection.execute(
            "SELECT 1 FROM turns WHERE turn_id=? AND state='running'",
            (source["execution_turn_id"],),
        ).fetchone():
            return False
        connection.execute("DELETE FROM turns WHERE turn_id=?", (source["turn_id"],))
        return True

    def withdraw_turn(self, turn_id: TurnId) -> None:
        """Forget a turn no provider or linked input ever saw."""
        with self.connect(write=True) as connection:
            withdrawn = connection.execute(
                "DELETE FROM turns WHERE turn_id=? AND state='running' "
                "AND episode_input IS NULL AND NOT EXISTS "
                "(SELECT 1 FROM turns AS linked WHERE linked.execution_turn_id=turns.turn_id)",
                (str(turn_id),),
            )
            if withdrawn.rowcount != 1:
                raise RuntimeError("only an unclaimed, unlinked running turn can be withdrawn")

    def bind_native_execution(self, turn_id: TurnId, expected_generation: int) -> None:
        """Fence this live input handle to the native lineage that opened it."""
        with self.connect(write=True) as connection:
            owner = connection.execute(
                "SELECT conversation_id FROM turns WHERE turn_id=? "
                "AND state='running' AND execution_turn_id IS NULL",
                (str(turn_id),),
            ).fetchone()
            live = owner and self._lineage_in(connection, owner["conversation_id"])
            if live is None or live.generation != expected_generation:
                raise RuntimeError("native input handle requires an active execution")
            row = connection.execute(
                "UPDATE turns SET provider=?, generation=?, provider_session_id=? "
                "WHERE turn_id=? AND state='running' AND execution_turn_id IS NULL",
                (
                    live.provider,
                    live.generation,
                    live.provider_session_id,
                    str(turn_id),
                ),
            )
            if row.rowcount != 1:
                raise RuntimeError("native input handle requires an active execution")

    def record_native_input_result(
        self, execution_turn_id: TurnId, source_id: str, disposition: str
    ) -> None:
        """Retain monotone source acknowledgement independently of execution outcome."""
        if disposition not in {"accepted", "rejected", "unresolved"}:
            raise ValueError("unknown native input disposition")
        with self.connect(write=True) as connection:
            row = connection.execute(
                "SELECT input_disposition FROM turns WHERE turn_id=? AND execution_turn_id=?",
                (source_id, str(execution_turn_id)),
            ).fetchone()
            if row is None:
                raise RuntimeError("native acknowledgement does not belong to this execution")
            if row[0] not in {"unresolved", disposition}:
                raise RuntimeError("native input disposition changed after resolution")
            connection.execute(
                "UPDATE turns SET input_disposition=? WHERE turn_id=?", (disposition, source_id)
            )

    def active_turn(self, conversation_id: ConversationId) -> Turn | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM turns WHERE conversation_id = ? AND state = 'running' AND execution_turn_id IS NULL",
                (str(conversation_id),),
            ).fetchone()
        return None if row is None else self._turn(row)

    def interrupt_turn(self, turn_id: TurnId, reason: str) -> None:
        """Interrupt a turn and discard any session without an accepted turn."""
        if not reason.strip():
            raise ValueError("interrupted turn requires a reason")
        with self.connect(write=True) as connection:
            row = connection.execute(
                "SELECT conversation_id, state FROM turns WHERE turn_id = ?",
                (str(turn_id),),
            ).fetchone()
            if row is not None and row["state"] == "interrupted":
                return
            if row is None or row["state"] != "running":
                raise RuntimeError("turn is not running")
            now = _now()
            changed = connection.execute(
                "UPDATE turns SET state = 'interrupted', status_reason = ?, "
                "completed_at = ? WHERE turn_id = ? AND state = 'running'",
                (reason, now, str(turn_id)),
            )
            if changed.rowcount != 1:
                raise RuntimeError("turn is not running")
            self._discard_unproven_lineage_in(connection, row["conversation_id"])

    def _discard_unproven_lineage_in(
        self, connection: sqlite3.Connection, conversation_id: str
    ) -> None:
        """Drop a session no completed turn ever accepted, in the caller's transaction."""
        lineage = self._lineage_in(connection, conversation_id)
        if lineage is None or lineage.provider_session_id is None:
            return
        proven = connection.execute(
            "SELECT 1 FROM turns WHERE conversation_id = ? AND state = 'completed' "
            "AND generation = ? AND provider_session_id = ?",
            (conversation_id, lineage.generation, lineage.provider_session_id),
        ).fetchone()
        if proven is None:
            self._bind_in(connection, conversation_id, lineage.provider, None)

    def interrupt_abandoned_turns(
        self, reason: str = "daemon restarted before the turn completed"
    ) -> int:
        """Interrupt crash-left turns and discard sessions without accepted turns."""
        if not reason.strip():
            raise ValueError("abandoned turns require a reason")
        with self.connect(write=True) as connection:
            now = _now()
            abandoned = connection.execute(
                "SELECT DISTINCT conversation_id FROM turns WHERE state = 'running' "
                "AND execution_turn_id IS NULL AND episode_input IS NULL"
            ).fetchall()
            for owner in abandoned:
                self._discard_unproven_lineage_in(connection, owner["conversation_id"])
            changed = connection.execute(
                "UPDATE turns SET state = 'interrupted', status_reason = ?, "
                "completed_at = ? WHERE state = 'running' "
                "AND execution_turn_id IS NULL AND episode_input IS NULL",
                (reason, now),
            )
            return changed.rowcount

    def _insert_task(self, connection, spec, *, task_id, kind, origin_ref,
                     conversation_id, provider, profile, status=TaskStatus.QUEUED,
                     status_reason=None, source=None):
        task_id, created = self.tasks.create(
            spec, kind=kind, source=source or (f"{kind.value}:{origin_ref}" if origin_ref else None),
            owner=str(conversation_id) if conversation_id else None,
            hold=status.value if status is not TaskStatus.QUEUED else None,
            reason=status_reason, task_id=task_id,
        )
        self._open_in(connection, str(ConversationId.for_task(task_id)), provider=provider, profile=profile)
        return TaskAdmission(task_id, created)

    @staticmethod
    def _task_result_text(task) -> str:
        """The result text for a conversation-owned task that stopped.

        Status, reason and findings are the last slice's, and the last slice
        is a commit.
        """
        lines = [
            f"Task {task.status.value}: {task.title}",
            f"Task: {task.task_id}",
            f"Repository: {task.repository}",
        ]
        if task.status is TaskStatus.DONE and task.tip:
            if task.read_only:
                lines.append(f"Evidence SHA: {task.tip}")
                if task.procedure.workdir is None:
                    lines.append(f"Reviewed candidate: {task.procedure.candidate}")
            else:
                lines.append(f"Landed SHA: {task.tip}")
        if task.reason:
            lines.append(f"Reason: {task.reason}")
        # What the execution actually concluded. For a task that changed no
        # files this is the only product the operator gets.
        if task.findings:
            lines.append(task.findings)
        if task.pending:
            context = "\n".join(f"{kind}: {text}" for _, kind, text, _ in task.pending)
            if len(context) > 2000:
                context = context[:2000] + "\n[truncated; remaining inputs retained in task state]"
            lines.append("Inputs not consumed by this execution:\n" + context)
        return "\n".join(lines)[:10_000]

    def _undelivered_task_results(self):
        """Each owned task that stopped at an outcome its owner has not been given."""
        from steward_harness.task_query import is_task_query
        for task in self.tasks.all():
            status = task.status
            if (not task.owner or task.dispatchable or status not in {
                    TaskStatus.WAITING, TaskStatus.BLOCKED, TaskStatus.CANCELLED, TaskStatus.DONE}
                    or (status is TaskStatus.WAITING and is_task_query(status.value, task.reason))):
                continue
            key = f"task_result:{task.task_id}:{task.outcome}:{status.value}"
            if self.result_receipt(key).get("done"):
                continue
            with self.connect() as connection:
                if connection.execute("SELECT 1 FROM turns WHERE conversation_id=? AND source_event_key=?",
                                      (task.owner, key)).fetchone():
                    continue
            yield task, key

    def pending_task_result_conversations(self):
        return tuple(dict.fromkeys([
            *(ConversationId(r["owner"]) for r in self.pending_result_receipts() if r.get("owner")),
            *(ConversationId(task.owner) for task, _ in self._undelivered_task_results()),
        ]))

    def result_receipt_path(self, source_key: str) -> Path:
        key = hashlib.sha256(source_key.encode()).hexdigest()
        return self.path.with_name(f"{self.path.name}.task-results") / f"{key}.json"

    def result_receipt(self, source_key: str) -> dict:
        path = self.result_receipt_path(source_key)
        return json.loads(path.read_text()) if path.exists() else {}

    def save_result_receipt(self, receipt: dict) -> None:
        write_receipt(self.result_receipt_path(receipt["source_key"]), receipt)

    def pending_result_receipts(self) -> list[dict]:
        receipts = [
            json.loads(path.read_text())
            for path in self.result_receipt_path("").parent.glob("*.json")
        ]
        return sorted(
            (receipt for receipt in receipts if not receipt.get("done")),
            key=lambda receipt: (receipt.get("target", ""), receipt.get("task_id") or "",
                                 receipt.get("sequence", 0), receipt["source_key"]),
        )

    def pending_task_result_for(self, conversation_id):
        for receipt in self.pending_result_receipts():
            if receipt["owner"] == str(conversation_id):
                return (TaskId(receipt["task_id"]) if receipt.get("task_id") else None), receipt["result_text"], receipt["source_key"]
        for task, key in self._undelivered_task_results():
            if task.owner == str(conversation_id):
                return task.task_id, self._task_result_text(task), key
        return None

    def retain_pending_result(self, conversation_id) -> dict | None:
        """Retain the selected outcome before assessment or route diagnostics."""
        pending = self.pending_task_result_for(conversation_id)
        if pending is None:
            return None
        task_id, result_text, source_key = pending
        receipt = self.result_receipt(source_key)
        if not receipt:
            receipt = {
                "owner": str(conversation_id), "task_id": str(task_id) if task_id else None,
                "source_key": source_key, "result_text": result_text,
            }
            self.save_result_receipt(receipt)
        return receipt

    @staticmethod
    def _incident(row: sqlite3.Row) -> Incident:
        return Incident(
            IncidentId(row["pipeline"], row["cycle"]),
            IncidentStatus(row["status"]),
            row["consecutive_failures"],
            row["transient_rechecks_left"],
            TaskId(row["repair_task_id"]) if row["repair_task_id"] else None,
            TaskId(row["escalation_task_id"])
            if row["escalation_task_id"]
            else None,
            row["last_details"],
            row["last_observed_at"],
            row["last_recovered_at"],
        )

    def get_incident(self, pipeline: str) -> Incident | None:
        IncidentId(pipeline, 1)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE pipeline = ?", (pipeline,)
            ).fetchone()
        return None if row is None else self._incident(row)

    def incident_probe_due(
        self,
        pipeline: str,
        interval_seconds: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Keep probe pacing durable without creating a scheduler subsystem."""
        IncidentId(pipeline, 1)
        if interval_seconds < 1:
            raise ValueError("probe interval must be positive")
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("probe clock must be timezone-aware")
        incident = self.get_incident(pipeline)
        if incident is None:
            return True
        observed = datetime.fromisoformat(incident.last_observed_at)
        return current >= observed + timedelta(seconds=interval_seconds)

    def _cancel_unneeded_incident_task(self, task_id: str) -> None:
        task = self.tasks.get(TaskId(task_id))
        # Work that reached the remote is not withdrawable.
        if task.status is TaskStatus.RUNNING:  # a live slice, or a candidate in publication
            self.tasks.hold(task.task_id, "cancelled", "probe recovered")
        elif task.status in {TaskStatus.PROPOSED, TaskStatus.QUEUED, TaskStatus.BLOCKED}:
            self.tasks.hold(task.task_id, "cancelled", "probe recovered before incident work completed")

    #: A repair task can only have run so many slices before a policy that
    #: allows three attempts a day has already refused it.
    _REPAIR_HISTORY = 32

    def _incident_repairs(self, pipeline):
        return [task for task in self.tasks.all()
                if task.origin_kind is TaskOriginKind.INCIDENT_REPAIR
                and (task.origin_ref or "").startswith(f"{pipeline}/")]

    def _incident_budget_counts(self, pipeline: str, cutoff: str) -> tuple[int, int]:
        """How much repair this pipeline has already had in the window.

        An attempt is a slice, and a slice is a commit on the repair task's
        branch, so this counts trailers rather than rows. It walks the
        pipeline's repair tasks, which the budget it feeds keeps to a handful.
        """
        attempts = sum(
            1
            for task in self._incident_repairs(pipeline)
            for slice_ in self.tasks.checkpoints(task, self._REPAIR_HISTORY)
            if slice_.created_at >= cutoff
        )
        # And a landing is a merged branch whose last slice closed inside the
        # window. Commit time is landing time now that the push happens in the
        # same breath as the gates.
        landed = sum(
            1
            for task in self._incident_repairs(pipeline)
            if task.status is TaskStatus.DONE
            and (recent := self.tasks.checkpoints(task, 1))
            and recent[0].created_at >= cutoff
        )
        return attempts, landed

    def _incident_failure_count(self, task) -> tuple[int, str | None]:
        """How often this repair has failed, and when it last did.

        `blocked` is the disposition a fault ends a slice with, so the branch
        counts them: each one is a commit the harness made carrying that
        trailer, dated when it made it.
        """
        failed = [
            slice_
            for slice_ in self.tasks.checkpoints(task, self._REPAIR_HISTORY)
            if slice_.disposition is CheckpointDisposition.BLOCKED
        ]
        return len(failed), failed[0].created_at if failed else None

    def _escalate_incident(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        repository: str,
        reason: str,
        provider: str,
        profile: str,
    ) -> str:
        incident_id = IncidentId(row["pipeline"], row["cycle"])
        existing = next((task for task in self.tasks.all()
                         if task.origin_kind is TaskOriginKind.INCIDENT_ESCALATION
                         and task.origin_ref == str(incident_id)), None)
        if existing is None:
            spec = TaskSpec(
                repository,
                f"Review unresolved incident {row['pipeline']}",
                f"Pipeline {row['pipeline']} remains unhealthy. {reason}. "
                f"Latest probe: {row['last_details']}",
                priority=5,
            )
            admission = self._insert_task(
                connection,
                spec,
                task_id=TaskId("task-" + uuid.uuid5(uuid.NAMESPACE_URL, f"incident_escalation:{incident_id}").hex),
                kind=TaskOriginKind.INCIDENT_ESCALATION,
                origin_ref=str(incident_id),
                conversation_id=None,
                provider=provider,
                profile=profile,
                status=TaskStatus.PROPOSED,
            )
            task_id = admission.task_id
        else:
            task_id = existing.task_id
        connection.execute(
            "UPDATE incidents SET status = 'escalated', transient_rechecks_left = 0, "
            "repair_task_id = NULL, escalation_task_id = ?, updated_at = ? "
            "WHERE pipeline = ?",
            (str(task_id), _now(), row["pipeline"]),
        )
        return f"[{row['pipeline']}] escalated ({task_id}): {reason}"

    def observe_incident(
        self,
        *,
        pipeline: str,
        repository: str,
        healthy: bool,
        details: str,
        observed_at: str,
        rule: IncidentPolicy,
        automatic_repair: bool,
        provider: str,
        profile: str,
    ) -> str | None:
        """Record a probe observation and authorize ordinary Git tasks within incident policy."""
        IncidentId(pipeline, 1)
        normalized_details = details.strip()[:1000]
        if not repository.strip() or not normalized_details:
            raise ValueError("incident repository and details must be nonblank")
        observed = datetime.fromisoformat(observed_at)
        if observed.tzinfo is None:
            raise ValueError("incident observation must be timezone-aware")
        if not provider.strip() or profile not in {"fast", "balanced", "deep"}:
            raise ValueError("incident task lineage is invalid")
        with self.connect(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE pipeline = ?", (pipeline,)
            ).fetchone()
            if row is None:
                now = _now()
                connection.execute(
                    "INSERT INTO incidents VALUES (?, 1, 'healthy', 0, 0, NULL, "
                    "NULL, ?, ?, NULL, ?, ?)",
                    (pipeline, normalized_details, observed_at, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM incidents WHERE pipeline = ?", (pipeline,)
                ).fetchone()
                assert row is not None

            if healthy:
                was_open = row["status"] != IncidentStatus.HEALTHY.value
                now = _now()
                for task_id in (row["repair_task_id"], row["escalation_task_id"]):
                    if task_id is not None:
                        self._cancel_unneeded_incident_task(task_id)
                connection.execute(
                    "UPDATE incidents SET cycle = cycle + ?, status = 'healthy', "
                    "consecutive_failures = 0, transient_rechecks_left = 0, "
                    "repair_task_id = NULL, escalation_task_id = NULL, "
                    "last_details = ?, last_observed_at = ?, "
                    "last_recovered_at = CASE WHEN ? THEN ? ELSE last_recovered_at END, "
                    "updated_at = ? WHERE pipeline = ?",
                    (
                        int(was_open),
                        normalized_details,
                        observed_at,
                        int(was_open),
                        observed_at,
                        now,
                        pipeline,
                    ),
                )
                return f"[{pipeline}] resolved: probe recovered" if was_open else None

            next_failures = row["consecutive_failures"] + 1
            if row["status"] in {
                IncidentStatus.HEALTHY.value,
                IncidentStatus.FAILING.value,
            }:
                status = (
                    IncidentStatus.CONFIRMED
                    if next_failures >= rule.confirm_after_failures
                    else IncidentStatus.FAILING
                )
                rechecks = rule.transient_retries if status is IncidentStatus.CONFIRMED else 0
            else:
                status = IncidentStatus(row["status"])
                rechecks = row["transient_rechecks_left"]
            connection.execute(
                "UPDATE incidents SET status = ?, consecutive_failures = ?, "
                "transient_rechecks_left = ?, last_details = ?, last_observed_at = ?, "
                "updated_at = ? WHERE pipeline = ?",
                (
                    status.value,
                    next_failures,
                    rechecks,
                    normalized_details,
                    observed_at,
                    _now(),
                    pipeline,
                ),
            )
            row = connection.execute(
                "SELECT * FROM incidents WHERE pipeline = ?", (pipeline,)
            ).fetchone()
            assert row is not None
            if status is IncidentStatus.FAILING:
                return None
            if status is IncidentStatus.ESCALATED:
                return None
            if row["repair_task_id"] is not None:
                task_id = TaskId(row["repair_task_id"])
                task = self.tasks.get(task_id)
                if task.status in {TaskStatus.RUNNING, TaskStatus.WAITING,
                                   TaskStatus.PROPOSED, TaskStatus.QUEUED}:
                    return None
                failures, last_failed_at = self._incident_failure_count(task)
                # A repair "did not restore health" only once its work
                # actually reached the remote.
                if task.status is TaskStatus.DONE:
                    return self._escalate_incident(
                        connection,
                        row,
                        repository=repository,
                        reason="a landed repair did not restore probe health",
                        provider=provider,
                        profile=profile,

                    )
                if failures >= rule.escalate_after_failed_repairs:
                    return self._escalate_incident(
                        connection,
                        row,
                        repository=repository,
                        reason=f"{failures} consecutive repair attempts failed",
                        provider=provider,
                        profile=profile,

                    )
                if last_failed_at is not None:
                    retry_at = datetime.fromisoformat(last_failed_at) + timedelta(
                        seconds=rule.repair_failure_cooldown_seconds
                    )
                    if observed < retry_at:
                        return None
                cutoff = (observed - timedelta(hours=24)).isoformat()
                attempts, landed = self._incident_budget_counts(pipeline, cutoff)
                if attempts >= rule.repair_attempts_per_24h:
                    return None
                if landed >= rule.landed_changes_per_24h:
                    return None
                self.tasks.retry(task_id, "probe remains unhealthy; reconcile the retained repair work",
                                 source=None)
                return (
                    f"[{pipeline}] repair_retried ({task_id}): "
                    "retrying the retained incident task"
                )

            if row["transient_rechecks_left"] > 0:
                connection.execute(
                    "UPDATE incidents SET transient_rechecks_left = "
                    "transient_rechecks_left - 1, updated_at = ? WHERE pipeline = ?",
                    (_now(), pipeline),
                )
                return None
            if not automatic_repair:
                return None
            if row["last_recovered_at"] is not None:
                repair_at = datetime.fromisoformat(row["last_recovered_at"]) + timedelta(
                    seconds=rule.recovery_cooldown_seconds
                )
                if observed < repair_at:
                    return None
            cutoff = (observed - timedelta(hours=24)).isoformat()
            attempts, landed = self._incident_budget_counts(pipeline, cutoff)
            if attempts >= rule.repair_attempts_per_24h:
                return None
            if landed >= rule.landed_changes_per_24h:
                return None
            incident_id = IncidentId(pipeline, row["cycle"])
            spec = TaskSpec(
                repository,
                f"Repair pipeline {pipeline}",
                f"Pipeline {pipeline} is unhealthy. Latest probe: "
                f"{normalized_details}. Diagnose the cause and implement the "
                "smallest durable repair.",
                priority=5,
            )
            admission = self._insert_task(
                connection,
                spec,
                task_id=TaskId("task-" + uuid.uuid5(uuid.NAMESPACE_URL, f"incident_repair:{incident_id}").hex),
                kind=TaskOriginKind.INCIDENT_REPAIR,
                origin_ref=str(incident_id),
                conversation_id=None,
                provider=provider,
                profile=profile,
            )
            connection.execute(
                "UPDATE incidents SET repair_task_id = ?, updated_at = ? "
                "WHERE pipeline = ?",
                (str(admission.task_id), _now(), pipeline),
            )
            return f"[{pipeline}] repair_admitted ({admission.task_id}): repair task admitted"
