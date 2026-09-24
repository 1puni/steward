"""Telegram command registration: API payload shape and service startup wiring."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from steward_harness.config.schema import TelegramConfig, UntrustedExecutionConfig
from steward_harness.runtime.contracts import RuntimeExecutionError
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import ConversationBusy, StateDatabase
from steward_harness.telegram import api as api_module
from steward_harness.telegram import service as service_module
from steward_harness.telegram.api import TelegramAPI, TelegramAPIError
from steward_harness.telegram.service import (
    TelegramDeliveryError,
    TelegramService,
    probe_chat_access,
)
from steward_harness.lease import Busy


def _broker() -> UntrustedExecutionBroker:
    return UntrustedExecutionBroker(config=UntrustedExecutionConfig())


def test_task_result_replays_only_unconfirmed_pieces_after_restart(tmp_path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "format_markdown_chunks", lambda _text: ["first", "second"])
    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 0)
    attempts = []
    def send(_chat, text, **_kwargs):
        attempts.append(text)
        if text == "second":
            raise TelegramAPIError("temporary outage")
        return 101
    monkeypatch.setattr(service.api, "send_message", send)
    with pytest.raises(TelegramAPIError):
        service.send_result(1, 42, "stable task outcome", "task_result:test:revision")
    assert attempts.count("first") == 1
    restarted = _service(tmp_path)
    sent = []
    monkeypatch.setattr(restarted.api, "send_message", lambda _chat, text, **kw: sent.append(text) or 102)
    restarted.send_result(1, 42, "stable task outcome", "task_result:test:revision")
    restarted.send_result(1, 42, "stable task outcome", "task_result:test:revision")
    assert sent == ["second"]


def _drain(service: "TelegramService", timeout: float = 10.0) -> None:
    """Run one executor pass to completion, as the worker pool does in production.

    Only usable where the test arranges for request_stop() to be reached; the
    loop keeps draining while anything is pending.
    """
    worker = threading.Thread(target=service._executor_loop, daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    assert not worker.is_alive(), "executor loop did not finish"


class _FakeResponse:
    def __init__(self, json_body: dict[str, Any]) -> None:
        self.status_code = 200
        self._json = json_body
        self.text = str(json_body)

    def json(self) -> dict[str, Any]:
        return self._json


class _FakeClient:
    def __init__(self, calls: list[tuple[str, str, dict[str, Any]]]) -> None:
        self._calls = calls

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self._calls.append((method, url, kwargs))
        return _FakeResponse({"ok": True, "result": True})


def test_set_my_commands_sends_expected_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(api_module.httpx, "Client", lambda timeout=None: _FakeClient(calls))

    client = TelegramAPI("tok")
    client.set_my_commands([{"command": "status", "description": "Show status"}])

    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url.endswith("/setMyCommands")
    assert kwargs["json"] == {"commands": [{"command": "status", "description": "Show status"}]}


def test_get_chat_administrators_rejects_a_result_that_is_not_a_member_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TelegramAPI("tok")
    monkeypatch.setattr(client, "_request", lambda *_a, **_kw: {"user": {"id": 7}})

    with pytest.raises(TelegramAPIError, match="invalid result"):
        client.get_chat_administrators(1)


def test_send_message_passes_explicit_html_parse_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    client = TelegramAPI("tok")
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, endpoint, **kwargs: calls.append((endpoint, kwargs))
        or {"message_id": 1},
    )

    client.send_message(1, "<b>done</b>", topic_id=9, parse_mode="HTML")

    endpoint, kwargs = calls[0]
    assert endpoint == "sendMessage"
    assert kwargs["json"] == {
        "chat_id": 1,
        "text": "<b>done</b>",
        "message_thread_id": 9,
        "parse_mode": "HTML",
    }


def test_polling_admits_only_supported_message_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(api_module.httpx, "Client", lambda timeout=None: _FakeClient(calls))

    TelegramAPI("tok").get_updates(offset=19, timeout_seconds=15)

    _method, _url, kwargs = calls[0]
    assert kwargs["json"] == {
        "offset": 19,
        "timeout": 15,
        "allowed_updates": ["message"],
    }


def test_pin_chat_message_sends_fixed_numeric_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(api_module.httpx, "Client", lambda timeout=None: _FakeClient(calls))

    TelegramAPI("tok").pin_chat_message(-100123, 77)

    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url.endswith("/pinChatMessage")
    assert kwargs["json"] == {
        "chat_id": -100123,
        "message_id": 77,
        "disable_notification": True,
    }


def test_send_document_streams_generic_file_with_topic(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []
    client_type = httpx.Client

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 73}})

    monkeypatch.setattr(
        api_module.httpx, "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    artifact = tmp_path / "app.apk"
    artifact.write_bytes(b"apk")

    message_id = TelegramAPI("tok", base_url="http://bot-api:8081").send_document(
        -100123, artifact, caption="signed", topic_id=8
    )

    assert message_id == 73
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "http://bot-api:8081/bottok/sendDocument"
    assert request.method == "POST"
    assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 600.0)
    assert b'name="chat_id"\r\n\r\n-100123' in request.content
    assert b'name="message_thread_id"\r\n\r\n8' in request.content
    assert b'name="caption"\r\n\r\nsigned' in request.content
    assert b'name="document"; filename="app.apk"' in request.content
    assert b'Content-Type: application/vnd.android.package-archive\r\n\r\napk' in request.content


def test_download_file_uses_botapi_file_route_and_atomic_content(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[httpx.Request] = []
    client_type = httpx.Client

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/x.jpg"}})
        return httpx.Response(200, content=b"picture")

    monkeypatch.setattr(
        api_module.httpx, "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    client = TelegramAPI("tok", base_url="http://bot-api:8081")
    destination = tmp_path / "inbound" / "photo.image"

    size, digest = client.download_file("file-1", destination, max_bytes=20)

    assert [str(request.url) for request in calls] == [
        "http://bot-api:8081/bottok/getFile", "http://bot-api:8081/file/bottok/photos/x.jpg",
    ]
    assert calls[1].method == "GET"
    assert calls[1].extensions["timeout"]["read"] == 60.0
    assert size == 7
    assert digest == hashlib.sha256(b"picture").hexdigest()
    assert destination.read_bytes() == b"picture"
    assert list(destination.parent.glob("*.partial")) == []


def test_download_file_removes_partial_when_actual_content_exceeds_cap(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_type = httpx.Client
    monkeypatch.setattr(
        api_module.httpx, "Client",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"123456")),
            **kwargs,
        ),
    )
    client = TelegramAPI("tok")
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: {"file_path": "x.jpg"})
    destination = tmp_path / "inbound" / "photo.image"

    with pytest.raises(TelegramAPIError, match="exceeds configured"):
        client.download_file("file-1", destination, max_bytes=5)

    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_upload_retries_real_transport_failures_but_preserves_controller_faults(
    tmp_path, monkeypatch,
) -> None:
    artifact = tmp_path / "report.txt"
    artifact.write_bytes(b"complete report")
    requests = []
    fault = ValueError("broken request middleware")
    broken = False
    client_type = httpx.Client

    def respond(request):
        requests.append(request)
        if broken:
            raise fault
        if len(requests) < 3:
            raise httpx.ConnectError("connection unavailable", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 73}})

    monkeypatch.setattr(
        api_module.httpx, "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 0)
    client = TelegramAPI("tok")

    assert TelegramService._send_with_retry(lambda: client.send_document(1, artifact)) == 73
    assert len(requests) == 3
    assert all(b"complete report" in request.content for request in requests)

    broken = True
    requests.clear()
    with pytest.raises(ValueError) as caught:
        TelegramService._send_with_retry(lambda: client.send_document(1, artifact))
    assert caught.value is fault
    assert len(requests) == 1


def test_upload_uses_shared_api_response_errors(tmp_path, monkeypatch) -> None:
    artifact = tmp_path / "report.txt"
    artifact.write_text("report")
    responses = iter((
        httpx.Response(503, text="service unavailable"),
        httpx.Response(200, content=b"not JSON"),
        httpx.Response(200, json={"ok": False, "description": "document rejected"}),
    ))
    client_type = httpx.Client
    monkeypatch.setattr(
        api_module.httpx, "Client",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(lambda request: next(responses)), **kwargs,
        ),
    )
    client = TelegramAPI("tok")

    with pytest.raises(TelegramAPIError, match="HTTP 503"):
        client.send_document(1, artifact)
    with pytest.raises(TelegramAPIError, match="Invalid JSON"):
        client.send_document(1, artifact)
    with pytest.raises(TelegramAPIError, match="document rejected"):
        client.send_document(1, artifact)


@pytest.mark.parametrize("error", [httpx.ReadError("stream lost"), ValueError("broken stream")])
def test_download_closes_stream_and_removes_partial_after_failure(
    tmp_path, monkeypatch, error,
) -> None:
    class FailedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial image"
            raise error

    response = httpx.Response(200, stream=FailedStream())
    client_type = httpx.Client
    monkeypatch.setattr(
        api_module.httpx, "Client",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(lambda request: response), **kwargs,
        ),
    )
    client = TelegramAPI("tok")
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: {"file_path": "x.jpg"})
    destination = tmp_path / "inbound" / "photo.image"

    with pytest.raises(TelegramAPIError if isinstance(error, httpx.RequestError) else ValueError) as caught:
        client.download_file("file-1", destination, max_bytes=100)
    if isinstance(error, httpx.RequestError):
        assert caught.value.__cause__ is error
    else:
        assert caught.value is error
    assert response.is_closed
    assert list(destination.parent.iterdir()) == []


def test_download_persistence_fault_escapes_with_partial_removed(tmp_path, monkeypatch) -> None:
    client_type = httpx.Client
    monkeypatch.setattr(
        api_module.httpx, "Client",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"picture")),
            **kwargs,
        ),
    )
    failure = OSError("storage unavailable")

    def fail_fsync(_descriptor):
        raise failure

    monkeypatch.setattr(api_module.os, "fsync", fail_fsync)
    client = TelegramAPI("tok")
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: {"file_path": "x.jpg"})
    destination = tmp_path / "inbound" / "photo.image"

    with pytest.raises(OSError) as caught:
        client.download_file("file-1", destination, max_bytes=100)
    assert caught.value is failure
    assert list(destination.parent.iterdir()) == []


def test_register_commands_derives_builtin_and_adapter_descriptions(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path),
            chat_id=1,
            allowed_users=(2,),
            adapter_commands={
                "build": {
                    "description": "Build the product",
                    "command": {"argv": ["/bin/true"]},
                }
            },
        ),
        turn_handler=lambda *a: "",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )
    calls: list[Any] = []
    monkeypatch.setattr(service.api, "set_my_commands", lambda cmds: calls.append(cmds))

    service._register_commands()

    assert len(calls) == 1
    assert any(item["command"] == "status" for item in calls[0])
    assert {"command": "build", "description": "Build the product"} in calls[0]


def test_register_commands_failure_is_swallowed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path), chat_id=1, allowed_users=(2,)
        ),
        turn_handler=lambda *a: "",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )

    def _raise(cmds: Any) -> None:
        raise TelegramAPIError("boom")

    monkeypatch.setattr(service.api, "set_my_commands", _raise)

    service._register_commands()  # must not raise


# ---------------------------------------------------------------------------
# probe_chat_access — fixed-chat capability startup probe
# ---------------------------------------------------------------------------


def _config(**overrides: Any) -> TelegramConfig:
    overrides.setdefault("allowed_users", (1,))
    return TelegramConfig(token_path="/dev/null", chat_id=-100123, **overrides)


def test_probe_passes_for_plain_chat_without_topics(monkeypatch: pytest.MonkeyPatch) -> None:
    api = TelegramAPI("tok")
    monkeypatch.setattr(api, "get_me", lambda: {"id": 42})
    monkeypatch.setattr(api, "get_chat", lambda chat_id: {"id": chat_id, "type": "group"})

    probe_chat_access(api, _config())  # no topics configured -> reachability only


def test_probe_rejects_unreachable_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    api = TelegramAPI("tok")
    monkeypatch.setattr(api, "get_me", lambda: {"id": 42})
    monkeypatch.setattr(api, "get_chat", lambda chat_id: {"id": chat_id + 1, "type": "group"})

    with pytest.raises(TelegramAPIError):
        probe_chat_access(api, _config())


def test_probe_requires_forum_when_topics_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    api = TelegramAPI("tok")
    monkeypatch.setattr(api, "get_me", lambda: {"id": 42})
    monkeypatch.setattr(api, "get_chat", lambda chat_id: {"id": chat_id, "type": "group"})

    with pytest.raises(TelegramAPIError):
        probe_chat_access(api, _config(topics={"steward": 2}))


def test_probe_accepts_forum_without_topic_management_rights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = TelegramAPI("tok")
    monkeypatch.setattr(api, "get_me", lambda: {"id": 42})
    monkeypatch.setattr(
        api, "get_chat", lambda chat_id: {"id": chat_id, "type": "supergroup", "is_forum": True}
    )
    monkeypatch.setattr(
        api,
        "get_chat_member",
        lambda *_args: pytest.fail("ordinary topic messaging needs no admin probe"),
    )

    probe_chat_access(api, _config(topics={"steward": 2}))


def test_probe_requires_pin_permission_for_agent_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = TelegramAPI("tok")
    monkeypatch.setattr(api, "get_me", lambda: {"id": 42})
    monkeypatch.setattr(
        api, "get_chat", lambda chat_id: {"id": chat_id, "type": "group"}
    )
    monkeypatch.setattr(
        api,
        "get_chat_member",
        lambda chat_id, user_id: {
            "user": {"id": user_id},
            "status": "administrator",
            "can_pin_messages": False,
        },
    )

    with pytest.raises(TelegramAPIError, match="pin permission"):
        probe_chat_access(api, _config(agent_actions=("pin_reply",)))


def test_probe_proves_the_administrator_listing_it_will_depend_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = TelegramAPI("tok")
    monkeypatch.setattr(api, "get_me", lambda: {"id": 42})
    monkeypatch.setattr(api, "get_chat", lambda chat_id: {"id": chat_id, "type": "supergroup"})
    listed: list[int] = []
    monkeypatch.setattr(
        api, "get_chat_administrators", lambda chat_id: listed.append(chat_id) or ()
    )

    probe_chat_access(api, _config(allow_group_administrators=True))
    assert listed == [-100123]

    # The bot is an ordinary member here, which Telegram permits for this call.
    # Only the call failing may refuse startup.
    probe_chat_access(api, _config())
    assert listed == [-100123]


def test_probe_refuses_when_administrators_cannot_be_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = TelegramAPI("tok")
    monkeypatch.setattr(api, "get_me", lambda: {"id": 42})
    monkeypatch.setattr(api, "get_chat", lambda chat_id: {"id": chat_id, "type": "supergroup"})

    def _refuse(_chat_id: int) -> tuple[dict[str, Any], ...]:
        raise TelegramAPIError("Bad Request: member list is inaccessible")

    monkeypatch.setattr(api, "get_chat_administrators", _refuse)

    with pytest.raises(TelegramAPIError, match="member list is inaccessible"):
        probe_chat_access(api, _config(allow_group_administrators=True))


def test_group_administrator_is_admitted_without_being_an_allowed_user(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, allowed_users=(2,), allow_group_administrators=True)
    monkeypatch.setattr(
        service.api,
        "get_chat_administrators",
        lambda _chat_id: ({"status": "administrator", "user": {"id": 7}},),
    )

    service._ingest_update(
        {"update_id": 1, "message": {"chat": {"id": 1}, "from": {"id": 7}, "text": "status"}}
    )

    assert service._has_pending()


def test_a_plain_member_stays_out_when_administrators_are_admitted(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, allowed_users=(2,), allow_group_administrators=True)
    monkeypatch.setattr(
        service.api,
        "get_chat_administrators",
        lambda _chat_id: ({"status": "administrator", "user": {"id": 7}},),
    )

    service._ingest_update(
        {"update_id": 1, "message": {"chat": {"id": 1}, "from": {"id": 8}, "text": "status"}}
    )

    assert not service._has_pending()


def test_administrators_are_not_re_read_for_every_message(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, allowed_users=(2,), allow_group_administrators=True)
    reads = 0

    def _list(_chat_id: int) -> tuple[dict[str, Any], ...]:
        nonlocal reads
        reads += 1
        return ({"status": "creator", "user": {"id": 7}},)

    monkeypatch.setattr(service.api, "get_chat_administrators", _list)

    assert service._is_group_administrator(7)
    assert service._is_group_administrator(7)
    assert not service._is_group_administrator(8)
    assert reads == 1

    # A demotion is visible once the cached listing ages out, not before.
    monkeypatch.setattr(service_module, "_ADMINISTRATOR_TTL_SECONDS", 0.0)
    monkeypatch.setattr(service.api, "get_chat_administrators", lambda _chat_id: ())
    assert not service._is_group_administrator(7)


def test_an_unreachable_telegram_neither_admits_nor_sticks(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, allowed_users=(2,), allow_group_administrators=True)

    def _fail(_chat_id: int) -> tuple[dict[str, Any], ...]:
        raise TelegramAPIError("connection reset")

    monkeypatch.setattr(service.api, "get_chat_administrators", _fail)
    assert not service._is_group_administrator(7)

    # A configured operator is unaffected by the outage, and the failed read was
    # not cached as an empty administrator list.
    service._ingest_update(
        {"update_id": 1, "message": {"chat": {"id": 1}, "from": {"id": 2}, "text": "status"}}
    )
    assert service._has_pending()
    monkeypatch.setattr(
        service.api,
        "get_chat_administrators",
        lambda _chat_id: ({"status": "administrator", "user": {"id": 7}},),
    )
    assert service._is_group_administrator(7)


def test_the_bots_own_administrator_entry_is_not_an_operator(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram drops other bots from this listing but returns the caller's own.

    Shape taken from a live chat, which answers with the steward's own
    bot account as an administrator beside the two humans.
    """
    service = _service(tmp_path, allowed_users=(2,), allow_group_administrators=True)
    monkeypatch.setattr(
        service.api,
        "get_chat_administrators",
        lambda _chat_id: (
            {"status": "administrator", "user": {"id": 7, "is_bot": False}},
            {"status": "administrator", "user": {"id": 8664556359, "is_bot": True}},
            {"status": "creator", "user": {"id": 2, "is_bot": False}},
        ),
    )

    assert service._is_group_administrator(7)
    assert not service._is_group_administrator(8664556359)


