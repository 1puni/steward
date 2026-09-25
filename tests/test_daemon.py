"""Focused construction tests for the executable state-native daemon."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from test_world_turn_checkpoint import _git_world as make_world
from steward_harness.lease import Lease
from steward_harness.world.turn_checkpoint import WorldContentConflict, WorldUpdatePending
from state_fixtures import (
    accept_conversation_turn,
    admit_task,
    close_task_slice,
    running,

)
from steward_harness.repository_reconciler import RepositoryReconciler
from steward_harness.cognition import Cognition
from steward_harness.config.schema import StewardConfig, UntrustedExecutionConfig
from steward_harness.conversations import ConversationService
from steward_harness.daemon import KernelCommands, StewardDaemon
from steward_harness.deploy.release import ReleaseManager
from steward_harness.git_transport import ControllerGitTransport, GitTransportError
from steward_harness.incidents.kernel import IncidentProbeLoop
from steward_harness.runtime.execution import BoundaryStatus, UntrustedExecutionBroker
from steward_harness.task_lock import task_lock
from steward_harness.state import (
    ConversationBusy,
    ConversationId,
    StateDatabase,
)
from steward_harness.state import (
    TaskId,
    TaskSpec,
    TaskStatus,
)
from steward_harness.lease import Busy
from world_fixtures import ReadingAdapter


def test_startup_recovers_world_edits_before_starting_services(tmp_path, monkeypatch):
    from test_world_durability import Crash, run, runtime

    state, checkpoint, inbound, cognition = runtime(tmp_path)
    prepare = state.record_candidate

    def crash_after_prepare(*args, **kwargs):
        prepare(*args, **kwargs)
        raise Crash("prepared but not applied")

    monkeypatch.setattr(state, "record_candidate", crash_after_prepare)
    with pytest.raises(Crash):
        run(inbound)
    assert state.pending_turns()
    assert not (checkpoint.world.root / "decision.md").exists()

    config = StewardConfig.model_validate({
        **_config(tmp_path).model_dump(),
        "world": {"root": str(checkpoint.world.root)},
    })
    adapter = ReadingAdapter("unused", lambda _: pytest.fail("startup repeated cognition"))
    daemon = StewardDaemon(config, tmp_path / "steward.yaml", adapters={"codex": adapter})
    conversation = state.get_or_create_conversation(
        "desk", "abandoned", provider="codex", profile="balanced",
    )
    abandoned, _ = state.start_turn(conversation.conversation_id, "old-input", "desk", "Interrupted input")
    observed = threading.Event()
    pending = StateDatabase.pending_task_result_conversations

    def observe_recovered(database):
        assert database.pending_turns() == []
        assert (checkpoint.world.root / "decision.md").read_text().startswith("The private consumer")
        with database.connect() as connection:
            assert connection.execute(
                "SELECT state FROM turns WHERE turn_id=?", (str(abandoned.turn_id),),
            ).fetchone()[0] == "interrupted"
        observed.set()
        return pending(database)

    monkeypatch.setattr(StateDatabase, "pending_task_result_conversations", observe_recovered)
    with running(daemon):
        assert observed.wait(5), "the first polling read did not observe completed startup recovery"
        assert cognition.calls == 1
        assert adapter.requests == []


def _config(tmp_path, **provider_overrides) -> StewardConfig:
    workdir = tmp_path / "work"
    workdir.mkdir()
    provider = {
        "state_db": str(tmp_path / "state.db"),
        "workdir": str(workdir),
        **provider_overrides,
    }
    return StewardConfig.model_validate(
        {"identity": {"name": "Steward", "slug": "test"}, "provider": provider}
    )


def test_daemon_incident_callback_admits_an_ordinary_repair_task(tmp_path) -> None:
    workdir = tmp_path / "work"
    repository = tmp_path / "repository"
    workdir.mkdir()
    repository.mkdir()
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "Steward", "slug": "test"},
            "provider": {
                "state_db": str(tmp_path / "state.db"),
                "workdir": str(workdir),
                "fallback_families": [],
            },
            "incident_policy": {
                "confirm_after_failures": 1,
                "transient_retries": 0,
                "recovery_cooldown_seconds": 0,
                "repair_failure_cooldown_seconds": 0,
            },
            "repositories": {
                "app": {
                    "path": str(repository),
                    "remote_url": str(tmp_path / "remote.git"),
                }
            },
            "pipelines": {
                "health": {
                    "repository": "app",
                    "probe": {
                        "type": "command",
                        "command": {"argv": ["false"]},
                    },
                }
            },
        }
    )
    state = StateDatabase(config.provider.state_db)
    notifications = []

    def notify(message):
        # A separate read sees the admitted task before any transport effect.
        incident = state.get_incident("health")
        assert incident is not None and incident.repair_task_id is not None
        assert state.tasks.get(incident.repair_task_id).status is TaskStatus.QUEUED
        notifications.append(message)

    callback = IncidentProbeLoop(
        # Run the real command as the test user; split-identity enforcement
        # has its own Linux acceptance cases.
        state, config, UntrustedExecutionBroker(config.execution),
         notify=notify,
    )
    callback.observe("health")

    assert state.get_incident("health").last_details == "command: Command exited 1"
    tasks = state.tasks.all()
    assert len(tasks) == 1
    assert tasks[0].repository == "app"
    assert tasks[0].origin_kind.value == "incident_repair"
    assert notifications == [
        f"[health] repair_admitted ({tasks[0].task_id}): repair task admitted"
    ]


def test_daemon_lease_uses_kernel_identity(tmp_path) -> None:
    daemon = StewardDaemon(_config(tmp_path), tmp_path / "steward.yaml", adapters={})

    assert daemon._daemon_lease().path.name == ".state.db.kernel.lock"


def test_daemon_holds_one_process_lease_around_runtime_construction(
    tmp_path, monkeypatch
) -> None:
    config = _config(tmp_path)
    first = StewardDaemon(config, tmp_path / "steward.yaml", adapters={})
    second = StewardDaemon(config, tmp_path / "steward.yaml", adapters={})
    monkeypatch.setattr(
        second,
        "_start_owned",
        lambda: pytest.fail("runtime construction occurred without the daemon lease"),
    )

    with first._daemon_lease(), pytest.raises(Busy):
        second.run_forever(poll_seconds=0.01)


def test_boundary_is_checked_before_runtime_construction(tmp_path, monkeypatch) -> None:
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "Steward", "slug": "test"},
            "provider": {
                "state_db": str(tmp_path / "state.db"),
                "workdir": str(tmp_path / "work"),
            },
            "repositories": {
                "app": {
                    "path": str(tmp_path / "app"),
                    "remote_url": "https://example.invalid/app.git",
                }
            },
        }
    )
    broker = UntrustedExecutionBroker(config.execution)
    monkeypatch.setattr(
        broker,
        "status",
        lambda: BoundaryStatus(False, "no agent identity"),
    )
    daemon = StewardDaemon(
        config, tmp_path / "steward.yaml", broker=broker, adapters={}
    )
    monkeypatch.setattr(
        daemon,
        "_start_owned",
        lambda: pytest.fail("state initialized before boundary validation"),
    )

    with pytest.raises(RuntimeError, match="no agent identity"):
        daemon.run_forever(poll_seconds=0.01)

    assert not (tmp_path / "state.db").exists()


def test_commands_route_multiple_tasks_independently_of_the_steward(tmp_path) -> None:
    config = _config(
        tmp_path,
        default_family="codex",
        fallback_families=["future-provider"],
    )
    state = StateDatabase(config.provider.state_db)
    cognition = Cognition({})
    conversations = ConversationService(
        state,
        cognition,
        provider_order=config.provider.family_order,
        profile="balanced",
        workspace=tmp_path / "work",
        timeout_seconds=10,

    )
    commands = KernelCommands(
        config,
        state,
        conversations,
        cast(RepositoryReconciler, SimpleNamespace()),
        {},
        UntrustedExecutionBroker(config.execution),
    )

    switched = commands("model_family", "future-provider", 1, 42, 7)
    conversation = state.find_conversation("telegram", "42")
    assert conversation is not None
    assert conversation.provider == "future-provider"
    assert "no foreign session" in switched

    tasks = []
    for index in range(2):
        turn, _ = state.start_turn(
            conversation.conversation_id, f"task-{index}", "operator", "Do the work"
        )
        receipt = accept_conversation_turn(
            state,
            turn.turn_id,
            reply_text="Proposed work",
            provider="codex",
            model="model",
            provider_session_id="steward-session",
            spec=TaskSpec("app", f"Task {index}", "Complete this work."),
        )
        tasks.append(state.tasks.get(TaskId(receipt["task_id"])))
    first, second = tasks
    assert len({first.session_id, second.session_id, conversation.conversation_id}) == 3
    # A task's session is named after the task, so it cannot be another task's
    # or the topic's without being a different string. The collision a `UNIQUE`
    # column used to refuse is now unwritable: there is no column to rename.
    assert first.session_id == ConversationId.for_task(first.task_id)
    state.bind_conversation_provider(first.session_id, "codex", "task-session")
    commands("model_family", "future-provider", 1, 42, 7)
    assert state.find_conversation(conversation.transport, conversation.transport_key).provider == "future-provider"
    assert state.get_conversation(state.tasks.get(first.task_id).session_id).provider_session_id == "task-session"
    commands("model", "deep", 1, 42, 7)
    commands("model_family", "codex", 1, 42, 7)
    assert "codex/deep" in commands("model", None, 1, 42, 7)
    assert (
        state.get_conversation(state.tasks.get(first.task_id).session_id).provider_session_id == "task-session"
    )
    assert state.get_conversation(state.tasks.get(first.task_id).session_id).profile == "balanced"

    assert "codex/balanced" in commands("task", f"model {first.task_id}", 1, 42, 7)
    commands("task", f"model {first.task_id} fast", 1, 42, 7)
    commands("task", f"model_family {first.task_id} codex", 1, 42, 7)
    assert (
        state.get_conversation(state.tasks.get(first.task_id).session_id).provider_session_id == "task-session"
    )
    commands("task", f"model_family {first.task_id} future-provider", 1, 42, 7)
    changed = state.get_conversation(state.tasks.get(first.task_id).session_id)
    assert changed.provider == "future-provider"
    assert changed.profile == "fast"
    assert changed.generation == 2
    assert changed.provider_session_id is None
    assert "future-provider/fast" in commands(
        "task", f"model_family {first.task_id}", 1, 42, 7
    )
    # An old execution callback cannot restore its session after the switch.
    with pytest.raises(ConversationBusy):
        state.bind_conversation_provider(first.session_id, "codex", "stale-session", expected_generation=1)
    assert state.get_conversation(first.session_id) == changed
    assert state.get_conversation(state.tasks.get(second.task_id).session_id).provider == "codex"
    assert state.get_conversation(state.tasks.get(second.task_id).session_id).profile == "balanced"
    assert "codex/deep" in commands("model", None, 1, 42, 7)
    for verb, value in [("model", "invalid"), ("model_family", "unconfigured")]:
        assert "⚠️" in commands("task", f"{verb} {first.task_id} {value}", 1, 42, 7)
    assert state.get_conversation(first.session_id) == changed
    assert "⚠️" in commands("task", "model no-such-task", 1, 42, 7)

    assert "not configured" in commands("model_family", "unconfigured", 1, 42, 7)

    commands("pause", None, 1, 42, 7)
    assert state.paused()
    commands("resume", None, 1, 42, 7)
    assert not state.paused()


def test_task_cancel_records_an_operator_reason(tmp_path) -> None:
    config = _config(tmp_path, fallback_families=[])
    state = StateDatabase(config.provider.state_db)
    cognition = Cognition({})
    conversations = ConversationService(
        state,
        cognition,
        provider_order=config.provider.family_order,
        profile="balanced",
        workspace=tmp_path / "work",
        timeout_seconds=10,

    )
    commands = KernelCommands(
        config,
        state,
        conversations,
        cast(RepositoryReconciler, SimpleNamespace()),
        {},
        UntrustedExecutionBroker(config.execution),
    )
    task_id = admit_task(
        state,
        TaskSpec("app", "Superseded deployment", "Historical exact SHA failed."),
        provider="codex",
        profile="balanced",
    ).task_id
    state.tasks.hold(task_id, "blocked", "deployment was overtaken")

    reply = commands(
        "task",
        f"cancel {task_id} :: superseded by active descendant",
        1,
        42,
        7,
    )

    cancelled = state.tasks.get(task_id)
    assert "Cancellation recorded" in reply
    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.reason == "superseded by active descendant"


def test_one_cancel_closes_several_superseded_tasks(tmp_path) -> None:
    """Bulk cleaning belongs to the command that already cancels.

    The task board reads only, so the operator's second ask — closing work a
    later task made pointless — is served here rather than by a second way of
    cancelling with its own semantics. Each id is attempted on its own: one
    unknown slug must not withdraw nothing else.
    """
    config = _config(tmp_path, fallback_families=[])
    state = StateDatabase(config.provider.state_db)
    conversations = ConversationService(
        state,
        Cognition({}),
        provider_order=config.provider.family_order,
        profile="balanced",
        workspace=tmp_path / "work",
        timeout_seconds=10,

    )
    commands = KernelCommands(
        config,
        state,
        conversations,
        cast(RepositoryReconciler, SimpleNamespace()),
        {},
        UntrustedExecutionBroker(config.execution),
    )
    superseded = [
        admit_task(
            state, TaskSpec("app", f"Historical attempt {ordinal}", "Overtaken.")
        ).task_id
        for ordinal in range(3)
    ]

    reply = commands(
        "task",
        f"cancel {superseded[0]} {superseded[1]} nobody-admitted-this "
        f"{superseded[2]} :: superseded by the consolidated rollout",
        1,
        42,
        7,
    )

    assert reply.count("Cancellation recorded") == 3
    assert "⚠️ nobody-admitted-this: unknown task" in reply
    for task_id in superseded:
        closed = state.tasks.get(task_id)
        assert closed.status is TaskStatus.CANCELLED
        assert closed.reason == "superseded by the consolidated rollout"




def _cancel_conversation_task(
    state: StateDatabase, transport: str, transport_key: str
) -> TaskId:
    conversation = state.get_or_create_conversation(
        transport,  # type: ignore[arg-type]
        transport_key,
        provider="codex",
        profile="balanced",
    )
    turn, _started = state.start_turn(
        conversation.conversation_id,
        f"event-{transport_key}",
        "operator",
        "Please do this",
    )
    receipt = accept_conversation_turn(
        state,
        turn.turn_id,
        reply_text="Task admitted.",
        provider="codex",
        model="gpt",
        provider_session_id=None,
        spec=TaskSpec("app", "Report completion", "Do the work."),
    )
    task_id = TaskId(receipt["task_id"])
    state.tasks.cancel(task_id, "operator stopped it")
    return task_id


def test_daemon_delivers_task_truth_to_the_desk_that_admitted_it(tmp_path, monkeypatch) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    events = tmp_path / "desk" / "events.jsonl"
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "Steward", "slug": "test"},
            "repositories": {"app": {"path": str(tmp_path / "repo"), "remote_url": "https://example.invalid/app.git"}},
            "provider": {
                "state_db": str(tmp_path / "state.db"),
                "workdir": str(workdir),
                "fallback_families": [],
            },
            "desk": {
                "inbox_dir": str(tmp_path / "inbox"),
                "events_file": str(events),
            },
        }
    )
    reviewer = ReadingAdapter("review-session", lambda request: "")
    daemon = StewardDaemon(
        config, tmp_path / "steward.yaml", adapters={"codex": reviewer}
    )
    daemon.broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    monkeypatch.setattr(daemon, "_require_boundary", lambda: None)
    state = StateDatabase(config.provider.state_db)
    task_id = _cancel_conversation_task(state, "desk", "7")
    sent = threading.Event()
    from steward_harness.desk import DeskEvents
    append = DeskEvents.append

    def record(events, kind, text, msg_id=""):
        append(events, kind, text, msg_id)
        if kind == "reply":
            sent.set()

    monkeypatch.setattr(DeskEvents, "append", record)
    with running(daemon) as errors:
        assert sent.wait(5), errors

    assert len(reviewer.requests) == 1
    assert "Harness task result" in reviewer.requests[0].prompt
    assert str(task_id) in events.read_text()
    assert "Task cancelled" in events.read_text()


@pytest.mark.parametrize("busy_error", [Busy, ConversationBusy])
def test_daemon_requeues_desk_ingress_when_owner_is_busy(tmp_path, busy_error) -> None:
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "Steward", "slug": "test"},
            "desk": {
                "inbox_dir": str(tmp_path / "inbox"),
                "events_file": str(tmp_path / "events.jsonl"),
            },
        }
    )
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    queued = inbox_dir / "1.000000-busy.json"
    queued.write_text(
        '{"kind":"message","text":"wait","origin":"web","id":"busy"}'
    )
    daemon = StewardDaemon(config, tmp_path / "steward.yaml", adapters={})

    attempts = []

    def busy(**kwargs):
        attempts.append(kwargs["source_event_key"])
        if len(attempts) == 1:
            raise busy_error("Owner is busy")
        return SimpleNamespace(transport_reply="Accepted after deferral")

    desk = daemon._desk(cast(ConversationService, SimpleNamespace(run_turn=busy)))
    assert desk is not None
    desk.drain()

    assert queued.exists()
    assert not list(inbox_dir.glob("*.failed"))
    assert not Path(config.desk.events_file).exists()
    desk.drain()
    assert attempts == ["busy", "busy"]
    assert not queued.exists()
    assert "Accepted after deferral" in Path(config.desk.events_file).read_text()


# 42 is configured, 582 and 4568 are not, and 0 is General — the topic every
# message sent outside a thread lands in, and the one a live instance's stranded
# results were owed to.
@pytest.mark.parametrize("topic_id", [42, 582, 4568, 0])
@pytest.mark.parametrize("restart_after_failure", [False, True])
def test_daemon_delivers_task_truth_to_the_telegram_topic_that_admitted_it(
    tmp_path, monkeypatch, topic_id, restart_after_failure
) -> None:
    sent: list[tuple[int, int, str]] = []
    delivered = threading.Event()
    failed = threading.Event()
    transport_unavailable = restart_after_failure

    class Telegram:
        def __init__(self, config, *_args, **_kwargs):
            self.config = config

        def start(self):
            return None

        def raise_if_failed(self):
            return None

        def send_reply(self, chat_id, topic_id, text):
            sent.append((chat_id, topic_id, text))
            delivered.set()

        def send_result(self, chat_id, topic_id, text, source_key):
            if transport_unavailable:
                failed.set()
                raise OSError("transport unavailable")
            self.send_reply(chat_id, topic_id, text)

        def stop(self):
            return None

        def request_stop(self):
            return None

    monkeypatch.setattr("steward_harness.daemon.TelegramService", Telegram)
    workdir = tmp_path / "work"
    workdir.mkdir()
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "Steward", "slug": "test"},
            "repositories": {"app": {"path": str(tmp_path / "repo"), "remote_url": "https://example.invalid/app.git"}},
            "provider": {
                "state_db": str(tmp_path / "state.db"),
                "workdir": str(workdir),
                "fallback_families": [],
            },
            "telegram": {
                "token_path": str(tmp_path / "token"),
                "chat_id": 99,
                "allowed_users": [7],
                "topics": {"steward": 42},
            },
        }
    )
    reviewer = ReadingAdapter("review-session", lambda request: "SILENT")
    def make_daemon():
        daemon = StewardDaemon(
            config, tmp_path / "steward.yaml", adapters={"codex": reviewer}
        )
        # No external service or split execution identity in this fixture.
        monkeypatch.setattr(daemon, "_require_boundary", lambda: None)
        daemon.broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
        return daemon

    state = StateDatabase(config.provider.state_db)
    task_id = _cancel_conversation_task(state, "telegram", str(topic_id))
    if restart_after_failure:
        with running(make_daemon()) as errors:
            assert failed.wait(5), errors
        receipt, = state.pending_result_receipts()
        assert "reply" in receipt
        assert not receipt.get("done")
        transport_unavailable = False

    daemon = make_daemon()
    with running(daemon) as errors:
        assert delivered.wait(5), errors

    assert len(reviewer.requests) == 1
    assert "Harness task result" in reviewer.requests[0].prompt
    assert len(sent) == 1
    assert sent[0][:2] == (99, topic_id)
    assert str(task_id) in sent[0][2]
    assert not state.pending_result_receipts()


def test_task_note_command_reports_pending_then_consumed_context(tmp_path):
    from steward_harness.state import CheckpointDisposition

    config = _config(tmp_path, default_family="codex", fallback_families=[])
    state = StateDatabase(config.provider.state_db)
    cognition = Cognition({})
    service = ConversationService(
        state, cognition, provider_order=("codex",), profile="balanced",
        workspace=tmp_path / "work", timeout_seconds=10,

    )
    commands = KernelCommands(
        config, state, service, SimpleNamespace(), {},
        UntrustedExecutionBroker(config.execution),
    )
    task = admit_task(
        state,
        TaskSpec("app", "Inspect consumer", "Return current source evidence."),
        provider="codex",
        profile="balanced",
    )
    reply = commands("task", f"note {task.task_id} Include the private consumer", 1, 42, 7)
    assert "pending input" in reply
    assert "Pending inputs: 1" in commands("task", f"show {task.task_id}", 1, 42, 7)
    assert tuple((k, t) for _, k, t, _ in state.tasks.get(task.task_id).pending)
    # Consumption is not a comparison any more: the slice read these inputs
    # and the commit it made is the record that it did, so closing it deletes
    # them.
    close_task_slice(state, task.task_id, CheckpointDisposition.CONTINUE,
        findings="steward: inspected private consumer",
    )
    reply = commands("task", f"show {task.task_id}", 1, 42, 7)
    assert "Pending inputs: 0" in reply
    # Checkpoint history is the disposition; the commit subject it used to
    # repeat is on the task branch, which is where git already keeps it.
    assert "Recent checkpoints:\n  continue" in reply


def _status_commands(tmp_path) -> KernelCommands:
    """A command surface over one real repository and a real lock directory."""
    repository = tmp_path / "app"
    repository.mkdir()
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "Steward", "slug": "test"},
            "provider": {
                "state_db": str(tmp_path / "state.db"),
                "workdir": str(tmp_path / "work"),
            },
            "repositories": {
                "app": {
                    "path": str(repository),
                    "remote_url": "https://example.invalid/app.git",
                }
            },
        }
    )
    Path(config.provider.workdir).mkdir(parents=True, exist_ok=True)
    return KernelCommands(
        config,
        StateDatabase(config.provider.state_db),
        cast(ConversationService, SimpleNamespace()),
        cast(RepositoryReconciler, SimpleNamespace()),
        {},
        UntrustedExecutionBroker(config.execution),
    )


def test_status_reports_the_steward_its_repositories_and_its_running_owners(
    tmp_path,
) -> None:
    """`/status` had no test at all, and had been raising on every invocation.

    It called `state.scheduled_owners()`, a method deleted in the promotions
    commit whose caller was left behind, so every `/status` since raised
    `AttributeError` and nothing noticed. This walks the whole reply: the
    identity, the pause flag read from durable state, the configured
    repositories, and the running set derived from the task locks.
    """
    commands = _status_commands(tmp_path)

    idle = commands("status", None, 1, 42, 7)
    assert idle == "Steward test: 1 repositories; 0 active owners: idle."

    lock = task_lock(
        commands.state.tasks.locks_root,
        TaskId("refresh-tokens"),
    )
    assert lock.acquire()
    try:
        busy = commands("status", None, 1, 42, 7)
        assert busy == "Steward test: 1 repositories; 1 active owners: refresh-tokens."
        commands("pause", None, 1, 42, 7)
        assert commands("status", None, 1, 42, 7).startswith("Steward test [PAUSED]:")
    finally:
        lock.release()

    commands("resume", None, 1, 42, 7)
    assert commands("status", None, 1, 42, 7) == idle




@pytest.mark.parametrize(
    "deferring_error",
    [
        Busy("busy (thread mutex contention)"),
        GitTransportError("could not read from remote repository"),
        WorldContentConflict("accepted world conflicts with retained workspace"),
        WorldUpdatePending("retained workspace no longer belongs to its world"),
        # Any agent-git command in this lane raises CalledProcessError, which is
        # a SubprocessError and so was caught by neither OSError nor
        # TimeoutExpired. Three live controller crashes in one day came through
        # here (`git commit --allow-empty` exit 1 twice, `git add --all` exit
        # 128 once), each killing the process from `git_world.finish` under
        # `deliver_task_result`.
        subprocess.CalledProcessError(1, ["git", "commit", "--allow-empty"]),
        subprocess.CalledProcessError(128, ["git", "add", "--all"]),
    ],
)
def test_result_delivery_defers_instead_of_killing_the_pass(
    tmp_path, monkeypatch, deferring_error
) -> None:
    """Nothing recoverable in this lane may reach `dispatch.reap`, which re-raises.

    Every other lane reads contention as "someone else has it, ask again next
    pass". The result lane did not, so an ordinary lease collision inside
    `deliver_task_result` propagated out of the worker and exited the process
    — observed on a live instance, ending a five-hour run.

    A transport failure is the same shape. `deliver_task_result` reaches a
    pack transfer on its way to `checkpoint.retain`, and an unreachable remote
    there says nothing about the settled task: the six other call sites that
    touch this boundary already log and return. Neither error queues anything,
    so `pending_task_result_for` hands back the same row on the next pass.

    The failure only appears on the pass *after* the one that submitted the
    work, because that is when `reap()` reads the future.
    """
    owner = ConversationId("desk:busy")
    calls: list[object] = []

    def busy(self, requested, **_kwargs):
        calls.append(requested)
        raise deferring_error

    monkeypatch.setattr(ConversationService, "deliver_task_result", busy)
    monkeypatch.setattr(
        StateDatabase, "pending_task_result_conversations", lambda self: (owner,)
    )
    world = make_world(tmp_path / "world")
    native = tmp_path / "codex-home"
    native.mkdir()
    base = _config(
        tmp_path, fallback_families=[], native_homes={"codex": str(native)}
    ).model_dump()
    config = StewardConfig.model_validate({
        **base, "world": {"root": str(world.root)},
        "desk": {"inbox_dir": str(tmp_path / "inbox"),
                 "events_file": str(tmp_path / "events.jsonl")},
    })
    daemon = StewardDaemon(config, tmp_path / "steward.yaml")
    with daemon._daemon_lease():
        try:
            step = daemon._start_owned()
            for _ in range(4):
                step()
                time.sleep(0.05)
        finally:
            daemon.stop()
    assert calls, "the result lane never ran"


def test_native_activity_reads_unpublished_commits_across_repositories(tmp_path):
    from steward_harness.daemon import _native_git_heads
    from test_task_runner_kernel import _repository, _git

    roots = {}
    for name in ("one", "two"):
        directory = tmp_path / name
        directory.mkdir()
        _, roots[name] = _repository(directory)
    config = SimpleNamespace(repositories={
        name: SimpleNamespace(path=str(root)) for name, root in roots.items()
    })
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    before = _native_git_heads(config, broker)
    worktree = tmp_path / "active-task"
    _git("worktree", "add", "-b", "tasks/native-edit", str(worktree), cwd=roots["two"])
    _git("commit", "--allow-empty", "-m", "native work before checkpoint", cwd=worktree)
    after = _native_git_heads(config, broker)
    assert after["native:one:refs/heads/main"] == before["native:one:refs/heads/main"]
    assert after["native:two:refs/heads/tasks/native-edit"] != before["native:two:refs/heads/main"]
    assert after["native:two:refs/heads/tasks/native-edit"] == _git("rev-parse", "HEAD", cwd=worktree)


def test_rhythm_list_shows_quiet_policy_and_configured_runs_without_history(tmp_path):
    from steward_harness.config.schema import ProcedureRhythmConfig

    commands = _status_commands(tmp_path)
    commands.config.rhythms["light"] = ProcedureRhythmConfig(
        schedule={"quiet": 300}, procedure="reflect", input="repositories/app/main", owner=None)
    commands.config.rhythms["daily"] = ProcedureRhythmConfig(
        schedule=86400, procedure="consolidate", input="repositories/app/main", owner=None)
    output = commands("rhythm", "list", 1, 42, 7)
    assert "light: after 300s of quiet Git activity" in output
    assert "daily: every 86400s" in output
    assert output.count("no accepted run recorded") == 2
    commands.state.set_paused(True)
    assert commands("rhythm", "list", 1, 42, 7).count("automatic admission paused") == 2


def test_deferral_cause_names_the_subprocess_failure() -> None:
    """A deferral must be able to state why, not just that a command exited."""
    from steward_harness.daemon import deferral_cause

    failure = subprocess.CalledProcessError(
        128, ["git", "commit", "--allow-empty"],
        output=b"", stderr=b"error: insufficient permission for adding an object\n",
    )
    # `str()` alone is structurally incapable of carrying the reason: this is
    # what 227 world-checkpoint deferrals logged instead of their cause.
    assert "insufficient permission" not in str(failure)
    cause = deferral_cause(failure)
    assert "insufficient permission for adding an object" in cause
    assert "exit status 128" in cause


def test_deferral_cause_redacts_credentials_from_captured_output() -> None:
    """Git stderr reaches the journal, and can quote an authenticated remote."""
    from steward_harness.daemon import deferral_cause

    failure = subprocess.CalledProcessError(
        128, ["git", "push"],
        stderr="fatal: could not read from https://x-access-token:ghs_SECRET@github.com/o/r\n",
    )
    cause = deferral_cause(failure)
    assert "ghs_SECRET" not in cause
    assert "x-access-token" not in cause
    assert "could not read from" in cause


def test_deferral_cause_leaves_an_error_without_captured_output_alone() -> None:
    from steward_harness.daemon import deferral_cause

    assert deferral_cause(OSError("result transport unavailable")) == (
        "result transport unavailable"
    )

def _result_pass(tmp_path, config, state):
    """Real pass and receipt service, with a captured executor and no network."""
    daemon = StewardDaemon(config, tmp_path / "steward.yaml")
    queued = []
    kernel = SimpleNamespace(
        dispatch=SimpleNamespace(submit=lambda key, work: queued.append((key, work)),
                                 reap=lambda: None),
        owners=lambda: (), tasks=SimpleNamespace(flush_inputs=lambda: None),
    )
    service = object.__new__(ConversationService)
    service._state = state
    step = daemon._pass(state, service, kernel, SimpleNamespace(), None)
    return daemon, queued, step


def test_invalid_result_route_retains_receipt_and_config_repair_drains_it(tmp_path):
    config = StewardConfig.model_validate({
        **_config(tmp_path).model_dump(),
        "telegram": {"chat_id": 1, "allowed_users": [7], "topics": {"operator": 42}},
    })
    state = StateDatabase(config.provider.state_db)
    for sequence in (1, 2):
        state.save_result_receipt({
            "owner": "telegram:99", "task_id": None, "target": "app",
            "sequence": sequence, "source_key": f"target_result:{sequence}",
            "result_text": f"Observation {sequence}", "reply": f"Report {sequence}",
        })
    daemon, queued, step = _result_pass(tmp_path, config, state)
    sent = []
    daemon._telegram = SimpleNamespace(config=config.telegram,
                                       send_result=lambda *args: sent.append(args))
    step()
    step()
    assert not [key for key, _ in queued if key[0] == "result"]
    assert state.result_receipt("target_result:1")["delivery_error"]
    assert not state.result_receipt("target_result:1").get("done")
    assert sent == []
    config.telegram.topics["repaired"] = 99
    for _ in range(2):
        queued.clear()
        step()
        for key, work in queued:
            if key[0] == "result":
                work()
    assert [args[2] for args in sent] == ["Report 1", "Report 2"]
    assert state.pending_result_receipts() == []
    assert "delivery_error" not in state.result_receipt("target_result:1")


def test_unowned_target_waits_for_operator_route_then_delivers_without_task(tmp_path):
    config = _config(tmp_path)
    state = StateDatabase(config.provider.state_db)
    state.save_result_receipt({
        "owner": None, "task_id": None, "target": "external",
        "source_key": "target_result:external", "result_text": "External deployment failed",
        "reply": "External deployment failed",
    })
    daemon, queued, step = _result_pass(tmp_path, config, state)
    step()
    retained = state.result_receipt("target_result:external")
    assert retained["owner"] is None
    assert retained["delivery_error"] == "no configured operator result route"
    assert not [key for key, _ in queued if key[0] == "result"]
    daemon.config = StewardConfig.model_validate({
        **config.model_dump(),
        "telegram": {"chat_id": 1, "allowed_users": [7],
                     "topics": {"operator": 42, "incidents": 43}},
    })
    sent = []
    daemon._telegram = SimpleNamespace(config=daemon.config.telegram,
                                       send_result=lambda *args: sent.append(args))
    queued.clear()
    step()
    for key, work in queued:
        if key[0] == "result":
            work()
    assert sent == [(1, 43, "External deployment failed", "target_result:external")]
    assert state.result_receipt("target_result:external")["done"]
    assert state.tasks.all() == []


def test_status_exposes_undeliverable_receipts(tmp_path):
    commands = _status_commands(tmp_path)
    commands.state.save_result_receipt({
        "owner": "telegram:99", "task_id": None, "source_key": "target_result:missing",
        "result_text": "Retained", "delivery_error": "route is not a configured Telegram topic",
    })
    reply = commands("status", None, 1, 42, 7)
    assert "Undeliverable results: telegram:99: route is not a configured Telegram topic" in reply


def test_result_diagnostic_write_failure_defers_discovery(tmp_path, monkeypatch):
    config = _config(tmp_path)
    state = StateDatabase(config.provider.state_db)
    state.save_result_receipt({
        "owner": None, "task_id": None, "target": "external",
        "source_key": "target_result:external", "result_text": "Retained",
    })
    _, queued, step = _result_pass(tmp_path, config, state)
    def unavailable(_receipt):
        raise OSError("receipt store temporarily unavailable")
    monkeypatch.setattr(state, "save_result_receipt", unavailable)
    step()
    assert not [key for key, _ in queued if key[0] == "result"]
    assert not state.result_receipt("target_result:external").get("done")
