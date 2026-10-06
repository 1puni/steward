"""Slack delivery retains shared evidence and refuses ambiguous replay."""
from __future__ import annotations

import base64
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from steward_harness.conversations import ConversationService
from steward_harness.inbox import settle
from steward_harness.runtime.contracts import ResolvedModel, RuntimeResult
from steward_harness.slack import service as slack_module
from steward_harness.slack.service import (
    SlackAPIError, SlackContentRejected, SlackDeliveryUnknown, SlackRateLimited, SlackService,
)
from steward_harness.state import ConversationId, StateDatabase
from test_slack import config, event

ROUTE = "T123:C123:1234567890.000001"
OWNER = ConversationId("slack:" + ROUTE)
KEY = "result:test"


@pytest.fixture
def delivery(tmp_path):
    root = tmp_path.resolve()
    artifacts = root / "artifacts"
    artifacts.mkdir()
    state = StateDatabase(root / "state.db")
    cognition = Mock()

    def run(request, **kwargs):
        request = request() if callable(request) else request
        request.on_session_started("codex", "fixture-session")
        return RuntimeResult(output="Accepted reply", resolved=ResolvedModel("codex", "balanced", "fixture-model", "medium"),
                             effective_model="fixture-model", provider_session_id="fixture-session")

    cognition.run.side_effect = run
    conversations = ConversationService(state, cognition, provider_order=("codex",), profile="balanced",
                                        workspace=root, timeout_seconds=5)

    def turn(msg_id, route, author, text):
        return conversations.run_turn(transport="slack", transport_key=route, source_event_key=msg_id,
                                      operator_id=author, text=text).transport_reply

    api = Mock()

    def success(method, **kwargs):
        if method == "files_upload_v2":
            return dict(files=[dict(id="F123")])
        return dict(channel=kwargs["channel"], ts="1234567891.000001")

    api.call.side_effect = success
    cfg = config(delivery_roots=(str(artifacts),))

    def make():
        return SlackService(cfg, state=state, turn_handler=turn, command_handler=Mock(), api=api)

    def retain(text):
        state.save_result_receipt(dict(owner=str(OWNER), task_id=None, source_key=KEY,
                                       result_text=text, reply=text, done=False))

    return SimpleNamespace(root=root, artifacts=artifacts, state=state, conversations=conversations,
                           cognition=cognition, api=api, config=cfg, make=make, service=make(),
                           retain=retain, success=success)


def deliver_result(d):
    return d.conversations.deliver_task_result(OWNER, send=lambda text, key: d.service.send_result(ROUTE, text, key))


def test_result_completion_preserves_shared_per_piece_evidence(delivery):
    d = delivery
    d.retain("x" * 4000)
    assert deliver_result(d) == "x" * 4000
    receipt = d.state.result_receipt(KEY)
    assert receipt["done"] is True
    assert len(receipt["slack_sent"]) == 2
    assert all(part == dict(channel="C123", thread_ts="1234567890.000001", ts="1234567891.000001")
               for part in receipt["slack_sent"])
    d.service.send_result(ROUTE, "different text", KEY)
    assert d.api.call.call_count == 2
    d.cognition.run.assert_not_called()


def test_result_requires_existing_receipt_and_matching_owner(delivery):
    d = delivery
    with pytest.raises(ValueError, match="owner's shared result receipt"):
        d.service.send_result(ROUTE, "unretained", KEY)
    d.retain("retained")
    with pytest.raises(ValueError, match="owner's shared result receipt"):
        d.service.send_result("T123:C123:1234567890.000002", "retained", KEY)
    d.api.call.assert_not_called()


