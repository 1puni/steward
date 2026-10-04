"""Filesystem desk inbox and JSONL event log shared with the web sidecar."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_TEXT = 8000  # the sidecar enforces the same bound before queueing
_PROFILES = ("fast", "balanced", "deep")
_MAX_CONTEXT = 2000


@dataclass(frozen=True, slots=True)
class DeskMessage:
    """One accepted desk-inbox job."""

    path: Path
    msg_id: str
    text: str
    topic_id: int = 0  # an optional thread id: a new thread starts a new session
    # The quality the sender asks this thread to run at, as Telegram's /model
    # sets it. A phone call asks for "fast": the caller is waiting in silence.
    profile: str | None = None
    # How the sender frames every message on this thread, e.g. "this is a live
    # phone call". The model reads it; the world records only `text`.
    context: str | None = None


class DeskEvents:
    """Appends panel-compatible JSONL events; seq is the line number."""

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


class DeskInbox:
    """Claims atomic-rename inbox files the way the shell harnesses did.

    Ownership is the file's suffix: ``.json`` queued, ``.json.claimed`` being
    worked, ``.json.failed``/``.rejected`` parked for the operator. A restart returns orphaned claims to the queue; accepted native turns replay
    their receipt instead of repeating cognition.
    """

    def __init__(self, inbox_dir: str | Path) -> None:
        self.dir = Path(inbox_dir)

    def recover(self) -> int:
        """Requeue files left claimed by a dead process; returns the count."""
        if not self.dir.is_dir():
            return 0
        recovered = 0
        for claimed in sorted(self.dir.glob("*.claimed")):
            target = claimed.with_suffix("")
            claimed.replace(target)
            recovered += 1
        return recovered

    def pending(self) -> list[DeskMessage]:
        if not self.dir.is_dir():
            return []
        messages: list[DeskMessage] = []
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

    def claim(self, message: DeskMessage) -> DeskMessage:
        claimed = message.path.with_name(f"{message.path.name}.claimed")
        message.path.replace(claimed)
        return DeskMessage(
            path=claimed,
            msg_id=message.msg_id,
            text=message.text,
            topic_id=message.topic_id,
            profile=message.profile,
            context=message.context,
        )

    def done(self, message: DeskMessage) -> None:
        message.path.unlink(missing_ok=True)

    def requeue(self, message: DeskMessage) -> None:
        if message.path.suffix != ".claimed":
            raise ValueError("Only a claimed desk message can be requeued")
        message.path.replace(message.path.with_suffix(""))

    def park_failed(self, message: DeskMessage) -> None:
        self._park(message.path, "failed")

    def _park(self, path: Path, suffix: str) -> None:
        path.replace(path.with_suffix(f".{suffix}"))

    @staticmethod
    def _parse(path: Path) -> DeskMessage | None:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(job, dict) or job.get("kind") != "message":
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
        return DeskMessage(path=path, msg_id=msg_id, text=text, topic_id=topic,
                           profile=profile, context=context)
