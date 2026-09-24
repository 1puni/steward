"""Telegram transport service with background polling and durable outbox delivery."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from steward_harness.config.schema import TelegramConfig
from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeUnavailable
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import ConversationBusy, StateDatabase
from steward_harness.telegram.api import TelegramAPI, TelegramAPIError
from steward_harness.telegram.commands import (
    BUILTIN_COMMAND_DESCRIPTIONS,
    BUILTIN_COMMAND_MODES,
    CONTROL_COMMANDS,
    CommandArgumentMode,
    parse_inbound_text,
)
from steward_harness.telegram.format import (
    ArtifactKind,
    OutboundArtifact,
    TelegramActionKind,
    extract_artifact_markers,
    extract_telegram_action_markers,
    format_markdown_chunks,
)
from steward_harness.lease import Busy
from steward_harness.receipts import write_receipt
from steward_harness.world.turn_checkpoint import WorldContentConflict, WorldUpdatePending

log = logging.getLogger(__name__)

_SEND_ATTEMPTS = 3
_SEND_RETRY_BACKOFF_SECONDS = 1.0
# Executor threads may wait out Telegram's rate limit. The poller must stay free.
_MAX_RETRY_AFTER_WAIT = 300.0
_POLL_THREAD_MAX_WAIT = 5.0
_EXECUTOR_WORKERS = 10
# How stale an admission may be after the operator promotes or demotes someone
# in the group. A minute keeps a grant usable almost immediately while costing
# at most one call per minute per conversation.
_ADMINISTRATOR_TTL_SECONDS = 60.0

# topic_key = (chat_id, message_thread_id)
_TopicKey = tuple[int, int]


class InboundMediaRejected(TelegramAPIError):
    """This message's media will never be acceptable, so do not retry it.

    Subclasses `TelegramAPIError` because that is what it is, and is answered
    at the message rather than reaching the executor, which classifies that
    class as transient and requeues it. Requeueing is right for everything
    else that class covers — the network being the network. It is wrong here:
    a cap violation is a property of the message, so retrying it every second
    never succeeds and stops that topic behind it.
    """


class TelegramDeliveryError(TelegramAPIError):
    """A reply could not be fully delivered after exhausting retries.

    Never swallow this into a log line: a caller that only logs it recreates the
    exact "silent failure, fabricated success" bug this class exists to prevent.
    """


def probe_chat_access(api: TelegramAPI, config: TelegramConfig) -> None:
    """Verify the bot can actually operate in the configured chat before polling starts.

    A configured topic requires a forum chat. Administrator membership is
    required only for explicitly configured administrative actions.

    `allow_group_administrators` is proved by making the call it depends on
    rather than by inferring it from the bot's own status: a non-administrator
    bot may list a supergroup's administrators, so a status check would refuse
    a configuration that works. Failing here is the point — the alternative is
    a flag that looks enabled and silently admits nobody.
    """
    try:
        me = api.get_me()
        bot_id = me.get("id") if isinstance(me, dict) else None
        if not isinstance(bot_id, int) or bot_id <= 0:
            raise TelegramAPIError("getMe returned no usable bot id")
        chat = api.get_chat(config.chat_id)
        if not isinstance(chat, dict) or chat.get("id") != config.chat_id:
            raise TelegramAPIError("configured chat is not reachable")
        member: dict[str, Any] | None = None
        if config.agent_actions:
            raw_member = api.get_chat_member(config.chat_id, bot_id)
            member = raw_member if isinstance(raw_member, dict) else None
        if config.topics:
            if chat.get("type") != "supergroup" or chat.get("is_forum") is not True:
                raise TelegramAPIError("configured chat is not a forum but topics are configured")
        if config.agent_actions:
            member_user = member.get("user") if isinstance(member, dict) else None
            if (
                not isinstance(member, dict)
                or not isinstance(member_user, dict)
                or member_user.get("id") != bot_id
                or member.get("status") not in {"administrator", "creator"}
                or (
                    member.get("status") != "creator"
                    and member.get("can_pin_messages") is not True
                )
            ):
                raise TelegramAPIError("bot is not an administrator with pin permission")
        if config.allow_group_administrators:
            api.get_chat_administrators(config.chat_id)
    except TelegramAPIError:
        raise
    except Exception as exc:
        raise TelegramAPIError(f"chat access probe failed: {exc}") from exc


class TelegramService:
    """Multi-threaded Telegram gateway for long-polling and outbox draining."""

    def __init__(
        self,
        config: TelegramConfig,
        turn_handler: Callable[[str, int, int, int, str, tuple[Path, ...]], str],
        *,
        state_db: StateDatabase,
        execution_broker: UntrustedExecutionBroker,
        command_handler: Callable[[str, str | None, int, int, int], str],
        ongoing_topics: Callable[[], tuple[int, ...]] = lambda: (),
        native_turn_handler: Callable[[str, int, int, int, str, tuple[Path, ...]], str] | None = None,
    ) -> None:
        self.config = config
        token = self._read_token(config.token_path)
        self.api = TelegramAPI(token, base_url=config.botapi_base or "https://api.telegram.org")
        self.allowed_users = frozenset(config.allowed_users)
        self.allow_group_administrators = config.allow_group_administrators
        # Administrators change rarely and are re-read per admitted sender, so
        # the list is cached briefly: without it every message from an operator
        # who is not in `allowed_users` costs a round trip before the first byte
        # of work, and a busy topic would spend its rate limit on admission.
        self._administrator_lock = threading.Lock()
        self._administrators: frozenset[int] = frozenset()
        # None, not 0.0: `monotonic()` counts from boot, so a zero timestamp
        # would read as fresh for the first minute of uptime and refuse
        # everyone against an empty list that was never fetched.
        self._administrators_read_at: float | None = None
        # Resolved once: the config names topics, the wire carries ids.
        self.passive_topics = frozenset(
            config.topics[name] for name in config.passive_topics
        )
        self.turn_handler = turn_handler
        self.command_handler = command_handler
        self._ongoing_topics = ongoing_topics
        self._native_turn_handler = native_turn_handler
        self._commands = dict(BUILTIN_COMMAND_DESCRIPTIONS)
        self._command_modes = dict(BUILTIN_COMMAND_MODES)
        for name, adapter in config.adapter_commands.items():
            self._commands[name] = adapter.description
            self._command_modes[name] = CommandArgumentMode(adapter.argument_mode)
        self._stop = threading.Event()
        self._execution_broker = execution_broker
        self._state = state_db
        if config.inbound_media_dir:
            self._inbound_media_dir = Path(config.inbound_media_dir).resolve()
        else:
            self._inbound_media_dir = state_db.path.parent / "telegram-input"
        # Per-topic queues of updates that owe execution. Each is also written
        # into its receipt file before the poll offset moves past it, so the
        # offset is the acknowledgement and the receipt files are the spool.
        self._queue_lock = threading.Lock()
        self._pending: dict[_TopicKey, deque[dict[str, Any]]] = {}
        self._in_flight: set[_TopicKey] = set()
        # Retained updates whose sender's admission could not be read yet.
        self._unadmitted: dict[int, dict[str, Any]] = {}
        self._delivery_context = threading.local()
        # Transport receipts belong beside controller state, not in agent worktrees.
        self._receipt_dir = (
            state_db.path.parent / f"{state_db.path.name}.telegram-receipts"
            / str(config.chat_id)
        )
        self._receipt_floor = 0
        self._offset = 0
        self._threads: tuple[threading.Thread, ...] = ()
        self._executor = ThreadPoolExecutor(
            max_workers=_EXECUTOR_WORKERS, thread_name_prefix="telegram-executor"
        )

    @staticmethod
    def _read_token(token_path: str) -> str:
        try:
            return open(token_path, "r", encoding="utf-8").read().strip()
        except OSError as exc:
            raise RuntimeError(f"Could not read Telegram token from {token_path}: {exc}")

    def start(self) -> None:
        """Validate transport before admitting any polling or delivery work."""
        probe_chat_access(self.api, self.config)
        self._register_commands()
        self.requeue()
        # A long poll held open against a remote API is a real I/O boundary,
        # so these are threads. Nothing supervises them: an exception leaves
        # the thread, `threading.excepthook` logs it, and the process exits
        # for the service manager to restart. They are built here rather than
        # in `__init__` so each one calls whatever the step is by then.
        self._threads = (
            self._loop("telegram", lambda: self._poll_updates(), 3),
            *((self._loop("telegram-outbox", lambda: self._drain_delivery_outbox(), 1),)
              if self.config.delivery_outbox_dir is not None else ()),
        )
        for thread in self._threads:
            thread.start()
        # Executor threads run until _stop is set and nothing is pending.
        for _ in range(_EXECUTOR_WORKERS):
            self._executor.submit(self._executor_loop)

    def requeue(self) -> None:
        """Queue every update acknowledged before a restart and still owed work.

        Admission is asked again: configuration may have changed since the
        update was retained, and a retained update is not a grant.
        """
        for path in sorted(self._receipt_dir.glob("*.json"), key=lambda p: int(p.stem)):
            receipt = self._read_receipt(path)
            if "update" not in receipt or receipt.get("done"):
                continue
            self._readmit(receipt["update"])

    def _readmit(self, update: dict[str, Any]) -> None:
        """Queue a retained update once admitted, finish it once refused."""
        admitted = self._admits(update["message"])
        uid = self._update_key(update)
        if admitted is None:
            if uid is not None:
                self._unadmitted[uid] = update
            return
        if uid is not None:
            self._unadmitted.pop(uid, None)
        if admitted:
            self._enqueue(update)
        elif (path := self._receipt_path(update)) is not None:
            write_receipt(path, {**self._read_receipt(path), "done": True})

    def _poll_updates(self) -> list[dict[str, Any]] | None:
        offset = self._offset
        try:
            updates = self.api.get_updates(offset, timeout_seconds=15)
        except TelegramAPIError as exc:
            log.warning("Telegram polling error: %s (backing off 3s)", exc)
            return None
        # A successful poll acknowledges everything below its offset. Keep a
        # finished receipt until then, so a redelivered update is recognized.
        if offset > self._receipt_floor:
            for path in self._receipt_dir.glob("*.json"):
                if int(path.stem) < offset and self._read_receipt(path).get("done"):
                    path.unlink()
            self._receipt_floor = offset
        # Telegram answered, so ask again about senders it could not vouch for.
        for update in list(self._unadmitted.values()):
            self._readmit(update)
        for update in updates:
            if self._stop.is_set():
                break
            self._ingest_update(update)
            update_id = update.get("update_id")
            if isinstance(update_id, int) and update_id >= 0:
                self._offset = max(self._offset, update_id + 1)
        return updates

    def _receipt_path(self, update: dict[str, Any]) -> Path | None:
        uid = self._update_key(update)
        return self._receipt_dir / f"{uid}.json" if uid is not None else None

    @staticmethod
    def _read_receipt(path: Path | None) -> dict[str, Any]:
        return json.loads(path.read_text()) if path is not None and path.exists() else {}

    def _save_receipt(self) -> None:
        path = self._delivery_context.path
        if path is None:
            return
        write_receipt(path, self._delivery_context.receipt)

    def _ingest_update(self, update: dict[str, Any]) -> None:
        known = self._read_receipt(self._receipt_path(update))
        if known.get("done") or "update" in known:
            return
        self._delivery_context.path = self._receipt_path(update)
        self._delivery_context.receipt = self._read_receipt(self._delivery_context.path)
        try:
            self._route_update(update)
        except TelegramAPIError:
            self._enqueue(update)
        finally:
            self._delivery_context.path = None

    def _enqueue(self, update: dict[str, Any]) -> None:
        """Retain the update in its receipt, then queue it behind its topic."""
        path = self._receipt_path(update)
        if path is not None:
            receipt = self._read_receipt(path)
            if "update" not in receipt:
                write_receipt(path, {**receipt, "update": update})
        message = update["message"]
        key = (message["chat"]["id"], message.get("message_thread_id", 0))
        with self._queue_lock:
            self._pending.setdefault(key, deque()).append(update)

    def _route_update(self, update: dict[str, Any]) -> None:
        """Route one inbound update: control commands inline, else enqueue.

        Every update enters here; nothing downstream re-authenticates.
        """
        message = update.get("message")
        if not isinstance(message, dict):
            return

        admitted = self._admits(message)
        if admitted is None:
            # Unknown is not a refusal: retain it and ask again after the
            # next successful poll, or at the next start.
            path = self._receipt_path(update)
            if path is not None:
                write_receipt(path, {**self._read_receipt(path), "update": update})
            if (uid := self._update_key(update)) is not None:
                self._unadmitted[uid] = update
            return
        if not admitted:
            return
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        topic_id = message.get("message_thread_id", 0)

        if self._delivery_context.receipt.get("reply") is not None:
            self._handle_update(update, max_wait_seconds=_POLL_THREAD_MAX_WAIT)
            return

        log.info(
            "Telegram ingress update=%s chat=%s message=%s sender=%s sender_is_bot=%s "
            "thread_present=%s wire_thread=%s routed_topic=%s",
            update.get("update_id"), chat_id, message.get("message_id"), user_id,
            message.get("from", {}).get("is_bot"), "message_thread_id" in message,
            message.get("message_thread_id"), message.get("message_thread_id", 0),
        )

        # Control commands are handled inline so they can overtake a busy topic.
        if self._is_control_command(update):
            try:
                self._handle_update(update, max_wait_seconds=_POLL_THREAD_MAX_WAIT)
            except TelegramAPIError:
                raise
            except Exception as exc:
                log.warning("Control command handling error: %s", exc)
            return

        # Text-only updates for a topic with an active native execution are
        # routed directly to _native_turn_handler — a routing decision, not a
        # separate thread.
        if self._native_turn_handler is not None:
            raw_text = message.get("text") or message.get("caption") or ""
            has_attachments = bool(
                message.get("photo") or (
                    isinstance(message.get("document"), dict)
                    and isinstance(message["document"].get("mime_type"), str)
                    and message["document"]["mime_type"].startswith("image/")
                )
            )
            if not has_attachments and isinstance(raw_text, str) and raw_text.strip():
                with self._queue_lock:
                    queued = bool(self._pending.get((chat_id, topic_id)))
                # Retained updates in this topic come first; delivering this
                # one into the live execution would overtake them.
                if topic_id in self._ongoing_topics() and not queued:
                    event_id = f"tg_{update.get('update_id')}"
                    text = raw_text
                    self.api.send_chat_action(chat_id, "typing", topic_id=topic_id)
                    try:
                        raw_reply = self._native_turn_handler(
                            event_id, chat_id, topic_id, user_id, text, ()
                        )
                    except (TelegramAPIError, Busy, ConversationBusy,
                            WorldUpdatePending, WorldContentConflict) as exc:
                        log.debug("Native routing transient error, enqueueing: %s", exc)
                        # Falls through to enqueue below.
                    except (RuntimeExecutionError, RuntimeUnavailable) as exc:
                        log.warning("Turn execution interrupted: %s", exc)
                        self.send_reply(chat_id, topic_id, f"Turn execution interrupted: {exc}",
                                        max_wait_seconds=_POLL_THREAD_MAX_WAIT)
                        return
                    except Exception as exc:
                        log.warning("Native routing error: %s", exc)
                        return
                    else:
                        if raw_reply:
                            self.send_reply(chat_id, topic_id, raw_reply,
                                            max_wait_seconds=_POLL_THREAD_MAX_WAIT)
                        self._delivery_context.receipt["done"] = True
                        self._save_receipt()
                        return

        self._enqueue(update)

    def _admits(self, message: dict[str, Any]) -> bool | None:
        """This chat, an admitted sender, and not a passive topic.

        A passive topic is a one-way feed, so nothing posted there may act —
        not a turn, not a receipt replay, not a control command: a `/status`
        in a feed is inert for the same reason a sentence is. The drop is
        logged at info so log quiet can still confirm the feeds are inert.
        None means "not now": the administrator list could not be read.
        """
        if message.get("chat", {}).get("id") != self.config.chat_id:
            return False
        user_id = message.get("from", {}).get("id")
        if not isinstance(user_id, int) or user_id <= 0:
            return False
        topic_id = message.get("message_thread_id", 0)
        if topic_id in self.passive_topics:
            log.info("Ignoring update in passive Telegram topic %s", topic_id)
            return False
        if user_id in self.allowed_users:
            return True
        return self._is_group_administrator(user_id)

    def _is_group_administrator(self, user_id: int) -> bool | None:
        """Admit a current administrator of the configured chat, failing closed.

        An unreachable Telegram means "not admitted", never "admitted": the
        flag widens who may drive the steward, so a network fault must not be
        able to widen it further. Configured `allowed_users` are checked before
        this and so keep working through exactly that outage. A failed read is
        not cached either, so admission recovers on the next message rather
        than staying shut for the rest of the TTL. That unread case is None,
        which refuses like False but is not a verdict.
        """
        if not self.allow_group_administrators:
            return False
        with self._administrator_lock:
            read_at = self._administrators_read_at
            if read_at is not None and time.monotonic() - read_at < _ADMINISTRATOR_TTL_SECONDS:
                return user_id in self._administrators
        try:
            members = self.api.get_chat_administrators(self.config.chat_id)
        except TelegramAPIError as exc:
            log.warning("Could not read Telegram chat administrators: %s", exc)
            return None
        # `is_bot` is excluded deliberately. Telegram drops *other* bots from
        # this listing but returns the caller's own account, which for some bots
        # is an administrator — so without this the steward's bot id lands in
        # the admitted set. Nothing can reach admission under that id today,
        # because Telegram does not deliver a bot's own messages back through
        # getUpdates, but an admission set that contains a non-human is a
        # latent hole and not one worth leaving for a future transport.
        administrators = frozenset(
            member["user"]["id"]
            for member in members
            if member.get("status") in {"administrator", "creator"}
            and isinstance(member.get("user"), dict)
            and isinstance(member["user"].get("id"), int)
            and not member["user"].get("is_bot")
        )
        with self._administrator_lock:
            self._administrators = administrators
            self._administrators_read_at = time.monotonic()
        return user_id in administrators

    def _executor_loop(self) -> None:
        """Claim and process one update at a time until stopped and empty."""
        while not self._stop.is_set() or self._has_pending():
            update, topic_key = self._claim_next()
            if update is None:
                time.sleep(0.05)
                continue
            try:
                self._handle_update(update)
            except (TelegramAPIError, Busy, ConversationBusy,
                    WorldUpdatePending, WorldContentConflict) as exc:
                log.debug("Transient error processing update, requeueing: %s", exc)
                # Return to the front so per-topic ordering is preserved.
                with self._queue_lock:
                    if topic_key not in self._pending:
                        self._pending[topic_key] = deque()
                    self._pending[topic_key].appendleft(update)
                if self._stop.is_set():
                    # Shutting down: retrying a busy owner cannot succeed, and
                    # draining-while-pending would spin here forever, hanging
                    # stop()'s executor join. Its receipt requeues it on the
                    # next start.
                    return
                self._stop.wait(1.0)
            except Exception as exc:
                log.error("Unrecoverable error processing Telegram update: %s", exc)
                path = self._receipt_path(update)
                if path is not None:
                    write_receipt(path, {**self._read_receipt(path), "done": True})
                # Finished is not silent: the sender learns it was dropped.
                # Plain text, so nothing in the error can act as a marker.
                message = update["message"]
                try:
                    self.api.send_message(message["chat"]["id"],
                                          f"Could not process this message: {exc}",
                                          topic_id=message.get("message_thread_id") or None)
                except Exception as error:
                    log.warning("Could not report the dropped update: %s", error)
            finally:
                with self._queue_lock:
                    self._in_flight.discard(topic_key)

    @staticmethod
    def _update_key(update: dict[str, Any]) -> int | None:
        update_id = update.get("update_id")
        return update_id if isinstance(update_id, int) else None

    def _has_pending(self) -> bool:
        with self._queue_lock:
            return any(bool(q) for q in self._pending.values())

    def _claim_next(self) -> tuple[dict[str, Any] | None, _TopicKey | None]:
        """Pop the oldest update from a topic not currently in flight."""
        with self._queue_lock:
            for topic_key, q in self._pending.items():
                if topic_key in self._in_flight or not q:
                    continue
                update = q.popleft()
                if not q:
                    del self._pending[topic_key]
                self._in_flight.add(topic_key)
                return update, topic_key
        return None, None

    def _loop(
        self, name: str, step: Callable[[], object], poll_seconds: float
    ) -> threading.Thread:
        """One thread repeating one step until stopped; no restart, no latch.

        A step that produced something is asked again at once — more updates
        are probably waiting behind the ones it just took. Only an empty step
        sleeps.
        """

        def run() -> None:
            while not self._stop.is_set():
                if step() is None:
                    self._stop.wait(poll_seconds)

        return threading.Thread(target=run, name=f"steward-{name}", daemon=True)

    def request_stop(self) -> None:
        """Stop new polling/claims without cancelling the current obligation."""
        self._stop.set()

    def stop(self) -> None:
        """Drain owned writers before the controller can release its lease."""
        self.request_stop()
        for thread in self._threads:
            if thread.ident is not None and thread is not threading.current_thread():
                thread.join()
        self._executor.shutdown(wait=True)
        self.api.close()

    @staticmethod
    def _is_control_command(update: dict[str, Any]) -> bool:
        message = update.get("message")
        text = message.get("text") if isinstance(message, dict) else None
        if not isinstance(text, str):
            return False
        parsed = parse_inbound_text(text)
        return bool(
            parsed.command is not None
            and parsed.command.name in CONTROL_COMMANDS
        )

    def _register_commands(self) -> None:
        """Publish the whitelisted command list to Telegram's UI (idempotent)."""
        payload = [
            {"command": name, "description": desc}
            for name, desc in self._commands.items()
        ]
        try:
            self.api.set_my_commands(payload)
        except TelegramAPIError as exc:
            log.warning("Failed to register Telegram bot commands: %s", exc)

    def _handle_update(
        self, update: dict[str, Any], *,
        max_wait_seconds: float = _MAX_RETRY_AFTER_WAIT,
    ) -> None:
        self._delivery_context.path = self._receipt_path(update)
        self._delivery_context.receipt = self._read_receipt(self._delivery_context.path)
        try:
            receipt = self._delivery_context.receipt
            if receipt.get("done"):
                return
            if "reply" in receipt:
                chat, topic, text = receipt["reply"]
                self.send_reply(chat, topic, text, max_wait_seconds=max_wait_seconds)
            else:
                self._execute_update(update, max_wait_seconds=max_wait_seconds)
            receipt["done"] = True
            self._save_receipt()
        finally:
            self._delivery_context.path = None

    def _execute_update(
        self, update: dict[str, Any], *,
        max_wait_seconds: float = _MAX_RETRY_AFTER_WAIT,
    ) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat_id = message.get("chat", {}).get("id")
        user_id = message.get("from", {}).get("id")
        raw_text = message.get("text") or message.get("caption") or ""
        text = raw_text if isinstance(raw_text, str) else ""
        remote_attachments = self._remote_image_metadata(message)
        if not text.strip() and not remote_attachments:
            return

        topic_id = message.get("message_thread_id", 0)
        event_id = f"tg_{update.get('update_id')}"

        parsed = parse_inbound_text(text, self._command_modes)

        if parsed.error:
            self.send_reply(chat_id, topic_id, parsed.error, max_wait_seconds=max_wait_seconds)
            return

        if parsed.command is not None:
            cmd_reply = self.command_handler(
                parsed.command.name, parsed.command.arg,
                chat_id, topic_id, user_id,
            )
            self.send_reply(chat_id, topic_id, cmd_reply, max_wait_seconds=max_wait_seconds)
            return

        try:
            images = self._materialize_attachments(
                update_id=int(update.get("update_id", 0)),
                message_id=int(message.get("message_id", 0)),
                metadata=remote_attachments,
            )
        except InboundMediaRejected as exc:
            # Tell the sender rather than dropping it into a log: they are
            # holding the phone that sent it, and nothing will ever make this
            # message acceptable.
            self.send_reply(chat_id, topic_id, str(exc), max_wait_seconds=max_wait_seconds)
            return
        if not text.strip():
            text = "Please inspect the attached image."

        # Regular turn execution: trigger typing action
        self.api.send_chat_action(chat_id, "typing", topic_id=topic_id)

        try:
            raw_reply = self.turn_handler(event_id, chat_id, topic_id, user_id, text, images)
        except (RuntimeExecutionError, RuntimeUnavailable) as exc:
            log.warning("Turn execution interrupted: %s", exc)
            raw_reply = f"Turn execution interrupted: {exc}"
        if raw_reply:
            self.send_reply(chat_id, topic_id, raw_reply, max_wait_seconds=max_wait_seconds)

    @staticmethod
    def _remote_image_metadata(message: Mapping[str, object]) -> tuple[dict[str, object], ...]:
        candidates: list[dict[str, object]] = []
        raw_photos = message.get("photo")
        photos = (
            [item for item in raw_photos if isinstance(item, dict)]
            if isinstance(raw_photos, list)
            else []
        )
        if photos:
            largest = max(
                photos,
                key=lambda item: item.get("file_size", 0)
                if isinstance(item.get("file_size"), int)
                else 0,
            )
            file_id = largest.get("file_id")
            if isinstance(file_id, str) and 1 <= len(file_id) <= 512:
                size = largest.get("file_size", 0)
                candidates.append(
                    {
                        "file_id": file_id,
                        "mime_type": "image/jpeg",
                        "declared_size": size if isinstance(size, int) else 0,
                    }
                )
        raw_document = message.get("document")
        document = raw_document if isinstance(raw_document, dict) else {}
        file_id = document.get("file_id")
        mime_type = document.get("mime_type")
        if (
            isinstance(file_id, str)
            and 1 <= len(file_id) <= 512
            and isinstance(mime_type, str)
            and mime_type.startswith("image/")
            and len(mime_type) <= 256
        ):
            size = document.get("file_size", 0)
            candidates.append(
                {
                    "file_id": file_id,
                    "mime_type": mime_type,
                    "declared_size": size if isinstance(size, int) else 0,
                }
            )
        return tuple(candidates)

    def _materialize_attachments(
        self,
        *,
        update_id: int,
        message_id: int,
        metadata: tuple[dict[str, object], ...],
    ) -> tuple[Path, ...]:
        """Spool this message's images, once, and hand back their paths.

        Existence is the whole record. `download_file` writes a `.partial`,
        fsyncs it and `os.replace`s it into place, so the rename is the commit
        and a file that is there is a file that finished — the same primitive
        `json_store.py` and `release.py` already commit with.

        This used to be a two-file transaction: the image plus a JSON sidecar
        recording `file_id`, `mime_type`, `size` and `sha256`, re-verified by
        re-hashing the image on every redelivery, with three branches
        reconciling the pair (both present, image only, either present). None
        of those four fields had a reader — downstream takes the path and
        nothing else, and a provider is handed `{"type": "localImage", "path":
        ...}` — so the sidecar's only job was to distinguish a finished
        download from a truncated one. The rename already does that, and did
        before the sidecar was written.

        What the sidecar did hold, and a bare `<update>-<message>-<part>` name
        does not, is *which image this is*: if update ids ever restart, a stale
        file under the same name would be served silently. That is identity, so
        it goes in the identity — the pathname carries a digest of the
        `file_id`, and a collision cannot be written down.
        """
        if not metadata:
            return ()
        limit = self.config.media_max_mb * 1024 * 1024
        root = self._inbound_media_dir
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self._execution_broker.grant_read_access(root)
        results: list[Path] = []
        for index, item in enumerate(metadata):
            file_id = str(item["file_id"])
            if int(item.get("declared_size", 0)) > limit:
                raise InboundMediaRejected(
                    f"Telegram image exceeds configured {self.config.media_max_mb}MB cap"
                )
            identity = hashlib.sha256(file_id.encode()).hexdigest()[:16]
            target = (
                root
                / f"update-{update_id}-message-{message_id}-part-{index}-{identity}.image"
            )
            if target.is_symlink():
                raise RuntimeError(f"Inbound Telegram media spool contains a symlink: {target}")
            if not target.is_file():
                self.api.download_file(file_id, target, max_bytes=limit)
            elif target.stat().st_size > limit:
                # The cap was lowered since this arrived. The file is intact;
                # it is simply no longer allowed through.
                raise InboundMediaRejected(
                    f"spooled Telegram image exceeds configured "
                    f"{self.config.media_max_mb}MB cap: {target}"
                )
            self._execution_broker.grant_read_access(target)
            results.append(target)
        return tuple(results)

    def _send_piece(
        self, key: str, send: Callable[[], Any], *,
        max_wait_seconds: float = _MAX_RETRY_AFTER_WAIT,
    ) -> Any:
        def confirmed_send() -> Any:
            result = send()
            if key.startswith("text:") and (not isinstance(result, int) or result <= 0):
                raise TelegramAPIError("sendMessage returned no usable message id")
            return result

        if getattr(self._delivery_context, "path", None) is None:
            return self._send_with_retry(confirmed_send, max_wait_seconds=max_wait_seconds)
        pieces = self._delivery_context.receipt.setdefault("pieces", {})
        if key not in pieces:
            pieces[key] = self._send_with_retry(confirmed_send, max_wait_seconds=max_wait_seconds)
            self._save_receipt()
        return pieces[key]

    def send_result(self, chat_id: int, topic_id: int, text: str, source_key: str) -> None:
        """Use the same per-piece receipts for an unsolicited task outcome."""
        path = self._receipt_dir / "task-results" / f"{hashlib.sha256(source_key.encode()).hexdigest()}.json"
        previous = (
            getattr(self._delivery_context, "path", None),
            getattr(self._delivery_context, "receipt", {}),
        )
        self._delivery_context.path = path
        self._delivery_context.receipt = self._read_receipt(path)
        try:
            if not self._delivery_context.receipt.get("done"):
                self.send_reply(chat_id, topic_id, text)
                self._delivery_context.receipt["done"] = True
                self._save_receipt()
        finally:
            self._delivery_context.path, self._delivery_context.receipt = previous

    def send_reply(
        self, chat_id: int, topic_id: int, text: str, *,
        max_wait_seconds: float = _MAX_RETRY_AFTER_WAIT,
    ) -> None:
        """Send formatted text and extracted images to Telegram.

        Update replies retain each confirmed piece across retries and restart.
        Failed pieces raise so callers cannot mistake partial delivery for success.
        The poller uses a short rate-limit wait; an unfinished reply is queued
        for an executor to resume with its longer wait budget.
        """
        if getattr(self._delivery_context, "path", None) is not None:
            receipt = self._delivery_context.receipt
            receipt.setdefault("reply", [chat_id, topic_id, text])
            chat_id, topic_id, text = receipt["reply"]
            self._save_receipt()
        clean_text, actions = extract_telegram_action_markers(text)
        clean_text, artifacts = extract_artifact_markers(clean_text)
        chunks = format_markdown_chunks(clean_text)
        failures: list[str] = []
        artifact_failures: list[tuple[OutboundArtifact, str, Path | None]] = []
        sent_message_ids: list[int] = []
        allowed_actions = set(self.config.agent_actions)

        for action in actions:
            if chat_id != self.config.chat_id:
                failures.append("Telegram action destination is not the configured chat")
            elif action.kind.value not in allowed_actions:
                failures.append(f"Telegram action {action.kind.value!r} is not enabled")
            elif action.kind is TelegramActionKind.PIN_REPLY and not chunks:
                failures.append("pin_reply requires a non-empty text reply")
            elif (
                action.kind is TelegramActionKind.PIN_MESSAGE
                and action.message_id is not None
                and action.message_id > 2_147_483_647
            ):
                failures.append("pin_message message id exceeds Telegram's numeric range")
        if failures:
            raise TelegramDeliveryError("; ".join(failures))

        for index, chunk in enumerate(chunks):
            try:
                message_id = self._send_piece(
                    f"text:{index}", lambda c=chunk: self.api.send_message(
                        chat_id, c, topic_id=topic_id, parse_mode="HTML"
                    ),
                    max_wait_seconds=max_wait_seconds,
                )
                sent_message_ids.append(message_id)
                log.info(
                    "Telegram reply confirmed chat=%s topic=%s message=%s receipt=%s piece=%s",
                    chat_id, topic_id, message_id,
                    getattr(self._delivery_context, "path", None), index,
                )
            except TelegramAPIError as exc:
                log.error("Failed to send Telegram reply chunk after retries: %s", exc)
                failures.append(f"message chunk: {exc}")

        for index, artifact in enumerate(artifacts):
            if (getattr(self._delivery_context, "path", None) is not None
                    and f"artifact:{index}" in self._delivery_context.receipt.get("pieces", {})):
                continue
            try:
                path = self._resolve_delivery_artifact(artifact.path)
                size = path.stat().st_size
                limit = self.config.media_max_mb * 1024 * 1024
                if size > limit:
                    raise TelegramAPIError(
                        f"{path.name} is {size // (1024 * 1024)}MB; "
                        f"configured Telegram cap is {self.config.media_max_mb}MB"
                    )
                if artifact.kind is ArtifactKind.IMAGE:
                    send = lambda p=path: self.api.send_photo(
                        chat_id, p, topic_id=topic_id
                    )
                else:
                    send = lambda p=path: self.api.send_document(
                        chat_id, p, topic_id=topic_id
                    )
                self._send_piece(f"artifact:{index}", send, max_wait_seconds=max_wait_seconds)
            except (TelegramAPIError, OSError) as exc:
                label = artifact.kind.value
                log.error("Failed to send %s %s after retries: %s", label, artifact.path, exc)
                detail = f"{label} {artifact.path}: {exc}"
                failures.append(detail)
                record = self._quarantine(str(exc), chat_id=chat_id, topic_id=topic_id,
                                         kind=artifact.kind.value, path=artifact.path)
                artifact_failures.append((artifact, str(exc), record))

        for action in actions:
            if action.kind is TelegramActionKind.PIN_REPLY:
                if not sent_message_ids:
                    failures.append("pin_reply requires a successfully delivered text reply")
                    continue
                target_message_id = sent_message_ids[-1]
            else:
                target_message_id = action.message_id
            if target_message_id is None:  # pragma: no cover - parser invariant
                failures.append("Telegram pin action omitted its message id")
                continue
            try:
                self._send_piece(
                    f"pin:{target_message_id}", lambda message_id=target_message_id: self.api.pin_chat_message(
                        chat_id, message_id
                    ),
                    max_wait_seconds=max_wait_seconds,
                )
            except TelegramAPIError as exc:
                log.error("Failed to execute Telegram %s after retries: %s", action.kind, exc)
                failures.append(f"{action.kind.value}: {exc}")

        if artifact_failures:
            records = [record.name for _artifact, _error, record in artifact_failures if record]
            suffix = f" Quarantine: {', '.join(records)}." if records else ""
            notice = (
                f"⚠️ Couldn't deliver {len(artifact_failures)} attachment(s) to topic "
                f"{topic_id or 'general'}.{suffix}"
            )
            try:
                self._send_piece(
                    "attachment-alert", lambda: self.api.send_message(chat_id, notice, topic_id=None),
                    max_wait_seconds=max_wait_seconds,
                )
            except TelegramAPIError as exc:
                log.error("Failed to send thread-independent delivery alert: %s", exc)

        if failures:
            raise TelegramDeliveryError(
                f"{len(failures)} of {len(chunks) + len(artifacts)} reply piece(s) failed to "
                f"deliver: {'; '.join(failures)}"
            )

    def _drain_delivery_outbox(self) -> None:
        """Deliver fixed-destination jobs from the configured product spool.

        Jobs contain exactly one of ``text``, ``photo``, or ``document`` plus
        optional ``caption`` and ``thread_id``. The destination chat always
        comes from configuration, topics must be declared, and file paths must
        resolve beneath a configured delivery root.
        """
        assert self.config.delivery_outbox_dir is not None
        outbox = Path(self.config.delivery_outbox_dir)
        try:
            outbox.mkdir(parents=True, exist_ok=True)
            jobs = sorted(outbox.glob("*.json"))
        except OSError as exc:
            log.error("Could not inspect Telegram delivery outbox: %s", exc)
            return

        for job_path in jobs:
            try:
                if job_path.is_symlink():
                    raise ValueError("delivery job may not be a symbolic link")
                raw = json.loads(job_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("job must be a JSON object")
                if "chat_id" in raw:
                    raise ValueError("job may not override the configured chat_id")
                chat_id = self.config.chat_id
                try:
                    topic_id = int(raw.get("thread_id") or raw.get("topic_id") or 0)
                except TypeError as exc:
                    raise ValueError("job topic must be an integer") from exc
                allowed_topics = {0, *self.config.topics.values()}
                if topic_id not in allowed_topics:
                    raise ValueError(f"topic {topic_id} is not declared")
                caption = str(raw.get("caption") or "").strip() or None
                payloads = [name for name in ("text", "photo", "document") if raw.get(name)]
                if len(payloads) != 1:
                    raise ValueError("job must contain exactly one of text, photo, or document")

                kind = payloads[0]
                value = str(raw[kind])
                if kind != "text":
                    marker = "send_image" if kind == "photo" else "send_document"
                    value = f"{caption or ''} [[{marker}:{value}]]".strip()
            except (OSError, ValueError) as exc:
                log.error("Telegram delivery job %s is invalid: %s", job_path.name, exc)
                self._quarantine(str(exc), job=job_path)
                continue
            try:
                self.send_reply(chat_id, topic_id, value)
            except TelegramAPIError as exc:
                log.error("Telegram delivery job %s failed: %s", job_path.name, exc)
                self._quarantine(str(exc), job=job_path)
                continue
            job_path.unlink()

    def _resolve_delivery_artifact(self, raw_path: str) -> Path:
        try:
            artifact = Path(raw_path).resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise TelegramAPIError(f"could not resolve delivery artifact: {exc}") from exc
        if not artifact.is_file():
            raise TelegramAPIError(f"delivery artifact is not a file: {artifact}")
        roots = [Path(root).resolve() for root in self.config.delivery_roots]
        if not any(artifact.is_relative_to(root) for root in roots):
            raise TelegramAPIError("delivery artifact is outside configured delivery_roots")
        return artifact

    def _quarantine(self, error: str, *, job: Path | None = None, **facts: object) -> Path | None:
        """Record one failed delivery beside whatever it could not deliver.

        An outbox job moves into quarantine and gains a ``.failure.json``
        record; a failed reply artifact is only a record. Returns the parked
        job, or the record.
        """
        configured = self.config.delivery_quarantine_dir
        if configured is None:
            return None
        directory = Path(configured)
        stamp = datetime.now(timezone.utc)
        # Records stay readable by whoever inspects the quarantine.
        stem = f"{job.stem if job else stamp.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex}"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            parked = None
            if job is not None:
                parked = directory / f"{stem}.json"
                shutil.move(str(job), str(parked))
                facts["job"] = parked.name
            record = directory / (f"{stem}.failure.json" if parked else f"{stem}.json")
            write_receipt(record, {"version": 1, "failed_at": stamp.isoformat(),
                                   "error": error[:1000], **facts})
            record.chmod(0o644)
            return parked or record
        except OSError as exc:
            log.error("Could not quarantine Telegram delivery %s: %s", job or facts, exc)
            return None

    @staticmethod
    def _send_with_retry(
        send: Callable[[], Any], *, max_wait_seconds: float = _MAX_RETRY_AFTER_WAIT
    ) -> Any:
        for attempt in range(1, _SEND_ATTEMPTS + 1):
            try:
                return send()
            except TelegramAPIError as exc:
                if attempt == _SEND_ATTEMPTS:
                    raise
                # Wait what Telegram asked for when it said so. Retrying a 429
                # early is not a retry: it is another rate-limited request,
                # which extends the window it is waiting out.
                wait = exc.retry_after or _SEND_RETRY_BACKOFF_SECONDS
                if wait > max_wait_seconds:
                    # This caller cannot afford the wait Telegram is asking
                    # for. Giving up here is the whole point: the alternative
                    # is holding a thread that other work depends on.
                    raise
                time.sleep(wait)
