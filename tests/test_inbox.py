"""Inbox file ownership, recovery, the shared drain, and the desk event log."""

from __future__ import annotations

import json
import threading

import pytest
from pathlib import Path

from steward_harness import inbox as inbox_module
from steward_harness.inbox import EventLog, Inbox, InboundMessage, InboxDrain, Source
from steward_harness.runtime.contracts import NativeStorageDeferred, RuntimeExecutionError
from steward_harness.state import ConversationBusy
from steward_harness.world.turn_checkpoint import WorldUpdatePending


def _queue(tmp_path: Path, msg_id: str, text: str, topic: int | None = None) -> None:
    inbox = tmp_path / "desk-inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    job = {"kind": "message", "text": text, "origin": "web", "id": msg_id}
    if topic is not None:
        job["topic"] = topic
    (inbox / f"1.000000-{msg_id}.json").write_text(json.dumps(job))


def test_recover_requeues_orphaned_claims(tmp_path: Path) -> None:
    _queue(tmp_path, "orphan", "interrupted")
    inbox = Inbox(tmp_path / "desk-inbox")
    [queued] = inbox.pending()
    claimed = inbox.claim(queued)

    assert claimed.path.name == "1.000000-orphan.json.claimed"
    assert inbox.recover() == 1
    assert inbox.pending() == [queued]


def test_claim_preserves_desk_thread_identity(tmp_path: Path) -> None:
    _queue(tmp_path, "threaded", "hello", topic=42)
    inbox = Inbox(tmp_path / "desk-inbox")
    [message] = inbox.pending()

    assert inbox.claim(message).topic_id == 42


def test_requeue_returns_a_claim_to_pending(tmp_path: Path) -> None:
    _queue(tmp_path, "busy", "wait for the world")
    inbox = Inbox(tmp_path / "desk-inbox")
    [queued] = inbox.pending()

    inbox.requeue(inbox.claim(queued))

    assert inbox.pending() == [queued]


def test_invalid_jobs_are_rejected_not_processed(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "desk-inbox"
    inbox_dir.mkdir(parents=True)
    (inbox_dir / "1.0-notmsg.json").write_text(
        json.dumps({"kind": "spam", "text": "x", "id": "notmsg"})
    )
    (inbox_dir / "2.0-traversal.json").write_text("not json at all")

    assert Inbox(inbox_dir).pending() == []
    assert len(list(inbox_dir.glob("*.rejected"))) == 2


def test_events_writer_dedupe_scan(tmp_path: Path) -> None:
    events = EventLog(tmp_path / "desk" / "events.jsonl")
    assert not events.has_reply("m1")
    events.append("reply", "answer", "m1")
    assert events.has_reply("m1")
    assert not events.has_reply("m2")
    events.append("accepted", "question", "m2")
    assert not events.has_reply("m2")


def test_a_message_may_ask_for_its_threads_quality(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path)
    for msg_id, profile in (("fast-one", "fast"), ("odd-one", "turbo"), ("plain-one", None)):
        job = {"kind": "message", "text": "hi", "id": msg_id, "topic": 1}
        if profile is not None:
            job["profile"] = profile
        (tmp_path / f"{msg_id}.json").write_text(json.dumps(job))
    profiles = {m.msg_id: m.profile for m in inbox.pending()}
    assert profiles == {"fast-one": "fast", "odd-one": None, "plain-one": None}
    claimed = inbox.claim(next(m for m in inbox.pending() if m.msg_id == "fast-one"))
    assert claimed.profile == "fast"


def test_a_message_may_carry_bounded_framing_for_the_model(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path)
    for msg_id, context in (("framed", "Live call."), ("huge", "x" * 2001), ("typed", 7)):
        job = {"kind": "message", "text": "hi", "id": msg_id, "context": context}
        (tmp_path / f"{msg_id}.json").write_text(json.dumps(job))
    contexts = {m.msg_id: m.context for m in inbox.pending()}
    assert contexts == {"framed": "Live call.", "huge": None, "typed": None}
    assert inbox.claim(next(m for m in inbox.pending() if m.msg_id == "framed")).context == "Live call."


def test_pending_tolerates_ingress_claim_between_listing_and_read(tmp_path, monkeypatch):
    _queue(tmp_path, 'racing', 'preserve this message')
    inbox = Inbox(tmp_path / 'desk-inbox')
    [queued] = inbox.pending()
    parse = inbox._parse
    claimed = []

    def claim_before_read(path):
        claimed.append(inbox.claim(queued))
        return parse(path)

    monkeypatch.setattr(inbox, '_parse', claim_before_read)
    assert inbox.pending() == []
    assert len(claimed) == 1
    assert parse(claimed[0].path).text == 'preserve this message'
    assert not list(inbox.dir.glob('*.rejected'))


def test_pending_read_error_does_not_reject_valid_job(tmp_path: Path, monkeypatch) -> None:
    _queue(tmp_path, "unreadable", "preserve on read failure")
    inbox = Inbox(tmp_path / "desk-inbox")
    [queued] = inbox.pending()
    read = Path.read_text

    def fail_read(path, *args, **kwargs):
        if path == queued.path:
            raise PermissionError("fixture transient read failure")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(PermissionError, match="fixture transient"):
        inbox.pending()
    assert queued.path.exists()
    assert not list(inbox.dir.glob("*.rejected"))


def test_pending_rejects_invalid_utf8(tmp_path: Path) -> None:
    inbox = Inbox(tmp_path)
    (tmp_path / "invalid.json").write_bytes(b"\xff")
    assert inbox.pending() == []
    assert (tmp_path / "invalid.rejected").read_bytes() == b"\xff"


def _job(directory: Path, name: str, topic: int, **fields) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(
        {"kind": "message", "id": name, "text": f"message {name}", "topic": topic, **fields}))