def test_administrators_are_ignored_unless_the_flag_is_set(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, allowed_users=(2,))

    def _unexpected(_chat_id: int) -> tuple[dict[str, Any], ...]:
        raise AssertionError("administrators must not be read when the flag is off")

    monkeypatch.setattr(service.api, "get_chat_administrators", _unexpected)

    service._ingest_update(
        {"update_id": 1, "message": {"chat": {"id": 1}, "from": {"id": 7}, "text": "status"}}
    )

    assert not service._has_pending()


def test_start_probes_before_registering_commands(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path), chat_id=-100123, allowed_users=(2,)
        ),
        turn_handler=lambda *a: "",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )
    service.request_stop()
    order: list[str] = []
    monkeypatch.setattr(
        service_module,
        "probe_chat_access",
        lambda api, config: order.append("probe"),
    )
    monkeypatch.setattr(
        service.api, "set_my_commands", lambda cmds: order.append("register")
    )

    service.start()
    service.stop()

    assert order == ["probe", "register"]


def test_start_does_not_admit_work_when_probe_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path), chat_id=-100123, allowed_users=(2,)
        ),
        turn_handler=lambda *a: "",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )

    def _fail_probe(api: Any, config: Any) -> None:
        raise TelegramAPIError("bot is not an administrator")

    monkeypatch.setattr(service_module, "probe_chat_access", _fail_probe)
    monkeypatch.setattr(
        service.api,
        "get_updates",
        lambda *a, **k: pytest.fail("polling must not start when the probe fails"),
    )

    with pytest.raises(TelegramAPIError):
        service.start()


