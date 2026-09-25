"""A world rhythm is an ordinary world turn, once per interval, reported to its owner."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from steward_harness.config.schema import StewardConfig
from steward_harness.daemon import StewardDaemon
from steward_harness.procedures import Procedures
from steward_harness.runtime.contracts import RuntimeExecutionError
from steward_harness.state import ConversationId
from test_world_durability import Crash, EditingCognition, runtime

DAY = 86400
NOW = DAY * 20 + 3600


def _config(root, world_root):
    instructions = root / "sleep.md"
    instructions.write_text("Consolidate the world. Do not commit; the harness accepts the world.\n")
    workdir = root / "work"
    workdir.mkdir(exist_ok=True)
    return StewardConfig.model_validate({
        "identity": {"name": "test", "slug": "test"},
        "provider": {"state_db": str(root / "state.db"), "workdir": str(workdir),
                     "fallback_families": []},
        "world": {"root": str(world_root)},
        "telegram": {"chat_id": 1, "allowed_users": [7], "topics": {"operator": 3}},
        "procedures": {"sleep": {"instructions": str(instructions), "provider": "codex",
                                 "model": {"model": "night-model", "effort": "high"},
                                 "access": "workspace-write"}},
        "rhythms": {"sleep": {"schedule": DAY, "procedure": "sleep", "input": "world",
                              "owner": "telegram:3"}},
    })


def _rhythm(tmp_path, cognition=None):
    state, checkpoint, service, cognition = runtime(tmp_path, cognition)
    config = _config(tmp_path, checkpoint.world.root)
    return config, state, checkpoint, service, cognition, Procedures(config, state, {})


def test_world_rhythm_runs_once_per_interval_as_a_world_turn(tmp_path):
    seen = []
    config, state, checkpoint, service, cognition, procedures = _rhythm(
        tmp_path, EditingCognition(before_return=seen.append))
    due = list(procedures.due_world_rhythms(now=NOW))
    assert due == [("sleep", "rhythm:sleep:20")]
    procedures.run_world_rhythm(service, *due[0])

    # An ordinary accepted world turn, with the procedure's model and no fallback.
    assert (checkpoint.world.root / "decision.md").exists()
    request = seen[0]
    assert request.model.model == "night-model" and request.provider_order == ("codex",)
    assert request.sandbox_mode == "workspace-write"
    assert "Consolidate the world." in request.prompt and "TASK_PROPOSAL" not in request.prompt
    # A rhythm owns no transport, so its turn cannot admit the task it proposed.
    assert state.tasks.all() == []
    receipt = state.result_receipt("rhythm:sleep:20")
    assert receipt["owner"] == "telegram:3" and not receipt["done"]
    assert receipt["reply"].startswith("Investigation saved.")
    assert "cannot admit" in receipt["reply"]

    assert list(procedures.due_world_rhythms(now=NOW + 600)) == []
    # A restarted controller reads the same settled interval.
    state, checkpoint, service, cognition = runtime(tmp_path, cognition)
    restarted = Procedures(config, state, {})
    assert list(restarted.due_world_rhythms(now=NOW + 600)) == []
    assert list(restarted.due_world_rhythms(now=NOW + DAY)) == [("sleep", "rhythm:sleep:21")]
    assert cognition.calls == 1
    # The world carries the night's work; the next interval does not resume its session.
    assert state.lineage(ConversationId("rhythm:sleep")).provider_session_id == "session"
    restarted.run_world_rhythm(service, "sleep", "rhythm:sleep:21")
    assert cognition.calls == 2 and seen[-1].provider_session_id is None


def test_crash_after_acceptance_replays_the_turn_without_repeating_cognition(tmp_path, monkeypatch):
    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path)

    def crash(_receipt):
        raise Crash

    monkeypatch.setattr(state, "save_result_receipt", crash)
    with pytest.raises(Crash):
        procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    monkeypatch.undo()
    state, checkpoint, service, _ = runtime(tmp_path, cognition)
    procedures = Procedures(config, state, {})
    assert list(procedures.due_world_rhythms(now=NOW)) == [("sleep", "rhythm:sleep:20")]
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert cognition.calls == 1
    assert state.result_receipt("rhythm:sleep:20")["reply"].startswith("Investigation saved.")


def test_failed_interval_is_consumed_rather_than_retried(tmp_path):
    class Failing(EditingCognition):
        def run(self, request, *, execution_id=None):
            self.calls += 1
            request()
            raise RuntimeExecutionError("provider failed")

    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path, Failing())
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert list(procedures.due_world_rhythms(now=NOW)) == []
    assert list(procedures.due_world_rhythms(now=NOW + DAY)) == [("sleep", "rhythm:sleep:21")]
    assert not (checkpoint.world.root / "decision.md").exists()


def test_silent_world_rhythm_settles_without_a_delivery(tmp_path):
    class Silent(EditingCognition):
        def run(self, request, *, execution_id=None):
            return replace(super().run(request, execution_id=execution_id), output="")

    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path, Silent())
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert (checkpoint.world.root / "decision.md").exists()
    assert state.result_receipt("rhythm:sleep:20")["done"]
    assert state.pending_result_receipts() == []
    assert list(procedures.due_world_rhythms(now=NOW)) == []


def test_pass_runs_the_rhythm_and_delivers_its_reply_to_the_owner_topic(tmp_path):
    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path)
    daemon = StewardDaemon(config, tmp_path / "steward.yaml")
    daemon._procedures = procedures
    sent = []
    daemon._telegram = SimpleNamespace(config=config.telegram,
                                       send_result=lambda *args: sent.append(args))
    queued = []
    kernel = SimpleNamespace(
        dispatch=SimpleNamespace(submit=lambda key, work: queued.append((key, work)),
                                 reap=lambda: None),
        owners=lambda: (), tasks=SimpleNamespace(flush_inputs=lambda: None),
    )
    step = daemon._pass(state, service, kernel, SimpleNamespace(), None)
    for _ in range(3):
        queued.clear()
        step()
        for key, work in queued:
            if key[0] in {"rhythm", "rhythms", "result"}:
                work()
    assert cognition.calls == 1
    assert state.tasks.all() == []
    assert len(sent) == 1
    chat, topic, text, source = sent[0]
    assert (chat, topic) == (1, 3) and source.startswith("rhythm:sleep:")
    assert text.startswith("Investigation saved.")
    assert state.lineage(ConversationId("rhythm:sleep")).provider == "codex"


def test_paused_controller_does_not_start_a_world_rhythm(tmp_path):
    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path)
    daemon = StewardDaemon(config, tmp_path / "steward.yaml")
    daemon._procedures = procedures
    queued = []
    kernel = SimpleNamespace(
        dispatch=SimpleNamespace(submit=lambda key, work: queued.append((key, work)),
                                 reap=lambda: None),
        owners=lambda: (), tasks=SimpleNamespace(flush_inputs=lambda: None),
    )
    state.set_paused(True)
    daemon._pass(state, service, kernel, SimpleNamespace(), None)()
    assert not [key for key, _ in queued if key[0] in {"rhythm", "rhythms"}]


def test_rhythm_command_reports_the_interval_and_refuses_an_extra_run(tmp_path):
    from typing import cast

    from steward_harness.conversations import ConversationService
    from steward_harness.daemon import KernelCommands
    from steward_harness.repository_reconciler import RepositoryReconciler
    from steward_harness.runtime.execution import UntrustedExecutionBroker
    from steward_harness.config.schema import UntrustedExecutionConfig

    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path)
    commands = KernelCommands(
        config, state, cast(ConversationService, SimpleNamespace()),
        cast(RepositoryReconciler, SimpleNamespace()), {},
        UntrustedExecutionBroker(UntrustedExecutionConfig()), procedures=procedures,
    )
    assert "sleep: every 86400s, sleep, world, owner=telegram:3; this interval: not run yet" in (
        commands("rhythm", "list", 1, 3, 7))
    assert "world rhythm" in commands("rhythm", "run sleep", 1, 3, 7)
    assert state.tasks.all() == []