def _run(sources, until: threading.Event) -> None:
    drain = InboxDrain(sources)
    drain.start()
    try:
        assert until.wait(5), "the drain did not answer"
    finally:
        drain.stop()


@pytest.mark.parametrize("deferral", [ConversationBusy, NativeStorageDeferred, WorldUpdatePending])
def test_a_deferred_message_keeps_its_place_in_its_conversation(tmp_path, monkeypatch, deferral):
    monkeypatch.setattr(inbox_module, "RETRY_SECONDS", 0.01)
    for name, topic in (("1-first", 5), ("2-second", 5), ("3-elsewhere", 6)):
        _job(tmp_path, name, topic)
    answered: list[str] = []
    deferred: list[str] = []
    finished = threading.Event()

    def answer(message: InboundMessage) -> None:
        if message.msg_id == "1-first" and not deferred:
            deferred.append(message.msg_id)
            raise deferral("owner busy")
        answered.append(message.msg_id)
        if len(answered) == 3:
            finished.set()

    _run([Source(Inbox(tmp_path), answer)], finished)
    assert deferred == ["1-first"]
    assert answered.index("1-first") < answered.index("2-second")
    assert not list(tmp_path.glob("*.json*"))


def test_conversations_and_sources_are_answered_concurrently(tmp_path):
    _job(tmp_path / "telegram", "1-telegram", 7)
    _job(tmp_path / "desk", "1-desk", 7)
    _job(tmp_path / "desk", "2-desk", 8)
    together = threading.Barrier(3, timeout=5)
    finished = threading.Event()
    answered: list[str] = []

    def answer(message: InboundMessage) -> None:
        together.wait()  # all three conversations are running at once
        answered.append(message.msg_id)
        if len(answered) == 3:
            finished.set()

    _run([Source(Inbox(tmp_path / "telegram"), answer), Source(Inbox(tmp_path / "desk"), answer)],
         finished)
    assert sorted(answered) == ["1-desk", "1-telegram", "2-desk"]


def test_a_failed_message_is_parked_and_does_not_block_its_conversation(tmp_path):
    _job(tmp_path, "1-broken", 5)
    _job(tmp_path, "2-fine", 5)
    finished = threading.Event()

    def answer(message: InboundMessage) -> None:
        if message.msg_id == "1-broken":
            raise RuntimeExecutionError("provider refused this turn")
        finished.set()

    _run([Source(Inbox(tmp_path), answer)], finished)
    assert [path.name for path in tmp_path.iterdir()] == ["1-broken.json.failed"]


def test_a_producer_claims_its_record_until_it_hands_it_on(tmp_path):
    inbox = Inbox(tmp_path)
    message = inbox.put("000000000042", {"kind": "message", "id": "tg_42", "text": "hi", "topic": 3,
                                         "sender": 9})
    assert message.path.name == "000000000042.json.claimed"
    assert inbox.holds("000000000042") and not inbox.holds("000000000043")
    assert inbox.pending() == []
    inbox.requeue(message)
    [queued] = inbox.pending()
    assert (queued.msg_id, queued.topic_id, queued.record["sender"]) == ("tg_42", 3, 9)