def test_slow_delivery_outbox_does_not_block_update_polling(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outbox = tmp_path / "outbox"
    quarantine = tmp_path / "quarantine"
    service = _service(
        tmp_path,
        delivery_outbox_dir=str(outbox),
        delivery_quarantine_dir=str(quarantine),
    )
    drain_started = threading.Event()
    release_drain = threading.Event()
    polled = threading.Event()

    def slow_drain() -> None:
        drain_started.set()
        release_drain.wait(5)

    def poll(*_args, **_kwargs):
        assert drain_started.wait(2)
        polled.set()
        service.request_stop()
        return []

    monkeypatch.setattr(service_module, "probe_chat_access", lambda *_args, **_kw: None)
    monkeypatch.setattr(service, "_register_commands", lambda: None)
    monkeypatch.setattr(service, "_drain_delivery_outbox", slow_drain)
    monkeypatch.setattr(service.api, "get_updates", poll)

    service.start()
    try:
        assert drain_started.wait(2)
        assert polled.wait(2)
    finally:
        release_drain.set()
        service.stop()



@pytest.mark.parametrize("command", [
    "cancel", "status", "tasks", "pause", "resume", "task show t_123",
    "model deep", "model-family codex", "clear",
])
def test_controller_command_overtakes_an_already_active_turn(
    tmp_path, monkeypatch: pytest.MonkeyPatch, command
) -> None:
    active = threading.Event()
    cancelled = threading.Event()
    polls = 0
    commands = []

    def turn_handler(*_args: Any) -> str:
        active.set()
        assert cancelled.wait(2)
        return "interrupted"

    def command_handler(name: str, *_args: Any) -> str:
        commands.append(name)
        assert name == command.split()[0].replace("-", "_")
        assert active.is_set()
        cancelled.set()
        return "Cancellation requested"

    (tmp_path / "token").write_text("tok")
    service = TelegramService(
        config=TelegramConfig(
            token_path=str(tmp_path / "token"), chat_id=1, allowed_users=(2,)
        ),
        turn_handler=turn_handler,
        command_handler=command_handler,
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )
    normal = {
        "update_id": 10,
        "message": {"chat": {"id": 1}, "from": {"id": 2}, "text": "work"},
    }
    cancel = {
        "update_id": 11,
        "message": {"chat": {"id": 1}, "from": {"id": 2}, "text": f"/{command}"},
    }
    unauthorized = {
        "update_id": 12,
        "message": {"chat": {"id": 1}, "from": {"id": 999}, "text": f"/{command}"},
    }

    def poll(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal polls
        polls += 1
        if polls == 1:
            return [normal]
        if polls == 2:
            assert active.wait(2)
            return [cancel, unauthorized]
        service.request_stop()
        return []

    monkeypatch.setattr(service_module, "probe_chat_access", lambda *_args, **_kw: None)
    monkeypatch.setattr(service, "_register_commands", lambda: None)
    monkeypatch.setattr(service, "send_reply", lambda *_args, **_kw: None)
    monkeypatch.setattr(service.api, "get_updates", poll)

    # Typing narration is unrelated to cancellation ordering and must not put
    # a real network request inside this bounded concurrency regression.
    monkeypatch.setattr(service.api, "send_chat_action", lambda *args, **kwargs: None)

    service.start()
    try:
        assert service._stop.wait(3)
    finally:
        service.stop()

    assert cancelled.is_set()
    assert commands == [command.split()[0].replace("-", "_")]
    # Every ingested update was consumed: the control command overtook the busy
    # topic inline and the unauthorized one was dropped at the trust boundary.
    assert not service._has_pending()


@pytest.mark.parametrize("text", ["/git reconcile app", "/rhythm run sleep", "/build", "work"])
def test_external_work_does_not_enter_the_polling_control_path(text):
    assert not TelegramService._is_control_command({"message": {"text": text}})


def test_bare_slash_gets_a_reply_without_blocking_its_topic(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("tok")
    state = StateDatabase(tmp_path / "state.db")
    received = []

    def respond(_event, _chat, _topic, _user, text, _images):
        received.append(text)
        service.request_stop()
        return "Hello back"

    service = TelegramService(
        TelegramConfig(token_path=str(tmp_path / "token"), chat_id=1, allowed_users=(2,)),
        respond, command_handler=lambda *args: pytest.fail("not a command"),
        state_db=state, execution_broker=_broker(),
    )
    replies = []
    monkeypatch.setattr(service, "send_reply", lambda _chat, _topic, text, **_kw: replies.append(text))
    monkeypatch.setattr(service.api, "send_chat_action", lambda *a, **kw: None)
    for update_id, text in [(1, "/"), (2, "Hello")]:
        service._ingest_update({
            "update_id": update_id,
            "message": {"chat": {"id": 1}, "from": {"id": 2}, "text": text},
        })
    worker = threading.Thread(target=service._executor_loop, daemon=True)
    worker.start()
    try:
        assert service._stop.wait(3)
    finally:
        service.stop()
        worker.join(timeout=3)
    assert received == ["Hello"]
    assert replies == ["Use Telegram's command menu.", "Hello back"]
    assert not service._has_pending()


def test_unknown_slash_command_never_reaches_model(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    turns: list[str] = []
    replies: list[str] = []
    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path), chat_id=1, allowed_users=(2,)
        ),
        turn_handler=lambda *args: turns.append(str(args[-1])) or "model reply",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )
    monkeypatch.setattr(
        service,
        "send_reply",
        lambda chat_id, topic_id, text, **_kw: replies.append(text),
    )

    service._handle_update(
        {
            "update_id": 12,
            "message": {
                "chat": {"id": 1},
                "from": {"id": 2},
                "text": "/buidl",
            },
        }
    )

    assert turns == []
    assert replies == ["Unknown command /buidl. Use Telegram's command menu."]


@pytest.mark.parametrize("busy", [Busy, ConversationBusy])
def test_busy_owner_returns_persisted_turn_to_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, busy: type[RuntimeError]
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    state = StateDatabase(tmp_path / "state.db")
    service: TelegramService

    def busy_turn(*_args: Any) -> str:
        service.request_stop()
        raise busy("Owner is busy")

    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path), chat_id=1, allowed_users=(2,)
        ),
        turn_handler=busy_turn,
        command_handler=lambda *args: "",
        state_db=state,
        execution_broker=_broker(),
    )
    monkeypatch.setattr(service.api, "send_chat_action", lambda *args, **kwargs: None)

    update = {
        "update_id": 16,
        "message": {
            "chat": {"id": 1},
            "from": {"id": 2},
            "text": "wait for the world",
        },
    }
    service._ingest_update(update)

    worker = threading.Thread(target=service._executor_loop, daemon=True)
    worker.start()
    worker.join(timeout=5)
    # A busy owner must never hold the executor open: stop() joins these.
    assert not worker.is_alive()

    # The turn stays at the front of its topic, and its receipt retains it for
    # the next start.
    assert list(service._pending[(1, 0)]) == [update]
    receipt = service._read_receipt(service._receipt_path(update))
    assert receipt["update"] == update and not receipt.get("done")


