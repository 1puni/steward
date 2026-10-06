"""Slack ingress and retained results share the daemon's owners and lifecycle."""
from types import SimpleNamespace
import threading

import pytest

from state_fixtures import admit_task
from test_daemon import _result_pass, _status_commands
from steward_harness.config.schema import StewardConfig, UntrustedExecutionConfig
from steward_harness.conversations import ConversationService
from steward_harness.daemon import StewardDaemon
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.slack.commands import slack_command
from steward_harness.slack.service import SlackContentRejected, SlackDeliveryUnknown, SlackService
from steward_harness.state import StateDatabase, TaskSpec


ROUTE = "T123:C123:1700000000.000001"
OTHER_ROUTE = "T123:C123:1700000000.000002"


def config_for(tmp_path):
    (tmp_path / "work").mkdir(exist_ok=True)
    return StewardConfig.model_validate({
        "identity": {"name": "Steward", "slug": "test"},
        "provider": {"state_db": str(tmp_path / "state.db"),
                     "workdir": str(tmp_path / "work"), "fallback_families": []},
        "slack": {"team_id": "T123", "channel_id": "C123",
                  "bot_token_path": str(tmp_path / "bot-token"),
                  "app_token_path": str(tmp_path / "app-token"),
                  "users": {"U123": "operator", "U456": "observer"},
                  "notifications": {"operator": ROUTE}},
        "telegram": {"chat_id": 1, "allowed_users": [7], "topics": {"operator": 42}},
    })


def receipt(state, key="notice:1", *, owner="slack:" + ROUTE, **extra):
    state.save_result_receipt({"owner": owner, "task_id": None, "source_key": key,
                               "result_text": "Retained result", "reply": "Retained result", **extra})


def results(queued, step):
    queued.clear()
    step()
    for key, work in queued:
        if key[0] == "result":
            work()


@pytest.mark.parametrize("owner", ["slack:" + OTHER_ROUTE, "telegram:42"])
def test_retained_owner_does_not_follow_notification_preference(tmp_path, owner):
    config = config_for(tmp_path)
    state = StateDatabase(config.provider.state_db)
    receipt(state, owner=owner)
    daemon, queued, step = _result_pass(tmp_path, config, state)
    sent = []
    daemon._slack = SimpleNamespace(send_result=lambda *args: sent.append(("slack", args)))
    daemon._telegram = SimpleNamespace(config=config.telegram,
        send_result=lambda *args: sent.append(("telegram", args)))
    results(queued, step)
    assert sent == ([("slack", (OTHER_ROUTE, "Retained result", "notice:1"))]
                    if owner.startswith("slack:") else
                    [("telegram", (1, 42, "Retained result", "notice:1"))])
    assert state.result_receipt("notice:1")["owner"] == owner
    assert state.result_receipt("notice:1")["done"]


def test_missing_slack_never_falls_back_to_telegram_and_recovers(tmp_path):
    config = config_for(tmp_path)
    state = StateDatabase(config.provider.state_db)
    receipt(state)
    daemon, queued, step = _result_pass(tmp_path, config, state)
    daemon._telegram = SimpleNamespace(config=config.telegram,
        send_result=lambda *args: pytest.fail("Slack result migrated to Telegram"))
    results(queued, step)
    assert state.result_receipt("notice:1")["delivery_error"] == "Slack transport unavailable"
    assert not state.result_receipt("notice:1").get("done")
    sent = []
    daemon._slack = SimpleNamespace(send_result=lambda *args: sent.append(args))
    results(queued, step)
    assert sent == [(ROUTE, "Retained result", "notice:1")]
    assert state.result_receipt("notice:1")["done"]
    assert "delivery_error" not in state.result_receipt("notice:1")


@pytest.mark.parametrize("route", ["T999:C123:1700000000.000001", "T123:C999:1700000000.000001",
                                   "T123:C123:invalid"])
def test_slack_route_must_match_configured_boundary(tmp_path, route):
    config = config_for(tmp_path)
    state = StateDatabase(config.provider.state_db)
    receipt(state, owner="slack:" + route)
    daemon, queued, step = _result_pass(tmp_path, config, state)
    daemon._slack = SimpleNamespace(send_result=lambda *args: pytest.fail("invalid route sent"))
    results(queued, step)
    retained = state.result_receipt("notice:1")
    assert "configured Slack workspace/channel" in retained["delivery_error"]
    assert not retained.get("done")
    assert retained["owner"] == "slack:" + route


def test_unowned_result_and_incident_notice_retain_configured_slack_owner(tmp_path):
    config = config_for(tmp_path)
    state = StateDatabase(config.provider.state_db)
    receipt(state, owner=None)
    daemon, queued, step = _result_pass(tmp_path, config, state)
    daemon._state = state
    daemon._notify_topic("incidents", "Probe failed")
    notice = next(r for r in state.pending_result_receipts() if r["source_key"] != "notice:1")
    assert notice["owner"] == "slack:" + ROUTE
    assert notice["reply"] == "Probe failed"
    results(queued, step)  # Ingress is down; configured ownership still persists.
    assert state.result_receipt("notice:1")["owner"] == "slack:" + ROUTE
    assert all(not r.get("done") for r in state.pending_result_receipts())
    assert state.tasks.all() == []


