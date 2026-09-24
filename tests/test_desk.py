"""Filesystem desk ownership, recovery, and durable event mechanics."""

from __future__ import annotations

import json
from pathlib import Path

from steward_harness.desk import DeskEvents, DeskInbox


def _queue(tmp_path: Path, msg_id: str, text: str, topic: int | None = None) -> None:
    inbox = tmp_path / "desk-inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    job = {"kind": "message", "text": text, "origin": "web", "id": msg_id}
    if topic is not None:
        job["topic"] = topic
    (inbox / f"1.000000-{msg_id}.json").write_text(json.dumps(job))


def test_recover_requeues_orphaned_claims(tmp_path: Path) -> None:
    _queue(tmp_path, "orphan", "interrupted")
    inbox = DeskInbox(tmp_path / "desk-inbox")
    [queued] = inbox.pending()
    claimed = inbox.claim(queued)

    assert claimed.path.name == "1.000000-orphan.json.claimed"
    assert inbox.recover() == 1
    assert inbox.pending() == [queued]


def test_claim_preserves_desk_thread_identity(tmp_path: Path) -> None:
    _queue(tmp_path, "threaded", "hello", topic=42)
    inbox = DeskInbox(tmp_path / "desk-inbox")
    [message] = inbox.pending()

    assert inbox.claim(message).topic_id == 42


def test_requeue_returns_a_claim_to_pending(tmp_path: Path) -> None:
    _queue(tmp_path, "busy", "wait for the world")
    inbox = DeskInbox(tmp_path / "desk-inbox")
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

    assert DeskInbox(inbox_dir).pending() == []
    assert len(list(inbox_dir.glob("*.rejected"))) == 2


def test_events_writer_dedupe_scan(tmp_path: Path) -> None:
    events = DeskEvents(tmp_path / "desk" / "events.jsonl")
    assert not events.has_reply("m1")
    events.append("reply", "answer", "m1")
    assert events.has_reply("m1")
    assert not events.has_reply("m2")
    events.append("accepted", "question", "m2")
    assert not events.has_reply("m2")


def test_observations_keep_the_first_evidence_across_claim_recovery_and_completion(tmp_path):
    inbox = DeskInbox(tmp_path / 'inbox')
    assert inbox.observe('failure-1', 'first evidence')
    [message] = inbox.pending()
    assert message.observation
    assert not inbox.observe('failure-1', 'later measurement')
    assert inbox.claim(message).observation
    assert inbox.pending() == []
    assert not inbox.observe('failure-1', 'during native work')
    assert inbox.recover() == 1
    [recovered] = inbox.pending()
    assert recovered.text == 'first evidence'
    inbox.done(inbox.claim(recovered))
    assert not inbox.observe('failure-1', 'after completion')
    assert inbox.pending() == []
    assert (inbox.dir / 'failure-1.json.done').exists()
    assert not list(inbox.dir.glob('*.json'))


def test_observation_recovers_a_source_written_before_queue_publication(tmp_path):
    inbox = DeskInbox(tmp_path / 'inbox')
    inbox.observe('failure-1', 'first evidence')
    (inbox.dir / 'failure-1.json').unlink()  # crash before the queue link existed
    assert inbox.observe('failure-1', 'different later reading')
    [message] = inbox.pending()
    assert message.text == 'first evidence'
    inbox.park_failed(inbox.claim(message))
    assert not inbox.observe('failure-1', 'do not retry uncertain cognition')
    assert inbox.pending() == []


def test_a_message_may_ask_for_its_threads_quality(tmp_path: Path) -> None:
    inbox = DeskInbox(tmp_path)
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
    inbox = DeskInbox(tmp_path)
    for msg_id, context in (("framed", "Live call."), ("huge", "x" * 2001), ("typed", 7)):
        job = {"kind": "message", "text": "hi", "id": msg_id, "context": context}
        (tmp_path / f"{msg_id}.json").write_text(json.dumps(job))
    contexts = {m.msg_id: m.context for m in inbox.pending()}
    assert contexts == {"framed": "Live call.", "huge": None, "typed": None}
    assert inbox.claim(next(m for m in inbox.pending() if m.msg_id == "framed")).context == "Live call."
