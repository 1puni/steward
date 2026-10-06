"""Slack admission and restart boundaries use the shared inbox and source drain."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from steward_harness.config.schema import SlackConfig, StewardConfig
from steward_harness.inbox import InboxDrain, settle
from steward_harness.slack.service import SlackService


def config(**fields):
    return SlackConfig(**(dict(
        team_id="T123", channel_id="C123", users={"U123": "operator", "U456": "observer"},
        bot_token_path="/etc/steward/slack-bot", app_token_path="/etc/steward/slack-app",
    ) | fields))


def event(number=1, **fields):
    return dict(type="event_callback", team_id="T123", event_id=f"Ev{number}", event=dict(
        type="message", channel="C123", user="U123", ts=f"1234567890.{number:06}", text="hello",
    ) | fields)


@pytest.fixture
def transport(tmp_path):
    api = Mock()
    api.call.side_effect = lambda method, **kwargs: dict(channel=kwargs["channel"], ts="1234567891.000001")
    turn = Mock(return_value="Turn reply")
    command = Mock(return_value="Command reply")
    state = SimpleNamespace(path=tmp_path / "state.db")

    def make(cfg=None):
        return SlackService(cfg or config(), state=state, turn_handler=turn,
                            command_handler=command, api=api)

    return SimpleNamespace(make=make, service=make(), api=api, turn=turn, command=command)


def answer_one(service):
    [queued] = service.inbox.pending()
    claimed = service.inbox.claim(queued)
    assert settle(service.source, claimed)
    return claimed


@pytest.mark.parametrize("fields", [
    dict(user="U999"), dict(channel="C999"), dict(user_team="T999"), dict(bot_id="B123"),
    dict(app_id="A123"), dict(hidden=True), dict(subtype="message_changed"),
    dict(type="app_mention"), dict(ts="../etc"), dict(thread_ts="not-a-timestamp"),
    dict(text="x" * 8001), dict(text=[]), dict(user=[]), dict(user={}),
])
def test_unadmitted_events_create_no_work(transport, fields):
    transport.service.ingest(event(**fields))
    assert not transport.service.inbox.pending()
    transport.turn.assert_not_called()
    transport.command.assert_not_called()


@pytest.mark.parametrize("fields", [dict(team_id="T999"), dict(event_id="../escape"), dict(type="other")])
def test_outer_envelope_cannot_cross_authority_boundary(transport, fields):
    transport.service.ingest(event() | fields)
    assert not transport.service.inbox.pending()


def test_socket_ack_follows_retention_and_storage_failure_is_not_acknowledged(transport, monkeypatch):
    pytest.importorskip("slack_sdk")
    service = transport.service
    request = SimpleNamespace(type="events_api", payload=event(), envelope_id="envelope")
    client = Mock()

    def ack(response):
        assert response.envelope_id == "envelope"
        [queued] = service.inbox.pending()
        assert queued.record["payload"] == request.payload

    client.send_socket_mode_response.side_effect = ack
    service.receive(client, request)
    client.send_socket_mode_response.assert_called_once()
    client.reset_mock()
    monkeypatch.setattr(service.inbox, "put", Mock(side_effect=OSError("disk full")))
    request.payload = event(2)
    with pytest.raises(OSError, match="disk full"):
        service.receive(client, request)
    client.send_socket_mode_response.assert_not_called()
    service.request_stop()
    service.receive(client, request)
    client.send_socket_mode_response.assert_not_called()


@pytest.mark.parametrize("text", [
    "do work", "!pause", "!resume", "!model deep", "!clear", "!cancel",
    "!task cancel task123", "!task show task123 extra", "!rhythm run daily", "!git reconcile repo",
])
def test_observer_cannot_start_a_turn_or_mutation(transport, text):
    transport.service.ingest(event(user="U456", text=text))
    answer_one(transport.service)
    transport.turn.assert_not_called()
    transport.command.assert_not_called()
    assert transport.api.call.call_args.kwargs["text"]


@pytest.mark.parametrize("text,name,arg", [
    ("!help", "help", None), ("!status", "status", None), ("!tasks", "tasks", None),
    ("!task show task123", "task", "show task123"),
])
def test_observer_can_read_explicit_commands(transport, text, name, arg):
    transport.service.ingest(event(user="U456", text=text))
    answer_one(transport.service)
    transport.command.assert_called_once_with(name, arg, "T123:C123:1234567890.000001", "U456")
    transport.turn.assert_not_called()


def test_operator_turn_keeps_thread_and_author_identity(transport):
    transport.service.ingest(event(thread_ts="1234567800.000001"))
    answer_one(transport.service)
    args = transport.turn.call_args.args
    assert args[:3] == ("Ev1", "T123:C123:1234567800.000001", "T123:U123")
    assert json.loads(args[3].splitlines()[0].removeprefix("Slack sender: ")) == dict(
        transport="slack", team="T123", user="U123", role="operator")
    assert args[3].endswith("\n\nhello")
    assert transport.api.call.call_args.kwargs["thread_ts"] == "1234567800.000001"


def test_inbound_files_are_refused_without_cognition(transport):
    transport.service.ingest(event(subtype="file_share", files=[dict(id="F123")]))
    answer_one(transport.service)
    transport.turn.assert_not_called()
    assert "not supported" in transport.api.call.call_args.kwargs["text"]


@pytest.mark.parametrize("state", ["queued", "claimed", "failed", "rejected", "done"])
def test_duplicate_event_preserves_every_retained_state_across_restart(transport, state):
    service = transport.service
    service.ingest(event())
    [message] = service.inbox.pending()
    if state in {"claimed", "failed", "done"}:
        message = service.inbox.claim(message)
    if state == "failed":
        service.inbox.park_failed(message)
    elif state == "rejected":
        message.path.rename(message.path.with_suffix(".rejected"))
    elif state == "done":
        service.inbox.done(message)
    before = {path.name: path.read_bytes() for path in service.inbox.dir.iterdir()}
    restarted = transport.make()
    restarted.ingest(event(text="duplicate must not replace the first event"))
    assert {path.name: path.read_bytes() for path in restarted.inbox.dir.iterdir()} == before
    transport.turn.assert_not_called()


def test_concurrent_duplicate_callbacks_retain_and_execute_one_event(transport):
    service = transport.service
    with ThreadPoolExecutor(max_workers=8) as callbacks:
        list(callbacks.map(service.ingest, [event()] * 20))
    answer_one(service)
    transport.turn.assert_called_once()
    transport.api.call.assert_called_once()


def test_restart_requeues_orphan_and_completed_event_never_reexecutes(transport):
    service = transport.service
    service.ingest(event())
    service.inbox.claim(service.inbox.pending()[0])
    restarted = transport.make()
    assert restarted.inbox.recover() == 1
    restarted.ingest(event())
    answer_one(restarted)
    restarted_again = transport.make()
    restarted_again.ingest(event())
    assert restarted_again.inbox.pending() == []
    transport.turn.assert_called_once()
    transport.api.call.assert_called_once()


def test_restart_rechecks_revoked_sender_before_execution(transport):
    transport.service.ingest(event())
    restarted = transport.make(config(users={"U456": "operator"}))
    answer_one(restarted)
    transport.turn.assert_not_called()
    transport.command.assert_not_called()
    transport.api.call.assert_not_called()


def test_restart_rechecks_downgraded_operator_before_mutation(transport):
    transport.service.ingest(event(text="!pause"))
    restarted = transport.make(config(users={"U123": "observer", "U456": "operator"}))
    answer_one(restarted)
    transport.turn.assert_not_called()
    transport.command.assert_not_called()
    assert "requires an explicitly configured operator" in transport.api.call.call_args.kwargs["text"]


def test_interrupted_mutating_command_is_not_replayed(transport):
    class ProcessDied(BaseException):
        pass

    transport.command.side_effect = ProcessDied()
    service = transport.service
    service.ingest(event(text="!pause"))
    claimed = service.inbox.claim(service.inbox.pending()[0])
    with pytest.raises(ProcessDied):
        service.answer(claimed)
    assert json.loads(claimed.path.read_text())["command_started"] is True
    transport.command.assert_called_once()
    transport.api.call.assert_not_called()
    restarted = transport.make()
    assert restarted.inbox.recover() == 1
    answer_one(restarted)
    transport.command.assert_called_once()
    assert "Inspect its effect" in transport.api.call.call_args.kwargs["text"]


def test_separate_threads_run_concurrently_and_same_thread_stays_ordered(transport):
    service = transport.service
    first_started, other_started, release_first, same_started = (threading.Event() for _ in range(4))
    order = []

    def turn(msg_id, route, author, text):
        order.append(msg_id)
        if msg_id == "Ev1":
            first_started.set()
            assert release_first.wait(5)
        elif msg_id == "Ev2":
            same_started.set()
        elif msg_id == "Ev3":
            other_started.set()
        return "done"

    transport.turn.side_effect = turn
    service.ingest(event())
    service.ingest(event(2, thread_ts="1234567890.000001"))
    service.ingest(event(3))
    messages = service.inbox.pending()
    keys = [service.source.conversation_key(message) for message in messages]
    assert keys == ["T123:C123:1234567890.000001", "T123:C123:1234567890.000001", "T123:C123:1234567890.000003"]
    drain = InboxDrain([service.source], workers=2)
    drain.start()
    try:
        assert first_started.wait(5)
        assert other_started.wait(5), "a different Slack thread was blocked by the first thread"
        assert not same_started.is_set(), "same-thread work overlapped the earlier turn"
        release_first.set()
        assert same_started.wait(5)
    finally:
        release_first.set()
        drain.stop()
    assert order.index("Ev1") < order.index("Ev2")


@pytest.mark.parametrize("field", ["bot_token_path", "app_token_path"])
@pytest.mark.parametrize("root", ["/home/worker", "/srv/product", "/srv/world", "/srv/work", "/srv/native"])
def test_slack_token_paths_stay_outside_model_writable_roots(field, root):
    document = dict(
        identity=dict(name="test", slug="test"), execution=dict(user="worker", home="/home/worker"),
        provider=dict(workdir="/srv/work", native_homes={"codex": "/srv/native"}),
        repositories={"app": dict(path="/srv/product", remote_url="git@example.com:org/app.git")},
        world=dict(root="/srv/world"), slack=config(**{field: root + "/token"}).model_dump(),
    )
    with pytest.raises(ValidationError, match="slack." + field):
        StewardConfig.model_validate(document)


@pytest.mark.parametrize("fields", [
    dict(users={}), dict(users={"U456": "observer"}), dict(users={"U123": "admin"}),
    dict(team_id="TPLACEHOLDER"), dict(channel_id="CPLACEHOLDER"), dict(users={"UPLACEHOLDER": "operator"}),
    dict(bot_token_path="relative"), dict(app_token_path="/"),
    dict(app_token_path="/etc/steward/slack-bot"), dict(bot_token="xoxb-inline"),
    dict(notifications={"operator": "T999:C123:1234567890.000001"}),
])
def test_config_refuses_implicit_authority_and_unsafe_token_references(fields):
    with pytest.raises(ValidationError):
        config(**fields)


def test_slack_authority_requires_execution_boundary():
    cfg = StewardConfig.model_validate(dict(identity=dict(name="test", slug="test"), slack=config().model_dump()))
    assert cfg.requires_execution_boundary


def test_cancel_overtakes_busy_turn_in_its_thread(transport):
    service = transport.service
    running, cancelled, release = (threading.Event() for _ in range(3))

    def turn(*args):
        running.set()
        assert release.wait(5)
        return "Interrupted"

    def command(name, arg, route, user):
        assert name == "cancel"
        assert running.is_set()
        assert route == "T123:C123:1234567890.000001"
        cancelled.set()
        release.set()
        return "Cancellation requested"

    transport.turn.side_effect = turn
    transport.command.side_effect = command
    service.ingest(event())
    drain = InboxDrain([service.source], workers=2)
    drain.start()
    try:
        assert running.wait(5)
        service.ingest(event(2, text="!cancel", thread_ts="1234567890.000001"))
        assert cancelled.wait(5), "cancel was queued behind the turn it must interrupt"
    finally:
        release.set()
        drain.stop()
    transport.turn.assert_called_once()
    transport.command.assert_called_once()


@pytest.mark.parametrize("variable", ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_TOKEN"])
def test_slack_credentials_cannot_be_inherited_by_untrusted_execution(variable):
    with pytest.raises(ValidationError, match="cannot be inherited"):
        StewardConfig.model_validate(dict(
            identity=dict(name="test", slug="test"),
            execution=dict(user="worker", inherited_environment=["PATH", variable]),
            slack=config().model_dump(),
        ))


@pytest.mark.parametrize("earlier,later", [
    ("9.999999", "10.000000"),
    ("12345678901234567890.000001", "12345678901234567890.000002"),
])
def test_thread_order_uses_exact_slack_timestamps_not_event_ids(transport, earlier, later):
    service = transport.service
    answered = []
    complete = threading.Event()

    def turn(msg_id, *args):
        answered.append(msg_id)
        if len(answered) == 2:
            complete.set()
        return "done"

    transport.turn.side_effect = turn
    service.ingest(event(ts=later, thread_ts="1234567890.000001") | {"event_id": "EvA"})
    service.ingest(event(ts=earlier, thread_ts="1234567890.000001") | {"event_id": "EvZ"})
    drain = InboxDrain([service.source], workers=2)
    drain.start()
    try:
        assert complete.wait(5)
    finally:
        drain.stop()
    assert answered == ["EvZ", "EvA"]


def test_socket_cancel_acks_before_mutation_with_all_drain_workers_busy(transport):
    pytest.importorskip("slack_sdk")
    service = transport.service
    running, cancelled, release = (threading.Event() for _ in range(3))
    boundary = []

    def turn(*args):
        running.set()
        assert release.wait(5)
        return "Interrupted"

    def ack(response):
        assert response.envelope_id == "cancel-envelope"
        assert (service.inbox.dir / "Ev2.json.claimed").exists()
        transport.command.assert_not_called()
        boundary.append("ack")

    def command(name, arg, route, user):
        assert name == "cancel"
        assert running.is_set()
        assert boundary == ["ack"]
        boundary.append("mutation")
        cancelled.set()
        release.set()
        return "Cancellation requested"

    transport.turn.side_effect = turn
    transport.command.side_effect = command
    client = Mock()
    client.send_socket_mode_response.side_effect = ack
    request = SimpleNamespace(type="events_api", envelope_id="cancel-envelope",
                              payload=event(2, text="!cancel", thread_ts="1234567890.000001"))
    service.ingest(event())
    drain = InboxDrain([service.source], workers=1)
    drain.start()
    try:
        assert running.wait(5)
        service.receive(client, request)
        assert cancelled.is_set(), "Socket callback did not cancel while the sole drain worker was busy"
    finally:
        release.set()
        drain.stop()
    assert boundary == ["ack", "mutation"]
    assert (service.inbox.dir / "Ev2.json.done").exists()
    transport.command.assert_called_once()


@pytest.mark.parametrize("private_field,delivery_transport,private_path", [
    ("slack.bot_token_path", "telegram", "/exports/telegram/slack-bot"),
    ("slack.app_token_path", "telegram", "/exports/telegram/slack-app"),
    ("telegram.token_path", "slack", "/exports/slack/telegram-token"),
    ("provider.state_db", "slack", "/exports/slack/state.db"),
    ("provider.state_db", "telegram", "/exports/telegram/state.db"),
])
def test_neither_transport_can_upload_the_others_credentials_or_state(private_field, delivery_transport, private_path):
    document = dict(
        identity=dict(name="test", slug="test"),
        execution=dict(user="worker", home="/home/worker"),
        provider=dict(state_db="/controller/state/state.db", workdir="/srv/work"),
        slack=config(delivery_roots=("/exports/slack",)).model_dump(),
        telegram=dict(chat_id=1, allowed_users=[1], token_path="/controller/telegram-token",
                      inbound_media_dir="/controller/media", delivery_roots=["/exports/telegram"]),
    )
    block, field = private_field.split(".")
    document[block][field] = private_path
    assert private_path.startswith(document[delivery_transport]["delivery_roots"][0] + "/")
    with pytest.raises(ValidationError, match="outside delivery roots"):
        StewardConfig.model_validate(document)


@pytest.mark.parametrize("transport", ["slack", "telegram"])
def test_delivery_root_cannot_descend_into_controller_state_directory(transport):
    document = dict(
        identity=dict(name="test", slug="test"),
        execution=dict(user="worker", home="/home/worker"),
        provider=dict(state_db="/controller/state/state.db", workdir="/srv/work"),
        slack=config().model_dump(),
        telegram=dict(chat_id=1, allowed_users=[1], token_path="/controller/telegram-token",
                      inbound_media_dir="/controller/media"),
    )
    document[transport]["delivery_roots"] = ["/controller/state/deliverable"]
    with pytest.raises(ValidationError, match="controller state directory.*outside delivery roots"):
        StewardConfig.model_validate(document)


def test_failed_ack_still_processes_retained_control_once(transport):
    pytest.importorskip("slack_sdk")
    service = transport.service
    request = SimpleNamespace(type="events_api", envelope_id="failed-ack", payload=event(text="!pause"))
    client = Mock()
    client.send_socket_mode_response.side_effect = OSError("ACK connection lost")
    with pytest.raises(OSError, match="ACK connection lost"):
        service.receive(client, request)
    transport.command.assert_called_once_with("pause", None, "T123:C123:1234567890.000001", "U123")
    assert (service.inbox.dir / "Ev1.json.done").exists()
    assert not list(service.inbox.dir.glob("*.claimed"))
    transport.api.call.assert_called_once()
    client.send_socket_mode_response.side_effect = None
    service.receive(client, request)
    assert client.send_socket_mode_response.call_count == 2
    transport.command.assert_called_once()
    transport.api.call.assert_called_once()
    transport.turn.assert_not_called()


@pytest.mark.parametrize("field", ["ts", "thread_ts"])
def test_numeric_timestamp_is_rejected_even_when_string_conversion_looks_valid(transport, field):
    timestamp = 1234567890.123456
    assert str(timestamp) == "1234567890.123456"
    transport.service.ingest(event(**{field: timestamp}))
    assert not transport.service.inbox.pending()
    transport.turn.assert_not_called()
    transport.command.assert_not_called()


@pytest.mark.parametrize("private", ["slack_token", "telegram_token", "state_directory"])
def test_delivery_credentials_are_protected_without_configured_execution_user(private):
    document = dict(
        identity=dict(name="test", slug="test"),
        provider=dict(state_db="/controller/state/state.db"),
        slack=config().model_dump(),
        telegram=dict(chat_id=1, allowed_users=[1], token_path="/controller/telegram-token"),
    )
    if private == "slack_token":
        document["telegram"]["delivery_roots"] = ["/etc/steward"]
    elif private == "telegram_token":
        document["slack"]["delivery_roots"] = ["/controller/telegram-token"]
    else:
        document["slack"]["delivery_roots"] = ["/controller/state/artifacts"]
    with pytest.raises(ValidationError, match="outside delivery roots"):
        StewardConfig.model_validate(document)