def test_rate_limit_restart_resumes_only_unsent_frozen_chunk(delivery, monkeypatch):
    d = delivery
    now = [100.0]
    monkeypatch.setattr(slack_module.time, "time", lambda: now[0])
    d.retain("a" * 3500 + "original remainder")
    d.api.call.side_effect = [dict(channel="C123", ts="1234567891.000001"), SlackRateLimited(30)]
    with pytest.raises(SlackRateLimited):
        deliver_result(d)
    receipt = d.state.result_receipt(KEY)
    assert len(receipt["slack_sent"]) == 1
    assert not receipt["done"]
    d.service = d.make()
    with pytest.raises(SlackAPIError, match="not due"):
        d.service.send_result(ROUTE, "changed text", KEY)
    assert d.api.call.call_count == 2
    now[0] += 31
    d.api.call.side_effect = d.success
    d.service.send_result(ROUTE, "changed text", KEY)
    assert d.api.call.call_args.kwargs["text"] == "original remainder"
    assert d.api.call.call_count == 3
    deliver_result(d)
    assert d.state.result_receipt(KEY)["done"] is True
    assert len(d.state.result_receipt(KEY)["slack_sent"]) == 2
    assert d.api.call.call_count == 3


def test_unknown_result_remains_pending_without_automatic_resend(delivery):
    d = delivery
    d.retain("must not duplicate")
    d.api.call.side_effect = SlackDeliveryUnknown("connection lost")
    with pytest.raises(SlackDeliveryUnknown):
        deliver_result(d)
    d.service = d.make()
    with pytest.raises(SlackDeliveryUnknown):
        deliver_result(d)
    receipt = d.state.result_receipt(KEY)
    assert not receipt["done"]
    assert receipt["slack_sending"] == 0
    assert receipt["delivery_error"]
    assert len(d.state.pending_result_receipts()) == 1
    d.api.call.assert_called_once()


@pytest.mark.parametrize("failure", ["lost_response", "crash_before_receipt_save"])
def test_ambiguous_inbound_send_never_repeats_accepted_turn_or_send(delivery, monkeypatch, failure):
    d = delivery

    class ProcessDied(BaseException):
        pass

    original_save = slack_module.write_receipt

    def crash_after_api(path, record):
        if record.get("slack_sent"):
            raise ProcessDied()
        original_save(path, record)

    if failure == "lost_response":
        d.api.call.side_effect = SlackDeliveryUnknown("lost response")
    else:
        monkeypatch.setattr(slack_module, "write_receipt", crash_after_api)
    d.service.ingest(event())
    claimed = d.service.inbox.claim(d.service.inbox.pending()[0])
    if failure == "lost_response":
        assert settle(d.service.source, claimed)
        assert not d.service.inbox.pending()
    else:
        with pytest.raises(ProcessDied):
            d.service.answer(claimed)
        monkeypatch.setattr(slack_module, "write_receipt", original_save)
    d.cognition.run.assert_called_once()
    assert d.state.turn_for_source(OWNER, "Ev1").state == "completed"
    d.service = d.make()
    d.service.inbox.recover()
    d.service.ingest(event())
    if failure == "crash_before_receipt_save":
        assert settle(d.service.source, d.service.inbox.claim(d.service.inbox.pending()[0]))
    assert not d.service.inbox.pending()
    [parked] = d.service.inbox.dir.glob("*.failed")
    receipt = json.loads(parked.read_text())
    assert receipt["reply"] == "Accepted reply"
    assert receipt["slack_sending"] == 0
    d.api.call.assert_called_once()
    d.cognition.run.assert_called_once()


def test_attachment_bytes_are_snapshotted_before_rate_limit_and_model_edit(delivery, monkeypatch):
    d = delivery
    now = [100.0]
    monkeypatch.setattr(slack_module.time, "time", lambda: now[0])
    artifact = d.artifacts / "report.txt"
    artifact.write_bytes(b"original evidence")
    d.retain(f"Report\n[[send_file:{artifact}]]")
    d.api.call.side_effect = [dict(channel="C123", ts="1234567891.000001"), SlackRateLimited(1)]
    with pytest.raises(SlackRateLimited):
        deliver_result(d)
    saved = d.state.result_receipt(KEY)
    assert base64.b64decode(saved["slack_parts"][1]["bytes"]) == b"original evidence"
    artifact.write_bytes(b"later model edit")
    now[0] += 2
    d.service = d.make()
    d.api.call.side_effect = d.success
    deliver_result(d)
    method = d.api.call.call_args.args[0]
    uploaded = d.api.call.call_args.kwargs
    assert method == "files_upload_v2"
    assert uploaded == dict(channel="C123", thread_ts="1234567890.000001", filename="report.txt", file=b"original evidence")
    assert d.state.result_receipt(KEY)["slack_sent"][1]["files"] == ["F123"]
    assert d.state.result_receipt(KEY)["done"] is True