def test_worker_pool_runs_different_topics_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    state = StateDatabase(tmp_path / "state.db")
    both_entered = threading.Barrier(2, timeout=5)
    completed: list[int] = []
    service: TelegramService

    def turn_handler(_event: str, _chat: int, topic: int, *_rest: Any) -> str:
        # Both workers must reach this point before either may proceed: if
        # the pool were still serialized, the second call would never start
        # while the first is blocked here, and the barrier would time out.
        both_entered.wait()
        completed.append(topic)
        if len(completed) >= 2:
            service.request_stop()
        return "reply"

    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path), chat_id=1, allowed_users=(2,)
        ),
        turn_handler=turn_handler,
        command_handler=lambda *args: "",
        state_db=state,
        execution_broker=_broker(),
    )
    monkeypatch.setattr(service.api, "send_chat_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "send_reply", lambda *args, **kwargs: None)

    for update_id, topic in ((21, 10), (22, 20)):
        service._ingest_update(
            {
                "update_id": update_id,
                "message": {
                    "chat": {"id": 1},
                    "from": {"id": 2},
                    "message_thread_id": topic,
                    "text": f"topic {topic}",
                },
            }
        )

    workers = [
        threading.Thread(target=service._executor_loop, daemon=True)
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert not any(worker.is_alive() for worker in workers)
    assert sorted(completed) == [10, 20]
    assert not service._has_pending()


def test_foreign_chat_is_rejected_even_when_all_configured_chat_members_are_allowed(
    tmp_path,
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    turns: list[tuple[Any, ...]] = []
    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path), chat_id=1, allowed_users=(7,)
        ),
        turn_handler=lambda *args: turns.append(args) or "reply",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )

    service._ingest_update(
        {
            "update_id": 13,
            "message": {
                "chat": {"id": 2},
                "from": {"id": 7},
                "text": "run privileged work",
            },
        }
    )

    assert turns == []
    assert not service._has_pending()


def test_user_outside_configured_allowlist_is_rejected(tmp_path) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    turns: list[tuple[Any, ...]] = []
    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path),
            chat_id=1,
            allowed_users=(7,),
        ),
        turn_handler=lambda *args: turns.append(args) or "reply",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )

    service._ingest_update(
        {
            "update_id": 14,
            "message": {
                "chat": {"id": 1},
                "from": {"id": 8},
                "text": "run privileged work",
            },
        }
    )

    assert turns == []
    assert not service._has_pending()


def test_empty_allowlist_cannot_fail_open_when_validation_is_bypassed(tmp_path) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    turns: list[tuple[Any, ...]] = []
    invalid_config = TelegramConfig.model_construct(
        token_path=str(token_path),
        chat_id=1,
        allowed_users=(),
    )
    service = TelegramService(
        config=invalid_config,
        turn_handler=lambda *args: turns.append(args) or "reply",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )

    service._ingest_update(
        {
            "update_id": 15,
            "message": {
                "chat": {"id": 1},
                "from": {"id": 7},
                "text": "run privileged work",
            },
        }
    )

    assert turns == []
    assert not service._has_pending()


def test_photo_without_caption_is_downloaded_and_passed_to_turn(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    turns: list[tuple[Any, ...]] = []
    shared: list[Path] = []

    class AccessBroker:
        def grant_read_access(self, path: Path) -> None:
            shared.append(path)

    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path),
            chat_id=1,
            allowed_users=(2,),
            media_max_mb=1,
        ),
        turn_handler=lambda *args: turns.append(args) or "seen",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=AccessBroker(),  # type: ignore[arg-type]
    )
    downloads: list[str] = []

    def _download(file_id: str, destination: Any, *, max_bytes: int) -> tuple[int, str]:
        downloads.append(file_id)
        destination.write_bytes(b"largest photo")
        return len(b"largest photo"), hashlib.sha256(b"largest photo").hexdigest()

    monkeypatch.setattr(service.api, "download_file", _download)
    monkeypatch.setattr(service.api, "send_chat_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "send_reply", lambda *args, **_kw: None)

    update = {
        "update_id": 61,
        "message": {
            "message_id": 12,
            "chat": {"id": 1},
            "from": {"id": 2},
            "photo": [
                {"file_id": "small", "file_size": 3},
                {"file_id": "large", "file_size": 13},
            ],
        },
    }
    service._handle_update(update)
    service._handle_update(update)  # a delivered update is silent on replay

    assert downloads == ["large"]
    assert [turn[4] for turn in turns] == [
        "Please inspect the attached image.",
    ]
    image = turns[0][5][0]
    assert image.is_absolute()
    assert image.read_bytes() == b"largest photo"
    assert shared == [tmp_path / "telegram-input", image]


def test_image_document_caption_is_preserved(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    turns: list[tuple[Any, ...]] = []
    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path), chat_id=1, allowed_users=(2,)
        ),
        turn_handler=lambda *args: turns.append(args) or "seen",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )

    def _download(file_id: str, destination: Any, *, max_bytes: int) -> tuple[int, str]:
        assert file_id == "image-doc"
        destination.write_bytes(b"png")
        return 3, hashlib.sha256(b"png").hexdigest()

    monkeypatch.setattr(service.api, "download_file", _download)
    monkeypatch.setattr(service.api, "send_chat_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "send_reply", lambda *args, **_kw: None)

    service._handle_update(
        {
            "update_id": 62,
            "message": {
                "message_id": 13,
                "chat": {"id": 1},
                "from": {"id": 2},
                "caption": "What is wrong here?",
                "document": {
                    "file_id": "image-doc",
                    "file_size": 3,
                    "mime_type": "image/png",
                },
            },
        }
    )

    assert turns[0][4] == "What is wrong here?"
    assert turns[0][5][0].read_bytes() == b"png"


def test_a_spooled_image_is_reused_rather_than_downloaded_again(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existence is the record, because the rename is the commit.

    This used to be `..._recovers_crash_between_file_and_sidecar` and asserted
    that the missing sidecar was rebuilt. There is no sidecar; a file in the
    spool is a file `os.replace` finished putting there, and the only thing
    worth asserting is that it is not fetched twice.
    """
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path), chat_id=1, allowed_users=(2,)
        ),
        turn_handler=lambda *args: "seen",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )
    spool = tmp_path / "telegram-input"
    monkeypatch.setattr(service.api, "send_chat_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "send_reply", lambda *args, **_kw: None)

    def download(_file_id, destination, **_kwargs):
        Path(destination).write_bytes(b"complete atomic download")
        return len(b"complete atomic download"), "unread-digest"

    monkeypatch.setattr(service.api, "download_file", download)
    update = {
        "update_id": 65,
        "message": {
            "message_id": 16,
            "chat": {"id": 1},
            "from": {"id": 2},
            "photo": [{"file_id": "recover", "file_size": 24}],
        },
    }
    service._handle_update(update)

    # One file, and only one: the spool entry is not a two-file transaction.
    assert [path.suffix for path in sorted(spool.iterdir())] == [".image"]

    monkeypatch.setattr(
        service.api,
        "download_file",
        lambda *args, **kwargs: pytest.fail("a spooled image must not be fetched twice"),
    )
    service._handle_update(update)
    assert [path.suffix for path in sorted(spool.iterdir())] == [".image"]


def test_an_oversized_image_is_refused_without_wedging_its_topic(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is not downloaded, the sender is told, and the queue empties.

    The last clause is the one that was broken. A cap violation used to raise a
    bare `TelegramAPIError`, which the executor classifies as transient and
    returns to the front of its topic, retrying every second. The poll offset
    only advances past a handled update, so one oversized photo stopped that
    topic — and every newer update in every other topic — from ever being seen
    again. `InboundMediaRejected` is the same failure said precisely enough to
    be distinguishable from the network being the network.
    """
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    replies: list[str] = []
    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path),
            chat_id=1,
            allowed_users=(2,),
            media_max_mb=1,
        ),
        turn_handler=lambda *args: "seen",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )
    monkeypatch.setattr(
        service.api,
        "download_file",
        lambda *args, **kwargs: pytest.fail("oversized media must not be downloaded"),
    )
    monkeypatch.setattr(service.api, "send_chat_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        service, "send_reply", lambda _chat, _topic, text, **_kw: replies.append(text)
    )

    service._ingest_update(
        {
            "update_id": 63,
            "message": {
                "message_id": 14,
                "chat": {"id": 1},
                "from": {"id": 2},
                "photo": [{"file_id": "huge", "file_size": 1024 * 1024 + 1}],
            },
        }
    )
    service._stop.set()
    _drain(service)

    assert replies == ["Telegram image exceeds configured 1MB cap"]
    assert not service._has_pending()


def test_non_image_document_does_not_become_a_turn(tmp_path) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    turns: list[tuple[Any, ...]] = []
    service = TelegramService(
        config=TelegramConfig(
            token_path=str(token_path), chat_id=1, allowed_users=(2,)
        ),
        turn_handler=lambda *args: turns.append(args) or "seen",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )

    service._handle_update(
        {
            "update_id": 64,
            "message": {
                "message_id": 15,
                "chat": {"id": 1},
                "from": {"id": 2},
                "document": {
                    "file_id": "pdf",
                    "file_size": 3,
                    "mime_type": "application/pdf",
                },
            },
        }
    )

    assert turns == []


# ---------------------------------------------------------------------------
# send_reply — bounded retry, honest failure (never fabricate success)
# ---------------------------------------------------------------------------


def _service(tmp_path, **overrides: Any) -> TelegramService:
    token_path = tmp_path / "token"
    token_path.write_text("tok")
    overrides.setdefault("delivery_roots", [str(tmp_path)])
    overrides.setdefault("chat_id", 1)
    overrides.setdefault("allowed_users", (1,))
    return TelegramService(
        config=TelegramConfig(token_path=str(token_path), **overrides),
        turn_handler=lambda *a: "",
        command_handler=lambda *a: "",
        state_db=StateDatabase(tmp_path / "state.db"),
        execution_broker=_broker(),
    )


