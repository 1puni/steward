"""Socket Mode retains admitted events in the shared inbox before acknowledging them."""
from __future__ import annotations

import base64
import json
import os
import re
import stat
import threading
import time
from pathlib import Path

from steward_harness.config.schema import SlackConfig
from steward_harness.inbox import DEFERRALS, Inbox, InboundMessage, Source, settle
from steward_harness.receipts import write_receipt
from steward_harness.telegram.commands import CONTROL_COMMANDS
from steward_harness.telegram.format import extract_artifact_markers
from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeUnavailable
from .commands import command_text, observer_command, parse_command

TS = re.compile(r"[0-9]{1,20}\.[0-9]{6}")


class SlackAPIError(OSError):
    """Transient transport failure; the saved reply can be retried."""


class SlackDeliveryUnknown(OSError):
    """The request may have succeeded; inspect the receipt before any replay."""


class SlackRateLimited(SlackAPIError):
    def __init__(self, seconds: float):
        self.seconds = max(1, seconds)
        super().__init__("Slack rate limited; delivery retained")


class SlackContentRejected(Exception):
    """A permanent refusal settles this delivery rather than blocking later results."""


class SlackAPI:
    def __init__(self, token: str):
        from slack_sdk import WebClient
        self.client = WebClient(token=token, timeout=30, retry_handlers=[])

    def call(self, method: str, **kwargs):
        from slack_sdk.errors import SlackApiError
        try:
            return getattr(self.client, method)(**kwargs).data
        except SlackApiError as exc:
            response = exc.response
            if response.status_code == 429 or response.get("error") == "ratelimited":
                raise SlackRateLimited(float(response.headers.get("Retry-After", "1"))) from None
            code = str(response.get("error", "unknown"))
            if response.status_code >= 500 or code in {"internal_error", "fatal_error", "request_timeout", "service_unavailable"}:
                raise SlackDeliveryUnknown("Slack server error; delivery outcome unknown") from None
            # Credentials and temporary permissions can be repaired without discarding a result.
            if code in {"invalid_auth", "not_authed", "token_revoked", "account_inactive", "missing_scope", "token_expired", "not_in_channel", "no_permission"}:
                raise SlackAPIError("Slack authentication or permissions unavailable") from None
            raise SlackContentRejected("Slack rejected request: " + code) from None
        except (SlackAPIError, SlackContentRejected, SlackDeliveryUnknown):
            raise
        except Exception:
            # Do not expose SDK exception payloads, which may contain tokens or URLs.
            raise SlackDeliveryUnknown("Slack connection failed; delivery outcome unknown") from None


