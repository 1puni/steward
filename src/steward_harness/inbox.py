"""One durable inbox for every inbound message, and the one drain that answers it.

A message is a JSON file, and the file's suffix is its ownership: ``.json``
queued, ``.json.claimed`` being answered, ``.json.failed``/``.rejected`` parked
for the operator. A restart returns orphaned claims to the queue; an accepted
native turn replays its receipt instead of repeating cognition. A desk
observation also retains an immutable ``.source`` and ends as ``.json.done``.

The directory, not the record, names who is speaking. External clients (the web
bridge, the phone gateway, local agents) write the configured desk inbox, whose
permissions are their trust boundary. The Telegram ingress writes a private
inbox beside controller state after admitting the sender, so a desk client can
never speak as a Telegram operator. Each inbox has a `Source` that answers its
messages and replies through the transport they came from.

One drain serves every source: messages run in name order within a
conversation, different conversations run concurrently, and a deferral leaves
a message at the front of its conversation to be retried. Observations are
controller work, not speech, so the scheduler pass runs them instead.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from steward_harness.lease import Busy
from steward_harness.receipts import write_receipt
from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeUnavailable
from steward_harness.state import ConversationBusy
from steward_harness.world.turn_checkpoint import WorldContentConflict, WorldUpdatePending

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_TEXT = 8000  # the web sidecar enforces the same bound before queueing
_PROFILES = ("fast", "balanced", "deep")
_MAX_CONTEXT = 2000

#: The owner a message needs is occupied; retry it.
DEFERRALS: tuple[type[BaseException], ...] = (
    Busy, ConversationBusy, WorldUpdatePending, WorldContentConflict,
)
SCAN_SECONDS = 0.25
RETRY_SECONDS = 1.0
WORKERS = 10


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One message in an inbox; `record` is the whole file, delivery progress included."""

    path: Path
    msg_id: str
    text: str
    topic_id: int = 0  # the conversation within its source; a new thread starts a new session
    observation: bool = False
    # The quality the sender asks this thread to run at, as Telegram's /model
    # sets it. A phone call asks for "fast": the caller is waiting in silence.
    profile: str | None = None
    # How the sender frames every message on this thread, e.g. "this is a live
    # phone call". The model reads it; the world records only `text`.
    context: str | None = None
    record: dict[str, Any] = field(default_factory=dict, compare=False)