def test_send_reply_retries_then_succeeds(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 0.0)
    service = _service(tmp_path)
    attempts: list[int] = []

    def _flaky(
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        *,
        parse_mode: str | None = None,
    ) -> int:
        assert parse_mode == "HTML"
        attempts.append(1)
        if len(attempts) < 3:
            raise TelegramAPIError("transient")
        return 1

    monkeypatch.setattr(service.api, "send_message", _flaky)

    service.send_reply(1, 0, "hello")  # must not raise

    assert len(attempts) == 3


def test_send_reply_formats_markdown_as_safe_telegram_html(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    sent: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        service.api,
        "send_message",
        lambda chat_id, text, topic_id=None, **kwargs: sent.append(
            (text, kwargs.get("parse_mode"))
        )
        or 1,
    )

    service.send_reply(1, 0, "**ready** & <not-html>")

    assert sent == [("<b>ready</b> &amp; &lt;not-html&gt;", "HTML")]


def test_send_reply_raises_after_exhausting_retries(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 0.0)
    service = _service(tmp_path)
    attempts: list[int] = []

    def _always_fails(
        chat_id: int,
        text: str,
        topic_id: int | None = None,
        *,
        parse_mode: str | None = None,
    ) -> int:
        assert parse_mode == "HTML"
        attempts.append(1)
        raise TelegramAPIError("permanent")

    monkeypatch.setattr(service.api, "send_message", _always_fails)

    with pytest.raises(TelegramDeliveryError):
        service.send_reply(1, 0, "hello")

    assert len(attempts) == service_module._SEND_ATTEMPTS


def test_send_reply_raises_when_image_delivery_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 0.0)
    service = _service(tmp_path)
    monkeypatch.setattr(service.api, "send_message", lambda *a, **k: 1)
    monkeypatch.setattr(
        service.api,
        "send_photo",
        lambda *a, **k: (_ for _ in ()).throw(TelegramAPIError("no such file")),
    )

    with pytest.raises(TelegramDeliveryError):
        service.send_reply(1, 0, "look: [[send_image:/tmp/x.png]]")


def test_send_reply_delivers_text_and_image_when_both_succeed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    image = tmp_path / "x.png"
    image.write_bytes(b"image")
    sent_messages: list[str] = []
    sent_photos: list[str] = []
    monkeypatch.setattr(
        service.api,
        "send_message",
        lambda chat_id, text, topic_id=None, **kwargs: sent_messages.append(text) or 1,
    )
    monkeypatch.setattr(
        service.api,
        "send_photo",
        lambda chat_id, path, topic_id=None: sent_photos.append(str(path)) or 1,
    )

    service.send_reply(1, 0, f"caption text [[send_image:{image}]]")  # must not raise

    assert sent_messages == ["caption text"]
    assert sent_photos == [str(image)]