class SlackService:
    def __init__(self, config: SlackConfig, *, state, turn_handler, command_handler, api=None):
        self.config = config
        self.state = state
        self.turn_handler = turn_handler
        self.command_handler = command_handler
        self.api = api
        self.inbox = Inbox(Path(str(state.path) + ".slack-inbox"), retain_done=True)
        self._ingress_lock = threading.Lock()
        self._stop = threading.Event()
        self.socket = None

    @property
    def source(self) -> Source:
        return Source(self.inbox, self.answer, self.defers, self.conversation_key,
                      lambda message: tuple(map(int, message.record["payload"]["event"]["ts"].split("."))))

    @staticmethod
    def conversation_key(message):
        text = message.text.lstrip()
        parsed = parse_command("/" + text[1:]) if text.startswith("!") else None
        # Cheap controller commands must be able to cancel a running turn.
        control = bool(parsed and parsed.command and parsed.command.name in CONTROL_COMMANDS)
        return (message.record["route"], "control") if control else message.record["route"]

    @staticmethod
    def defers(error: BaseException) -> bool:
        return not isinstance(error, SlackDeliveryUnknown) and isinstance(error, (*DEFERRALS, OSError))

    def valid_route(self, route: str) -> bool:
        return self.config.valid_route(route)

    def admits(self, payload) -> bool:
        if not isinstance(payload, dict) or payload.get("type") != "event_callback":
            return False
        event = payload.get("event")
        return (isinstance(event, dict) and payload.get("team_id") == self.config.team_id
                and event.get("channel") == self.config.channel_id
                and event.get("type") == "message" and event.get("subtype") in (None, "file_share")
                and not any(event.get(k) for k in ("bot_id", "app_id", "hidden"))
                and isinstance(event.get("user"), str)
                and event["user"] in self.config.users
                and event.get("user_team", self.config.team_id) == self.config.team_id
                and isinstance(event.get("text", ""), str)
                and len(event.get("text", "")) <= 8000
                and isinstance(event.get("ts"), str)
                and isinstance(event.get("thread_ts", event["ts"]), str)
                and bool(TS.fullmatch(event["ts"]))
                and bool(TS.fullmatch(str(event.get("thread_ts", event.get("ts", "")))))
                and bool(re.fullmatch(r"Ev[A-Za-z0-9]{1,55}", str(payload.get("event_id", "")))))

    def ingest(self, payload, *, claim_control=False) -> InboundMessage | None:
        if not self.admits(payload):
            return
        event = payload["event"]
        name = payload["event_id"]
        route = f"{self.config.team_id}:{self.config.channel_id}:{event.get('thread_ts', event['ts'])}"
        with self._ingress_lock:
            if self._stop.is_set():
                raise OSError("Slack ingress stopping; event not retained")
            if self.inbox.holds(name):
                return
            record = {"kind": "message", "id": name,
                      "text": event.get("text") or "[Slack attachment]", "route": route,
                      "payload": payload}
            message = self.inbox.put(name, record)
            if claim_control and isinstance(self.conversation_key(message), tuple):
                return message
            self.inbox.requeue(message)
        return None

    def receive(self, client, request) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse
        # A stopping callback must remain unacknowledged so another process can retain it.
        if self._stop.is_set():
            return
        message = self.ingest(request.payload, claim_control=True) if request.type == "events_api" else None
        try:
            client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        finally:
            if message is not None:
                # Retention authorizes processing even if the ACK connection dies.
                # Slack's retry sees this same receipt, never another mutation.
                # A full conversation pool cannot starve this control fast path.
                settle(self.source, message)

    def start(self) -> None:
        from slack_sdk.socket_mode import SocketModeClient
        if self.api is None:
            self.api = SlackAPI(Path(self.config.bot_token_path).read_text().strip())
        me = self.api.call("auth_test")
        if me.get("team_id") != self.config.team_id or not me.get("bot_id"):
            raise ValueError("Slack bot token does not match configured workspace")
        self.inbox.recover()
        self.socket = SocketModeClient(app_token=Path(self.config.app_token_path).read_text().strip(),
                                      web_client=self.api.client, concurrency=1)
        self.socket.socket_mode_request_listeners.append(self.receive)
        self.socket.connect()

    def request_stop(self) -> None:
        self._stop.set()
        if self.socket is not None:
            self.socket.disconnect()

    def stop(self) -> None:
        self.request_stop()
        if self.socket is not None:
            self.socket.close()
        # Slack's synchronous WebClient holds no persistent HTTP session.

    def answer(self, message: InboundMessage) -> None:
        record = message.record
        if not self.admits(record["payload"]):
            return  # A retained event is not a permanent authority grant.
        save = lambda: write_receipt(message.path, record)
        if "reply" not in record:
            event = record["payload"]["event"]
            role = self.config.users[event["user"]]
            text = event.get("text", "").lstrip()
            parsed = parse_command("/" + text[1:]) if text.startswith("!") else None
            if parsed is not None and parsed.error:
                reply = command_text(parsed.error)
            elif parsed is not None and parsed.command is not None:
                cmd = parsed.command
                readonly = observer_command(cmd.name, cmd.arg)
                if role == "observer" and not readonly:
                    reply = "This command requires an explicitly configured operator or contributor."
                elif record.get("command_started"):
                    reply = "Command interrupted before its reply was retained. Inspect its effect before issuing it again."
                else:
                    if not readonly:
                        record["command_started"] = True
                        save()  # Mutating commands cannot be blindly repeated after a crash.
                    reply = self.command_handler(cmd.name, cmd.arg, record["route"], event["user"])
            elif role == "observer":
                reply = "Observer access: use !help, !status, !tasks or !task show <task_id>."
            elif event.get("files"):
                reply = "Inbound Slack files are not supported. Send the request as text; no turn was started."
            elif not text:
                reply = "Send a text request or !status."
            else:
                try:
                    speaker = dict(transport="slack", team=self.config.team_id,
                                   user=event["user"], role=role)
                    if event["user"] in self.config.user_names:
                        speaker["name"] = self.config.user_names[event["user"]]
                    attributed = "Slack sender: " + json.dumps(speaker, ensure_ascii=True) + "\n\n" + text
                    reply = self.turn_handler(message.msg_id, record["route"],
                                              f"{self.config.team_id}:{event['user']}", attributed)
                except (RuntimeExecutionError, RuntimeUnavailable) as exc:
                    reply = f"Turn execution interrupted: {type(exc).__name__}"
            record["reply"] = reply
            save()
        self._deliver(record["route"], record["reply"], record, save)

    def send_result(self, route: str, text: str, source_key: str) -> None:
        receipt = self.state.result_receipt(source_key)
        if not receipt or receipt.get("owner") != "slack:" + route:
            raise ValueError("Slack delivery requires the owner's shared result receipt")
        if not receipt.get("done"):
            self._deliver(route, text, receipt, lambda: self.state.save_result_receipt(receipt))

    def _read_artifact(self, raw_path):
        path = Path(raw_path)
        roots = [Path(root) for root in self.config.delivery_roots]
        if not path.is_absolute() or ".." in path.parts or not any(root in path.parents for root in roots):
            raise SlackContentRejected("attachment is outside configured delivery roots")
        # Resolve each component through directory descriptors: even a swapped
        # model-owned parent symlink cannot redirect the controller's read.
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as stream:
                meta = os.fstat(stream.fileno())
                if not stat.S_ISREG(meta.st_mode) or meta.st_size > self.config.max_file_bytes:
                    raise SlackContentRejected("attachment is not a bounded regular file")
                data = stream.read(self.config.max_file_bytes + 1)
        except OSError:
            raise SlackContentRejected("attachment unavailable or contains a symlink") from None
        finally:
            os.close(fd)
        return path.name, data

    def parts(self, text):
        clean, artifacts = extract_artifact_markers(str(text or ""))
        if len(artifacts) > 10 or len(clean) > 200_000:
            raise SlackContentRejected("reply exceeds transport size limit")
        parts = [{"kind": "text", "text": clean[i:i + 3500]} for i in range(0, len(clean), 3500)]
        total = 0
        for artifact in artifacts:
            name, data = self._read_artifact(artifact.path)
            total += len(data)
            if total > self.config.max_file_bytes:
                raise SlackContentRejected("attachments exceed size limit")
            parts.append({"kind": "file", "name": name, "bytes": base64.b64encode(data).decode()})
        return parts

    def _deliver(self, route, text, receipt, save) -> None:
        if not self.valid_route(route):
            raise SlackContentRejected("Slack route is outside the configured workspace/channel")
        if "slack_sending" in receipt:
            receipt["delivery_error"] = "Slack delivery outcome unknown; inspect before replay"
            save()
            raise SlackDeliveryUnknown(receipt["delivery_error"])
        if receipt.get("slack_retry_at", 0) > time.time():
            raise SlackAPIError("Slack retry is not due")
        _, channel, thread = route.split(":")
        if "slack_parts" not in receipt:
            # Snapshot bounded bytes before any external send; later model edits
            # cannot change the remainder of a partially delivered reply.
            receipt["slack_parts"] = self.parts(text)
            save()
        sent = receipt.setdefault("slack_sent", [])
        for index, part in enumerate(receipt["slack_parts"][len(sent):], start=len(sent)):
            receipt["slack_sending"] = index
            save()  # A crash or lost response is not permission to resend.
            try:
                if part["kind"] == "text":
                    result = self.api.call("chat_postMessage", channel=channel, thread_ts=thread,
                                           text=part["text"], mrkdwn=False, parse="none",
                                           unfurl_links=False, unfurl_media=False)
                    if result.get("channel") != channel or not TS.fullmatch(str(result.get("ts", ""))):
                        raise SlackDeliveryUnknown("Slack response lacks a matching message receipt")
                    evidence = {"ts": result["ts"]}
                else:
                    result = self.api.call("files_upload_v2", channel=channel, thread_ts=thread,
                                           filename=part["name"], file=base64.b64decode(part["bytes"]))
                    files = result.get("files", [])
                    if not files or not all(f.get("id") for f in files):
                        raise SlackDeliveryUnknown("Slack response lacks file completion receipts")
                    evidence = {"files": [f["id"] for f in files]}
            except (SlackAPIError, SlackContentRejected) as error:
                receipt.pop("slack_sending")  # Explicit refusal; no visible send to duplicate.
                if isinstance(error, SlackRateLimited):
                    receipt["slack_retry_at"] = time.time() + error.seconds
                save()
                raise
            except Exception:
                receipt["delivery_error"] = "Slack delivery outcome unknown; inspect before replay"
                save()
                raise SlackDeliveryUnknown(receipt["delivery_error"]) from None
            sent.append(dict(channel=channel, thread_ts=thread, **evidence))
            receipt.pop("slack_sending")
            receipt.pop("slack_retry_at", None)
            receipt.pop("delivery_error", None)
            save()