def test_unknown_delivery_retains_progress_and_exposes_error(tmp_path):
    config = config_for(tmp_path)
    state = StateDatabase(config.provider.state_db)
    receipt(state)
    daemon, queued, step = _result_pass(tmp_path, config, state)

    attempts = []

    def uncertain(method, **kwargs):
        attempts.append(method)
        raise SlackDeliveryUnknown("Response lost after posting")

    daemon._slack = SlackService(config.slack, state=state, turn_handler=None,
                                 command_handler=None, api=SimpleNamespace(call=uncertain))
    results(queued, step)
    results(queued, step)
    retained = state.result_receipt("notice:1")
    assert not retained.get("done")
    assert retained["slack_sending"] == 0
    assert "outcome unknown" in retained["delivery_error"]
    assert attempts == ["chat_postMessage"]


def test_permanent_slack_refusal_settles_and_next_result_progresses(tmp_path):
    config = config_for(tmp_path)
    state = StateDatabase(config.provider.state_db)
    receipt(state)
    receipt(state, "notice:2")
    daemon, queued, step = _result_pass(tmp_path, config, state)
    sent = []

    def send(route, text, key):
        if key == "notice:1":
            raise SlackContentRejected("Slack rejected request: is_archived")
        sent.append(key)

    daemon._slack = SimpleNamespace(send_result=send)
    results(queued, step)
    results(queued, step)
    assert state.result_receipt("notice:1")["done"]
    assert "is_archived" in state.result_receipt("notice:1")["rejected"]
    assert sent == ["notice:2"]
    assert state.pending_result_receipts() == []


def test_slack_commands_render_cards_without_telegram_links_and_use_thread_owner(tmp_path):
    commands = _status_commands(tmp_path)
    commands.config = config_for(tmp_path)
    task = admit_task(commands.state, TaskSpec("app", "Fix output", "Preserve attachments."))
    shown = slack_command(commands, "task", f"show {task.task_id.short}", ROUTE)
    assert "Preserve attachments." in shown
    assert f"!task note {task.task_id.short}" in shown
    assert "t.me" not in shown and "/task" not in shown
    slack = commands.state.get_or_create_conversation("slack", ROUTE, provider="codex", profile="deep")
    telegram = commands.state.get_or_create_conversation("telegram", ROUTE, provider="codex", profile="fast")
    assert "codex/deep" in slack_command(commands, "model", None, ROUTE)
    slack_command(commands, "clear", None, ROUTE)
    assert commands.state.get_conversation(slack.conversation_id).generation == slack.generation + 1
    assert commands.state.get_conversation(telegram.conversation_id).generation == telegram.generation


def test_startup_uses_shared_inbox_and_shutdown_drains_reply_before_slack_closes(tmp_path, monkeypatch):
    config = config_for(tmp_path).model_copy(update={"telegram": None})
    calls, order = [], []
    sending, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def turn(_service, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(transport_reply="A retained reply")

    def api_call(method, **kwargs):
        assert method == "chat_postMessage"
        sending.set()
        assert release.wait(5), "test did not release the outbound send"
        order.append("sent")
        return {"channel": "C123", "ts": "1700000001.000001"}

    def start(service):
        service.api = SimpleNamespace(call=api_call)
        service.inbox.recover()

    stop_slack = SlackService.stop

    def close(service):
        order.append("closed")
        stop_slack(service)

    monkeypatch.setattr(ConversationService, "run_turn", turn)
    monkeypatch.setattr(SlackService, "start", start)
    monkeypatch.setattr(SlackService, "stop", close)
    daemon = StewardDaemon(config, tmp_path / "steward.yaml", adapters={},
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()))
    stop_thread = None
    try:
        daemon._start_owned()
        assert [source.inbox.dir for source in daemon._inbox.sources] == [daemon._slack.inbox.dir]
        daemon._slack.ingest({"type": "event_callback", "event_id": "Ev123", "team_id": "T123",
            "event": {"type": "message", "channel": "C123", "user": "U123",
                      "ts": "1700000000.000001", "text": "Do the work"}})
        assert sending.wait(5), "shared inbox did not answer Slack"
        assert calls == [{"transport": "slack", "transport_key": ROUTE,
                          "source_event_key": "Ev123", "operator_id": "T123:U123", "text": "Do the work"}]
        stop_thread = threading.Thread(target=lambda: (daemon.stop(), stopped.set()))
        stop_thread.start()
        assert daemon._slack._stop.wait(2), "shutdown did not stop ingress first"
        assert not stopped.is_set() and "closed" not in order
        release.set()
        stop_thread.join(5)
        assert stopped.is_set()
        assert order == ["sent", "closed"]
        assert daemon._slack.inbox.holds("Ev123")  # completed event still owns dedupe
    finally:
        release.set()
        if stop_thread is not None:
            stop_thread.join(5)
        else:
            daemon.stop()