def test_send_reply_can_pin_its_exact_delivered_message(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(
        tmp_path,
        chat_id=-100123,
        agent_actions=("pin_reply",),
    )
    sent: list[str] = []
    pinned: list[tuple[int, int]] = []
    monkeypatch.setattr(
        service.api,
        "send_message",
        lambda chat_id, text, topic_id=None, **kwargs: sent.append(text) or 77,
    )
    monkeypatch.setattr(
        service.api,
        "pin_chat_message",
        lambda chat_id, message_id: pinned.append((chat_id, message_id)),
    )

    service.send_reply(-100123, 0, "Welcome to OpenBrum [[telegram_pin_reply]]")

    assert sent == ["Welcome to OpenBrum"]
    assert pinned == [(-100123, 77)]


def test_send_reply_can_pin_enabled_existing_message(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(
        tmp_path,
        chat_id=-100123,
        agent_actions=("pin_message",),
    )
    pinned: list[tuple[int, int]] = []
    monkeypatch.setattr(service.api, "send_message", lambda *args, **kwargs: 88)
    monkeypatch.setattr(
        service.api,
        "pin_chat_message",
        lambda chat_id, message_id: pinned.append((chat_id, message_id)),
    )

    service.send_reply(-100123, 0, "Done [[telegram_pin_message:42]]")

    assert pinned == [(-100123, 42)]


def test_send_reply_rejects_disabled_or_redirected_pin_before_sending(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, chat_id=-100123)
    sent: list[str] = []
    monkeypatch.setattr(
        service.api,
        "send_message",
        lambda chat_id, text, topic_id=None, **kwargs: sent.append(text) or 77,
    )

    with pytest.raises(TelegramDeliveryError, match="not enabled"):
        service.send_reply(-100123, 0, "No [[telegram_pin_reply]]")
    assert sent == []

    enabled = _service(
        tmp_path,
        chat_id=-100123,
        agent_actions=("pin_reply",),
    )
    monkeypatch.setattr(
        enabled.api,
        "send_message",
        lambda chat_id, text, topic_id=None, **kwargs: sent.append(text) or 77,
    )
    with pytest.raises(TelegramDeliveryError, match="configured chat"):
        enabled.send_reply(-100999, 0, "No [[telegram_pin_reply]]")
    assert sent == []


def test_send_reply_delivers_document_marker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    artifact = tmp_path / "manual.pdf"
    artifact.write_bytes(b"pdf")
    sent_documents: list[tuple[str, int | None]] = []
    monkeypatch.setattr(service.api, "send_message", lambda *a, **k: 1)
    monkeypatch.setattr(
        service.api,
        "send_document",
        lambda chat_id, path, topic_id=None: sent_documents.append((str(path), topic_id)) or 1,
    )

    service.send_reply(1, 9, f"Here it is [[send_document:{artifact}]]")

    assert sent_documents == [(str(artifact), 9)]


def test_send_reply_rejects_artifact_outside_configured_roots(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    outside = tmp_path.parent / "controller-secret"
    outside.write_bytes(b"secret")
    uploads: list[str] = []
    monkeypatch.setattr(service.api, "send_message", lambda *a, **k: 1)
    monkeypatch.setattr(
        service.api,
        "send_document",
        lambda _chat_id, path, topic_id=None: uploads.append(str(path)) or 1,
    )

    with pytest.raises(
        TelegramDeliveryError, match="outside configured delivery_roots"
    ):
        service.send_reply(1, 0, f"[[send_document:{outside}]]")

    assert uploads == []


@pytest.mark.parametrize("failure", [TelegramAPIError("upload broke"), ValueError("broken upload")])
def test_failed_document_is_quarantined_and_alerted_outside_topic(
    tmp_path, monkeypatch: pytest.MonkeyPatch, failure: Exception,
) -> None:
    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 0.0)
    quarantine = tmp_path / "quarantine"
    service = _service(tmp_path, delivery_quarantine_dir=str(quarantine))
    artifact = tmp_path / "app.apk"
    artifact.write_bytes(b"apk")
    sent_topics: list[int | None] = []
    monkeypatch.setattr(
        service.api,
        "send_message",
        lambda chat_id, text, topic_id=None, **kwargs: sent_topics.append(topic_id) or 1,
    )
    attempts = []

    def upload(*args, **kwargs):
        attempts.append(None)
        raise failure

    monkeypatch.setattr(service.api, "send_document", upload)

    expected = TelegramDeliveryError if isinstance(failure, TelegramAPIError) else ValueError
    with pytest.raises(expected) as caught:
        service.send_reply(1, 9, f"[[send_file:{artifact}]]")

    records = list(quarantine.glob("*.json"))
    if isinstance(failure, TelegramAPIError):
        assert len(attempts) == 3
        assert len(records) == 1
        assert '"kind": "document"' in records[0].read_text()
        assert sent_topics == [None]
    else:
        assert caught.value is failure
        assert len(attempts) == 1
        assert records == []
        assert sent_topics == []


def test_document_over_configured_cap_is_not_uploaded(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 0.0)
    service = _service(tmp_path, media_max_mb=1)
    artifact = tmp_path / "too-big.bin"
    artifact.write_bytes(b"x" * (1024 * 1024 + 1))
    uploads: list[str] = []
    monkeypatch.setattr(service.api, "send_message", lambda *a, **k: 1)
    monkeypatch.setattr(
        service.api,
        "send_document",
        lambda chat_id, path, topic_id=None: uploads.append(str(path)) or 1,
    )

    with pytest.raises(TelegramDeliveryError, match="configured Telegram cap is 1MB"):
        service.send_reply(1, 0, f"[[send_document:{artifact}]]")

    assert uploads == []


@pytest.mark.parametrize("failure_at", [None, "send", "acknowledge"])
def test_delivery_outbox_sends_declared_root_document_and_removes_job(
    tmp_path, monkeypatch: pytest.MonkeyPatch, failure_at: str | None,
) -> None:
    outbox = tmp_path / "outbox"
    quarantine = tmp_path / "quarantine"
    artifacts = tmp_path / "artifacts"
    outbox.mkdir()
    artifacts.mkdir()
    document = artifacts / "app.apk"
    document.write_bytes(b"apk")
    job = outbox / "001.json"
    job.write_text(
        '{"document": "' + str(document) + '", "caption": "signed", "thread_id": 8}'
    )
    service = _service(
        tmp_path,
        chat_id=-100123,
        topics={"builds": 8},
        delivery_outbox_dir=str(outbox),
        delivery_quarantine_dir=str(quarantine),
        delivery_roots=[str(artifacts)],
    )
    deliveries: list[tuple[int, int, str]] = []
    failure = ValueError("broken delivery formatting") if failure_at == "send" else OSError("acknowledgement failed")

    def send(chat_id, topic_id, text):
        if failure_at == "send":
            raise failure
        deliveries.append((chat_id, topic_id, text))

    monkeypatch.setattr(service, "send_reply", send)
    if failure_at == "acknowledge":
        unlink = Path.unlink

        def fail_acknowledgement(path, *args, **kwargs):
            if path == job:
                raise failure
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_acknowledgement)

    if failure_at is None:
        service._drain_delivery_outbox()
    else:
        with pytest.raises(type(failure)) as caught:
            service._drain_delivery_outbox()
        assert caught.value is failure

    assert deliveries == ([] if failure_at == "send" else [
        (-100123, 8, f"signed [[send_document:{document}]]"),
    ])
    assert job.exists() == (failure_at is not None)
    assert not quarantine.exists()


def test_delivery_outbox_rejects_destination_override_and_quarantines_job(tmp_path) -> None:
    outbox = tmp_path / "outbox"
    quarantine = tmp_path / "quarantine"
    artifacts = tmp_path / "artifacts"
    outbox.mkdir()
    artifacts.mkdir()
    job = outbox / "evil.json"
    job.write_text('{"chat_id": 999, "text": "redirect me"}')
    service = _service(
        tmp_path,
        chat_id=-100123,
        delivery_outbox_dir=str(outbox),
        delivery_quarantine_dir=str(quarantine),
        delivery_roots=[str(artifacts)],
    )

    service._drain_delivery_outbox()

    assert not job.exists()
    parked = [path for path in quarantine.glob("evil-*.json") if ".failure." not in path.name]
    failures = list(quarantine.glob("evil-*.failure.json"))
    assert len(parked) == 1
    assert len(failures) == 1
    assert "may not override" in failures[0].read_text()


def test_delivery_outbox_rejects_artifact_outside_declared_roots(tmp_path) -> None:
    outbox = tmp_path / "outbox"
    quarantine = tmp_path / "quarantine"
    artifacts = tmp_path / "artifacts"
    outbox.mkdir()
    artifacts.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (outbox / "outside.json").write_text('{"document": "' + str(outside) + '"}')
    service = _service(
        tmp_path,
        chat_id=-100123,
        delivery_outbox_dir=str(outbox),
        delivery_quarantine_dir=str(quarantine),
        delivery_roots=[str(artifacts)],
    )

    service._drain_delivery_outbox()

    failures = list(quarantine.glob("outside-*.failure.json"))
    assert len(failures) == 1
    assert "outside configured delivery_roots" in failures[0].read_text()


def test_attached_native_source_finishes_without_duplicate_execution_reply(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("tok")
    service = TelegramService(
        config=TelegramConfig(token_path=str(tmp_path / "token"), chat_id=1, allowed_users=(2,)),
        turn_handler=lambda *_args: "",
        command_handler=lambda *_args: "unused",
        state_db=StateDatabase(tmp_path / "state.db"), execution_broker=_broker(),
    )
    sent = []
    monkeypatch.setattr(service.api, "send_chat_action", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "send_reply", lambda *_args, **_kw: sent.append(_args))
    service._handle_update({"update_id": 74, "message": {
        "chat": {"id": 1}, "from": {"id": 2}, "text": "correction",
    }})
    assert not sent

def test_accepted_world_reply_delivery_retries_without_repeating_cognition(tmp_path, monkeypatch):
    from test_world_durability import runtime

    state, checkpoint, conversations, cognition = runtime(tmp_path)
    (tmp_path / "token").write_text("tok")
    update = {"update_id": 90, "message": {
        "chat": {"id": 1}, "from": {"id": 2}, "message_thread_id": 7,
        "text": "Save the handoff and admit its task.",
    }}
    service = TelegramService(
        TelegramConfig(token_path=str(tmp_path / "token"), chat_id=1, allowed_users=(2,)),
        lambda event, _chat, topic, user, text, images: conversations.run_turn(
            transport="telegram", transport_key=str(topic), source_event_key=event,
            operator_id=str(user), text=text, images=images,
        ).transport_reply,
        command_handler=lambda *args: "", state_db=state, execution_broker=_broker(),
    )
    attempts = []
    def send(_chat, text, **_kwargs):
        attempts.append(text)
        if len(attempts) <= 3:
            if len(attempts) == 3:
                service.request_stop()
            raise TelegramAPIError("accepted reply transport unavailable")
        return 91
    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(service.api, "send_chat_action", lambda *a, **kw: None)
    monkeypatch.setattr(service.api, "send_message", send)
    service._ingest_update(update)
    _drain(service)
    with state.connect() as connection:
        receipts = connection.execute("SELECT * FROM turns WHERE state='completed'").fetchall()
    # Delivery failed, so the update is still queued for another attempt.
    assert list(service._pending[(1, 7)]) == [update]
    assert len(receipts) == 1
    assert len(attempts) == 3
    assert cognition.calls == 1
    assert len(state.tasks.all()) == 1
    accepted_head = checkpoint.world._git("rev-parse", "HEAD")
    saved_files = checkpoint.world._git("show", "HEAD:decision.md")
    _drain(service)
    assert not service._has_pending()
    assert cognition.calls == 1
    assert len(state.tasks.all()) == 1
    assert len(attempts) == 4 and attempts[-1] == attempts[0]
    assert checkpoint.world._git("rev-parse", "HEAD") == accepted_head
    assert checkpoint.world._git("show", "HEAD:decision.md") == saved_files


@pytest.mark.parametrize("failure_type", [RuntimeExecutionError, ValueError])
def test_interrupted_reply_replay_reports_retained_work_without_rerunning(
    tmp_path, monkeypatch, failure_type
):
    from test_world_durability import EditingCognition, runtime

    failure = failure_type("execution stopped after writing partial work")
    def fail(_request):
        raise failure
    cognition = EditingCognition(before_return=fail)
    state, checkpoint, conversations, _ = runtime(tmp_path, cognition)
    (tmp_path / "token").write_text("tok")
    update = {"update_id": 92, "message": {
        "chat": {"id": 1}, "from": {"id": 2}, "text": "Investigate the handoff.",
    }}
    service = TelegramService(
        TelegramConfig(token_path=str(tmp_path / "token"), chat_id=1, allowed_users=(2,)),
        lambda event, _chat, topic, user, text, images: conversations.run_turn(
            transport="telegram", transport_key=str(topic), source_event_key=event,
            operator_id=str(user), text=text, images=images,
        ).transport_reply,
        command_handler=lambda *args: "", state_db=state, execution_broker=_broker(),
    )
    def unavailable(*_args, **_kwargs):
        service.request_stop()
        raise TelegramAPIError("Telegram unavailable")
    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(service.api, "send_chat_action", lambda *a, **kw: None)
    monkeypatch.setattr(service.api, "send_message", unavailable)
    if failure_type is ValueError:
        # A defect escapes the handler untouched; the executor pool logs it.
        with pytest.raises(ValueError) as caught:
            service._handle_update(update)
        assert caught.value is failure
    else:
        # An interrupted execution is reported to the operator, but the reply
        # transport is down too, so delivery fails.
        with pytest.raises(TelegramAPIError):
            service._handle_update(update)
    replies = []
    monkeypatch.setattr(service.api, "send_message", lambda _chat, text, **kw: replies.append(text) or 93)
    # Telegram redelivers the same update_id; the replay must not re-run cognition.
    service._handle_update(update)
    assert len(replies) == 1
    if failure_type is ValueError:
        assert "no durable acceptance receipt" in replies[0]
        assert "inspect" in replies[0]
    else:
        assert "Turn execution interrupted" in replies[0]
    assert cognition.calls == 1
    assert state.tasks.all() == []
    with state.connect() as connection:
        assert connection.execute("SELECT count(*) FROM turns WHERE output IS NOT NULL").fetchone()[0] == 0
        assert connection.execute("SELECT state FROM turns").fetchone()[0] == "interrupted"
    assert len(list((tmp_path / "worktrees").glob("session-*/decision.md"))) == 1


def _update(uid, text="hello", topic=7):
    return {"update_id": uid, "message": {
        "chat": {"id": 1}, "from": {"id": 1},
        "message_thread_id": topic, "text": text,
    }}


def test_route_logs_distinguish_missing_thread_and_harness_reply(tmp_path, monkeypatch, caplog):
    service = _service(tmp_path)
    general = _update(90, "private message")
    general["message"].pop("message_thread_id")
    general["message"]["message_id"] = 20
    dump = _update(91, "private message", topic=8)
    dump["message"]["message_id"] = 21
    monkeypatch.setattr(service.api, "send_message", lambda *args, **kwargs: 22)
    with caplog.at_level("INFO"):
        service._ingest_update(general)
        service._ingest_update(dump)
        service.send_result(1, 8, "private reply", "task_result:example")
    assert set(service._pending) == {(1, 0), (1, 8)}
    assert "thread_present=False wire_thread=None routed_topic=0" in caplog.text
    assert "thread_present=True wire_thread=8 routed_topic=8" in caplog.text
    assert "update=91 chat=1 message=21 sender=1" in caplog.text
    assert "reply confirmed chat=1 topic=8 message=22 receipt=" in caplog.text
    assert "private message" not in caplog.text and "private reply" not in caplog.text
def _passive_service(tmp_path, **overrides: Any) -> TelegramService:
    """A service whose topic 311 is a one-way feed and whose topic 7 is not."""
    service = _service(
        tmp_path, topics={"steward": 7, "dump": 311}, passive_topics=("dump",), **overrides
    )
    service.turn_handler = lambda *a: pytest.fail("a passive topic started a turn")
    service.command_handler = lambda *a: pytest.fail("a passive topic ran a command")
    return service


def test_a_passive_topic_consumes_its_update_without_acting(tmp_path, monkeypatch):
    """A feed must go inert without going unacknowledged.

    Leaving the update outstanding would hold the poll offset at it forever,
    and one feed post would then stop every other topic from being seen.
    """
    service = _passive_service(tmp_path)
    monkeypatch.setattr(
        service.api, "send_message", lambda *a, **kw: pytest.fail("a passive topic replied")
    )
    monkeypatch.setattr(service.api, "get_updates", lambda *a, **kw: [_update(90, topic=311)])

    service._poll_updates()

    assert not service._has_pending()
    assert service._offset == 91


@pytest.mark.parametrize("command", ["/pause", "/git reconcile app"])
def test_a_command_in_a_passive_topic_is_not_executed(tmp_path, monkeypatch, command):
    """Inline control commands and queued ones alike stop at the same gate."""
    service = _passive_service(tmp_path)
    monkeypatch.setattr(
        service.api, "send_message", lambda *a, **kw: pytest.fail("a passive topic replied")
    )

    service._ingest_update(_update(90, command, topic=311))

    assert not service._has_pending()


def test_an_undeclared_topic_is_still_admitted_beside_a_passive_one(tmp_path):
    """Absence means admitted: passive_topics is a deny-list, never an allowlist.

    This is the e8a6d36 contract — a thread the operator has just created is
    not in `topics` yet, and swallowing it silently is the bug that rollout
    fixed. Declaring one feed passive must not bring that allowlist back.
    """
    service = _passive_service(tmp_path)
    update = _update(90, topic=544)

    service._ingest_update(update)

    assert list(service._pending[(1, 544)]) == [update]


def test_a_passive_topic_drop_is_visible_in_the_log(tmp_path, caplog):
    """A silent drop would leave log quiet unable to confirm a feed is inert."""
    service = _passive_service(tmp_path)

    with caplog.at_level("INFO", logger=service_module.log.name):
        service._ingest_update(_update(90, topic=311))

    assert any(
        record.levelname == "INFO" and "311" in record.getMessage()
        for record in caplog.records
    )


def test_passive_topics_must_name_a_declared_topic():
    with pytest.raises(ValueError, match="passive_topics has unknown topics: ghost"):
        _config(topics={"dump": 311}, passive_topics=("ghost",))


def test_polled_updates_are_acknowledged_once_retained_and_requeued_after_restart(tmp_path, monkeypatch):
    """The offset is the acknowledgement: an update is retained, then acknowledged."""
    service = _service(tmp_path)
    updates = [_update(90), _update(91, "/tasks")]
    telegram = list(updates)

    def get_updates(offset, **_kwargs):
        telegram[:] = [update for update in telegram if update["update_id"] >= offset]
        return list(telegram)

    commands = []
    service.command_handler = lambda *args: commands.append(args) or "tasks"
    monkeypatch.setattr(service.api, "send_message", lambda *a, **kw: 5)
    monkeypatch.setattr(service.api, "get_updates", get_updates)
    for _ in range(3):
        service._poll_updates()
    assert telegram == [], "both updates are acknowledged although one is still owed"
    assert list(service._pending[(1, 7)]) == [updates[0]]
    assert len(commands) == 1

    # Restart before the owed update ran: its receipt, not Telegram, owes it.
    restarted = _service(tmp_path)
    restarted.command_handler = lambda *a: pytest.fail("completed command replayed")
    monkeypatch.setattr(restarted.api, "get_updates", get_updates)
    restarted.requeue()
    restarted._poll_updates()
    assert list(restarted._pending[(1, 7)]) == [updates[0]]


def test_requeue_asks_admission_again(tmp_path, monkeypatch):
    """A retained update is not a grant: a sender removed before restart stays out."""
    service = _service(tmp_path)
    monkeypatch.setattr(service.api, "get_updates", lambda *a, **kw: [_update(90)])
    service._poll_updates()
    assert list(service._pending[(1, 7)])

    restarted = _service(tmp_path, allowed_users=(2,))
    monkeypatch.setattr(restarted.api, "get_chat_administrators", lambda *a: [])
    restarted.requeue()
    assert not restarted._has_pending()
    again = _service(tmp_path)
    again.requeue()
    assert not again._has_pending(), "a refused update is finished, not owed"


def test_requeue_keeps_an_update_whose_admission_could_not_be_read(tmp_path, monkeypatch):
    service = _service(tmp_path, allowed_users=(2,), allow_group_administrators=True)
    monkeypatch.setattr(service.api, "get_chat_administrators", lambda *a: [
        {"status": "administrator", "user": {"id": 1}}])
    monkeypatch.setattr(service.api, "get_updates", lambda *a, **kw: [_update(90)])
    service._poll_updates()

    def unreachable(*_args):
        raise TelegramAPIError("network down")

    restarted = _service(tmp_path, allowed_users=(2,), allow_group_administrators=True)
    monkeypatch.setattr(restarted.api, "get_chat_administrators", unreachable)
    restarted.requeue()
    assert not restarted._has_pending()
    recovered = _service(tmp_path, allowed_users=(2,), allow_group_administrators=True)
    monkeypatch.setattr(recovered.api, "get_chat_administrators", lambda *a: [
        {"status": "administrator", "user": {"id": 1}}])
    recovered.requeue()
    assert recovered._has_pending(), "an unread administrator list is not a refusal"


def test_an_unread_administrator_list_retains_a_live_update(tmp_path, monkeypatch):
    service = _service(tmp_path, allowed_users=(2,), allow_group_administrators=True)

    def unreachable(*_args):
        raise TelegramAPIError("network down")

    monkeypatch.setattr(service.api, "get_chat_administrators", unreachable)
    monkeypatch.setattr(service.api, "get_updates", lambda *a, **kw: [_update(91)])
    service._poll_updates()
    assert not service._has_pending()
    # Telegram answers again while running: the retained update is re-asked.
    monkeypatch.setattr(service.api, "get_chat_administrators", lambda *a: [
        {"status": "administrator", "user": {"id": 1}}])
    monkeypatch.setattr(service.api, "get_updates", lambda *a, **kw: [])
    service._poll_updates()
    assert service._has_pending()
    restarted = _service(tmp_path, allowed_users=(2,), allow_group_administrators=True)
    monkeypatch.setattr(restarted.api, "get_chat_administrators", lambda *a: [
        {"status": "administrator", "user": {"id": 1}}])
    restarted.requeue()
    assert restarted._has_pending()


def test_unrecoverable_update_is_finished_rather_than_replayed_forever(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service._enqueue(_update(90))

    def broken(update, **_kwargs):
        service.request_stop()
        raise ValueError("not a transient failure")

    replies = []
    monkeypatch.setattr(service, "_handle_update", broken)
    monkeypatch.setattr(service.api, "send_message", lambda chat, text, **kw: replies.append(text))
    _drain(service)
    assert replies == ["Could not process this message: not a transient failure"]
    restarted = _service(tmp_path)
    restarted.requeue()
    assert not restarted._has_pending()


def test_live_input_does_not_overtake_retained_updates_in_its_topic(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service._native_turn_handler = lambda *a: pytest.fail("overtook a retained update")
    monkeypatch.setattr(service, "_ongoing_topics", lambda: {7})
    monkeypatch.setattr(service.api, "send_chat_action", lambda *a, **kw: None)
    first, second = _update(90, "first"), _update(91, "second")
    service._enqueue(first)
    service._ingest_update(second)
    assert list(service._pending[(1, 7)]) == [first, second]


def test_partial_reply_resumes_after_restart_without_resending_confirmed_pieces(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.turn_handler = lambda *a: "the reply"
    monkeypatch.setattr(service_module, "format_markdown_chunks", lambda text: ["first", "second"])
    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(service.api, "send_chat_action", lambda *a, **kw: None)
    sent = []

    def send(chat, text, **kw):
        if text == "second":
            raise TelegramAPIError("temporary outage")
        sent.append(text)
        return 5

    monkeypatch.setattr(service.api, "send_message", send)
    for _ in range(5):
        with pytest.raises(TelegramDeliveryError):
            service._handle_update(_update(90))
    assert sent == ["first"]

    restarted = _service(tmp_path)
    restarted.turn_handler = lambda *a: pytest.fail("cognition repeated")
    monkeypatch.setattr(restarted.api, "send_message", lambda chat, text, **kw: sent.append(text) or 6)
    restarted._handle_update(_update(90))
    restarted._handle_update(_update(90))
    assert sent == ["first", "second"]
    # Equal content from a distinct update must still be delivered.
    restarted.turn_handler = lambda *a: "the reply"
    monkeypatch.setattr(restarted.api, "send_chat_action", lambda *a, **kw: None)
    restarted._handle_update(_update(92))
    assert sent == ["first", "second", "first", "second"]


def test_busy_owner_does_not_spend_a_delivery_budget(tmp_path, monkeypatch):
    service = _service(tmp_path)
    calls = []

    def turn(*args):
        calls.append(args)
        if len(calls) <= 5:
            raise ConversationBusy("busy")
        service.request_stop()
        return "eventually delivered"

    service.turn_handler = turn
    sent = []
    monkeypatch.setattr(service.api, "send_chat_action", lambda *a, **kw: None)
    monkeypatch.setattr(service.api, "send_message", lambda chat, text, **kw: sent.append(text) or 5)
    monkeypatch.setattr(service._stop, "wait", lambda *a: False)
    service._ingest_update(_update(90))
    _drain(service)
    assert len(calls) == 6
    assert sent == ["eventually delivered"]


def test_a_rate_limited_send_waits_as_long_as_telegram_asked(monkeypatch):
    """Telegram's `retry_after` is the only backoff that clears the window."""
    slept: list[float] = []
    monkeypatch.setattr(service_module.time, "sleep", slept.append)
    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 1.0)

    calls = {"n": 0}

    def send():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TelegramAPIError("Telegram API HTTP 429", retry_after=32.0)
        return 7

    assert TelegramService._send_with_retry(send) == 7
    assert slept == [32.0], "guessing a shorter wait spends a retry inside the window"


def test_delivery_receipts_are_retained_until_poll_acknowledgement(tmp_path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(service.api, "send_message", lambda *a, **kw: 5)
    service.command_handler = lambda *a: "tasks"
    service._handle_update(_update(91, "/tasks"))
    receipt = service._receipt_path(_update(91))
    assert receipt.exists()
    service._offset = 92

    def unavailable(*a, **kw):
        raise TelegramAPIError("poll unavailable")

    monkeypatch.setattr(service.api, "get_updates", unavailable)
    service._poll_updates()
    assert receipt.exists()
    monkeypatch.setattr(service.api, "get_updates", lambda *a, **kw: [])
    service._poll_updates()
    assert not receipt.exists()


def test_inline_command_delivery_retry_does_not_execute_command_twice(tmp_path, monkeypatch):
    service = _service(tmp_path)
    commands = []
    service.command_handler = lambda *a: commands.append(a) or "paused"
    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 0)

    def unavailable(*a, **kw):
        raise TelegramAPIError("temporarily unavailable")

    monkeypatch.setattr(service.api, "send_message", unavailable)
    service._ingest_update(_update(91, "/pause"))
    assert service._has_pending()
    assert len(commands) == 1
    restarted = _service(tmp_path)
    restarted.command_handler = lambda *a: pytest.fail("pause executed again")
    sent = []
    monkeypatch.setattr(restarted.api, "send_message", lambda chat, text, **kw: sent.append(text) or 5)
    restarted.requeue()
    restarted._ingest_update(_update(91, "/pause"))
    update, _key = restarted._claim_next()
    restarted._handle_update(update)
    assert sent == ["paused"]
def test_redelivery_while_reply_is_owed_does_not_multiply(tmp_path, monkeypatch):
    """A redelivered update must not turn into many sends.

    Telegram redelivers whatever the last poll did not acknowledge. An update
    already retained and owed a reply is recognized by its receipt; each
    redelivery used to enqueue another copy, and every copy sent the reply.
    """
    from test_world_durability import runtime

    state, _checkpoint, conversations, cognition = runtime(tmp_path)
    (tmp_path / "token").write_text("tok")
    update = {"update_id": 90, "message": {
        "chat": {"id": 1}, "from": {"id": 2}, "message_thread_id": 7,
        "text": "Save the handoff and admit its task.",
    }}
    service = TelegramService(
        TelegramConfig(token_path=str(tmp_path / "token"), chat_id=1, allowed_users=(2,)),
        lambda event, _chat, topic, user, text, images: conversations.run_turn(
            transport="telegram", transport_key=str(topic), source_event_key=event,
            operator_id=str(user), text=text, images=images,
        ).transport_reply,
        command_handler=lambda *args: "", state_db=state, execution_broker=_broker(),
    )
    attempts = []

    def send(_chat, text, **_kwargs):
        attempts.append(text)
        service.request_stop()
        raise TelegramAPIError("accepted reply transport unavailable")

    monkeypatch.setattr(service_module, "_SEND_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(service.api, "send_chat_action", lambda *a, **kw: None)
    monkeypatch.setattr(service.api, "send_message", send)

    service._ingest_update(update)
    _drain(service)
    # Delivery failed, so the reply is still owed and the update stays queued.
    assert list(service._pending[(1, 7)]) == [update]
    sends_after_first = len(attempts)

    # Telegram redelivers the same update because the offset never advanced.
    for _ in range(5):
        service._ingest_update(update)

    assert list(service._pending[(1, 7)]) == [update]
    assert len(attempts) == sends_after_first
    assert cognition.calls == 1


def test_the_polling_thread_never_blocks_out_a_long_rate_limit(monkeypatch):
    """A rate limit on the poll thread must cost a reply, not inbound traffic.

    Control commands are handled inline in `_ingest_update` so they can
    overtake a busy topic — and inline means *on the polling thread*. Honouring
    a 429's `retry_after` there stalls every inbound update for as long as
    Telegram asked, which on a 300s window stops the whole transport to deliver
    one `/status`.
    """
    slept: list[float] = []
    monkeypatch.setattr(service_module.time, "sleep", slept.append)

    def rate_limited():
        raise TelegramAPIError("Telegram API HTTP 429", retry_after=120.0)

    with pytest.raises(TelegramAPIError):
        TelegramService._send_with_retry(
            rate_limited, max_wait_seconds=service_module._POLL_THREAD_MAX_WAIT
        )
    assert slept == [], "the poll thread must not wait out a 120s rate limit"

    # An executor thread is free to wait: that is what it is for.
    with pytest.raises(TelegramAPIError):
        TelegramService._send_with_retry(
            rate_limited, max_wait_seconds=service_module._MAX_RETRY_AFTER_WAIT
        )
    assert slept == [120.0, 120.0]


@pytest.mark.parametrize("native", [False, True])
def test_rate_limited_inline_reply_and_restart_replay_defer_to_executor(
    tmp_path, monkeypatch, native
):
    service = _service(tmp_path)
    executions = []
    reply = lambda *a: executions.append(a) or "accepted reply"
    service.command_handler = reply
    if native:
        service._native_turn_handler = reply
        service._ongoing_topics = lambda: (7,)
    update = _update(99, "hello" if native else "/status")
    sleeps = []
    monkeypatch.setattr(service_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(service.api, "send_chat_action", lambda *a, **kw: None)

    def limited(*a, **kw):
        raise TelegramAPIError("rate limited", retry_after=120)

    monkeypatch.setattr(service.api, "send_message", limited)
    service._ingest_update(update)
    assert sleeps == []
    assert list(service._pending[(1, 7)]) == [update]
    assert len(executions) == 1

    restarted = _service(tmp_path)
    restarted.command_handler = lambda *a: pytest.fail("command rerun")
    restarted.turn_handler = lambda *a: pytest.fail("cognition rerun")
    monkeypatch.setattr(restarted.api, "send_message", limited)
    restarted.requeue()
    restarted._ingest_update(update)
    assert sleeps == [], "the poller never replays a retained update itself"
    assert list(restarted._pending[(1, 7)]) == [update]
    attempts = []

    def recover(chat, text, **kw):
        attempts.append(text)
        if len(attempts) == 1:
            raise TelegramAPIError("rate limited", retry_after=120)
        restarted.request_stop()
        return 5

    monkeypatch.setattr(restarted.api, "send_message", recover)
    _drain(restarted)
    assert sleeps == [120]
    assert attempts == ["accepted reply", "accepted reply"]
    assert restarted._read_receipt(restarted._receipt_path(update))["done"]


@pytest.mark.parametrize("piece", ["artifact", "pin", "attachment-alert"])
def test_poll_wait_budget_reaches_every_reply_piece(tmp_path, monkeypatch, piece):
    service = _service(tmp_path, agent_actions=("pin_reply",))
    photo = tmp_path / "photo.png"
    photo.write_bytes(b"image")
    sleeps = []
    monkeypatch.setattr(service_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(service.api, "send_message", lambda *a, **kw: 5)
    monkeypatch.setattr(service.api, "send_photo", lambda *a, **kw: 6)
    monkeypatch.setattr(service.api, "pin_chat_message", lambda *a, **kw: True)

    def limited(*a, **kw):
        raise TelegramAPIError("rate limited", retry_after=120)

    if piece == "pin":
        text = "reply [[telegram_pin_reply]]"
        monkeypatch.setattr(service.api, "pin_chat_message", limited)
    elif piece == "artifact":
        text = f"reply [[send_image:{photo}]]"
        monkeypatch.setattr(service.api, "send_photo", limited)
    else:
        text = f"[[send_image:{tmp_path / 'missing.png'}]]"
        monkeypatch.setattr(service.api, "send_message", limited)
    with pytest.raises(TelegramDeliveryError):
        service.send_reply(1, 7, text, max_wait_seconds=service_module._POLL_THREAD_MAX_WAIT)
    assert sleeps == []