class EventLog:
    """The desk's reply channel: panel-compatible JSONL events; seq is the line number."""

    def __init__(self, events_file: str | Path) -> None:
        self.path = Path(events_file)

    def append(self, type_: str, text: str, msg_id: str = "") -> None:
        event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "type": type_, "text": text}
        if msg_id:
            event["id"] = msg_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def has_reply(self, msg_id: str) -> bool:
        """True if a reply event for this id is already on the log."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == msg_id and obj.get("type") == "reply":
                return True
        return False


class Inbox:
    """Claims atomic-rename message files in one directory."""

    def __init__(self, inbox_dir: str | Path) -> None:
        self.dir = Path(inbox_dir)

    def put(self, name: str, record: dict[str, Any]) -> InboundMessage:
        """Retain a record already claimed by its producer; `requeue` hands it on."""
        path = self.dir / f"{name}.json.claimed"
        write_receipt(path, record)
        message = self._parse(path)
        assert message is not None, "a producer wrote an invalid inbound record"
        return message

    def holds(self, name: str) -> bool:
        """Whether a message of this name is retained in any state."""
        return any(self.dir.glob(f"{name}.*"))

    def observe(self, msg_id: str, text: str) -> bool:
        """Retain one immutable source and queue it through the ordinary inbox.

        A .source file freezes the first evidence for replay. The existing
        claim/failure markers and an observation's .done marker own custody;
        none of these archived files participate in the pending .json scan.
        """
        if not _ID_RE.fullmatch(msg_id) or not 1 <= len(text) <= _MAX_TEXT:
            raise ValueError("invalid desk observation identity or text")
        self.dir.mkdir(parents=True, exist_ok=True)
        source = self.dir / f"{msg_id}.source"
        target = self.dir / f"{msg_id}.json"
        if not source.exists():
            with tempfile.NamedTemporaryFile(mode="w", dir=self.dir, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    json.dump({"kind": "observation", "id": msg_id, "text": text}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                    try:
                        os.link(temporary, source)
                    except FileExistsError:
                        pass
                finally:
                    temporary.unlink()
        if any(target.with_name(target.name + suffix).exists()
               for suffix in ("", ".claimed", ".done", ".failed")):
            return False
        try:
            os.link(source, target)
        except FileExistsError:
            return False
        directory = os.open(self.dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True

    def recover(self) -> int:
        """Requeue files left claimed by a dead process; returns the count."""
        if not self.dir.is_dir():
            return 0
        recovered = 0
        for claimed in sorted(self.dir.glob("*.claimed")):
            claimed.replace(claimed.with_suffix(""))
            recovered += 1
        return recovered

    def pending(self) -> list[InboundMessage]:
        if not self.dir.is_dir():
            return []
        messages: list[InboundMessage] = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                message = self._parse(path)
            except FileNotFoundError:
                # Ingress may claim this file between listing and reading.
                continue
            if message is None:
                try:
                    self._park(path, "rejected")
                except FileNotFoundError:
                    # Ingress may claim a file after another scanner lists it.
                    # Its claimed copy remains owned by that ingress worker.
                    pass
                continue
            messages.append(message)
        return messages

    def claim(self, message: InboundMessage) -> InboundMessage:
        claimed = message.path.with_name(f"{message.path.name}.claimed")
        message.path.replace(claimed)
        return InboundMessage(
            path=claimed,
            msg_id=message.msg_id,
            text=message.text,
            topic_id=message.topic_id,
            observation=message.observation,
            profile=message.profile,
            context=message.context,
            record=message.record,
        )

    def done(self, message: InboundMessage) -> None:
        if message.observation:
            message.path.replace(message.path.with_suffix(".done"))
        else:
            message.path.unlink(missing_ok=True)

    def requeue(self, message: InboundMessage) -> None:
        if message.path.suffix != ".claimed":
            raise ValueError("Only a claimed message can be requeued")
        message.path.replace(message.path.with_suffix(""))

    def park_failed(self, message: InboundMessage) -> None:
        self._park(message.path, "failed")

    def _park(self, path: Path, suffix: str) -> None:
        path.replace(path.with_suffix(f".{suffix}"))

    @staticmethod
    def _parse(path: Path) -> InboundMessage | None:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(job, dict) or job.get("kind") not in ("message", "observation"):
            return None
        text = job.get("text")
        msg_id = job.get("id")
        if not isinstance(text, str) or not 1 <= len(text) <= _MAX_TEXT:
            return None
        if not isinstance(msg_id, str) or not _ID_RE.match(msg_id):
            return None
        topic = job.get("topic", 0)
        if not isinstance(topic, int) or isinstance(topic, bool) or topic < 0:
            topic = 0
        profile = job.get("profile")
        if profile not in _PROFILES:
            profile = None
        context = job.get("context")
        if not isinstance(context, str) or not 1 <= len(context) <= _MAX_CONTEXT:
            context = None
        return InboundMessage(path=path, msg_id=msg_id, text=text, topic_id=topic,
                              observation=job["kind"] == "observation", profile=profile,
                              context=context, record=job)


@dataclass(frozen=True, slots=True)
class Source:
    """An inbox and how its messages are answered.

    `answer` runs one claimed message and replies through its transport; it
    returns once the reply is delivered. `defers` says which of its errors mean
    "not now" rather than "never".
    """

    inbox: Inbox
    answer: Callable[[InboundMessage], None]
    defers: Callable[[BaseException], bool] = lambda error: isinstance(error, DEFERRALS)


def settle(source: Source, message: InboundMessage) -> bool:
    """Answer one claimed message and dispose of it; False means it was requeued."""
    try:
        source.answer(message)
    except Exception as error:
        if source.defers(error):
            log.info("inbound message %s deferred: %s", message.msg_id, error)
            source.inbox.requeue(message)
            return False
        # A provider refusing one turn (a usage limit, a failed run) ends that
        # message, not the controller. Anything else is a defect worth a trace.
        log.warning("inbound message %s failed: %s", message.msg_id, error,
                    exc_info=not isinstance(error, (RuntimeExecutionError, RuntimeUnavailable)))
        source.inbox.park_failed(message)
        return True
    source.inbox.done(message)
    return True


class InboxDrain:
    """Answer every source's messages: in order per conversation, concurrently across them.

    Inbound conversations run here rather than on the background executor, so a
    full `controller.workers` budget never keeps an operator waiting. Pause does
    not apply: it stops the steward taking on work, and an operator talking to
    their steward, through any source, is answered. A desk observation is work
    the steward takes on, so the pause-filtered scheduler pass runs it.
    """

    def __init__(self, sources: Sequence[Source], *, workers: int = WORKERS) -> None:
        self.sources = tuple(sources)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._in_flight: set[tuple[int, int]] = set()
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="inbox")
        self._thread = threading.Thread(target=self._run, name="inbox", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def request_stop(self) -> None:
        """Claim nothing new; messages already being answered finish."""
        self._stop.set()

    def stop(self) -> None:
        self.request_stop()
        if self._thread.ident is not None and self._thread is not threading.current_thread():
            self._thread.join()
        self._executor.shutdown(wait=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            for index, source in enumerate(self.sources):
                for message in source.inbox.pending():
                    if self._stop.is_set():
                        return
                    if message.observation:
                        continue
                    key = (index, message.topic_id)
                    with self._lock:
                        if key in self._in_flight:
                            continue  # its conversation is busy, or an earlier message is
                        self._in_flight.add(key)
                    try:
                        claimed = source.inbox.claim(message)
                    except FileNotFoundError:
                        self._release(key)
                        continue
                    self._executor.submit(self._answer, source, claimed, key)
            self._stop.wait(SCAN_SECONDS)

    def _answer(self, source: Source, message: InboundMessage, key: tuple[int, int]) -> None:
        try:
            if not settle(source, message) and not self._stop.is_set():
                # Hold the conversation while waiting, so the retried message
                # stays ahead of everything behind it.
                self._stop.wait(RETRY_SECONDS)
        except Exception:
            log.exception("inbound message %s could not be disposed of", message.msg_id)
        finally:
            self._release(key)

    def _release(self, key: tuple[int, int]) -> None:
        with self._lock:
            self._in_flight.discard(key)


__all__ = [
    "DEFERRALS", "EventLog", "Inbox", "InboundMessage", "InboxDrain", "Source", "settle",
]