@pytest.mark.parametrize("kind", ["outside", "file_symlink", "parent_symlink", "root_symlink", "fifo", "directory", "oversize", "aggregate_oversize"])
def test_unauthorized_or_unbounded_attachment_blocks_entire_reply_before_send(delivery, kind):
    d = delivery
    artifact = d.artifacts / "report.txt"
    artifact.write_bytes(b"evidence")
    markers = f"[[send_file:{artifact}]]"
    if kind == "outside":
        artifact = d.root / "secret"
        artifact.write_text("secret")
        markers = f"[[send_file:{artifact}]]"
    elif kind == "file_symlink":
        artifact.unlink()
        artifact.symlink_to(d.root / "private")
        (d.root / "private").write_text("private")
    elif kind in {"parent_symlink", "root_symlink"}:
        link = d.artifacts / "linked" if kind == "parent_symlink" else d.root / "linked-root"
        link.symlink_to(d.artifacts, target_is_directory=True)
        markers = f"[[send_file:{link / artifact.name}]]"
        if kind == "root_symlink":
            d.config.delivery_roots = (str(link),)
    elif kind == "fifo":
        artifact.unlink()
        os.mkfifo(artifact)
    elif kind == "directory":
        artifact.unlink()
        artifact.mkdir()
    elif kind == "oversize":
        d.config.max_file_bytes = 3
    elif kind == "aggregate_oversize":
        d.config.max_file_bytes = 10
        second = d.artifacts / "second.txt"
        second.write_bytes(b"evidence")
        markers += f"\n[[send_file:{second}]]"
    d.retain("Do not send even this prefix before validating files\n" + markers)
    with pytest.raises(SlackContentRejected):
        deliver_result(d)
    d.api.call.assert_not_called()
    assert not d.state.result_receipt(KEY)["done"]


@pytest.mark.parametrize('status,error,expected', [
    (429, 'ratelimited', 'SlackRateLimited'),
    (200, 'ratelimited', 'SlackRateLimited'),
    (500, 'internal_error', 'SlackDeliveryUnknown'),
    (200, 'fatal_error', 'SlackDeliveryUnknown'),
    (200, 'missing_scope', 'SlackAPIError'),
    (200, 'token_expired', 'SlackAPIError'),
    (200, 'msg_too_long', 'SlackContentRejected'),
])
def test_sdk_refusals_classify_delivery_without_leaking_payload(status, error, expected):
    from types import SimpleNamespace
    from steward_harness.slack import service as slack
    pytest.importorskip('slack_sdk')
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.slack_response import SlackResponse

    response = SlackResponse(client=None, http_verb='POST', api_url='https://slack.com/api/chat.postMessage',
                             req_args={}, data={'ok': False, 'error': error},
                             headers={'Retry-After': '30'}, status_code=status)

    def fail(**kwargs):
        raise SlackApiError('fake-token-and-private-request-body', response)

    api = slack.SlackAPI.__new__(slack.SlackAPI)
    api.client = SimpleNamespace(chat_postMessage=fail)
    with pytest.raises(getattr(slack, expected)) as raised:
        api.call('chat_postMessage', text='private-content')
    assert 'fake-token' not in str(raised.value)
    assert 'private-content' not in str(raised.value)
    if expected == 'SlackRateLimited':
        assert raised.value.seconds == 30


def test_start_refuses_bot_token_for_another_workspace(tmp_path):
    from types import SimpleNamespace
    from steward_harness.config.schema import SlackConfig
    from steward_harness.slack.service import SlackService
    pytest.importorskip('slack_sdk')
    config = SlackConfig(team_id='T123', channel_id='C123',
                         bot_token_path='/controller/bot', app_token_path='/controller/app',
                         users={'U123': 'operator'})
    service = SlackService(config, state=SimpleNamespace(path=tmp_path / 'state.db'),
                           turn_handler=None, command_handler=None,
                           api=SimpleNamespace(call=lambda method: {'team_id': 'T999', 'bot_id': 'B123'}))
    with pytest.raises(ValueError, match='configured workspace'):
        service.start()
    assert service.socket is None
