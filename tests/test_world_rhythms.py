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

    # An ordinary accepted world turn, with the procedure's model (no fallback is configured).
    assert (checkpoint.world.root / "decision.md").exists()
    request = seen[0]
    assert request.model.model == "night-model" and request.provider_order == ("codex",)
    assert request.sandbox_mode == "workspace-write"
    assert "Consolidate the world." in request.prompt and "TASK_PROPOSAL" not in request.prompt
    # It is told that nothing is sent unless it asks.
    assert request.prompt.count('operation="notify"') == 1
    assert request.prompt.count("Final replies from automatic runs are recorded only.") == 1
    # History is the world's Git, not a section of its files.
    assert "The world is Git." in request.prompt and "delete freely" in request.prompt
    # A rhythm receives a notification capability; it cannot admit tasks.
    assert request.task_call_socket is not None
    assert state.tasks.all() == []
    # Its reply asked to notify no one: recorded, not sent.
    receipt = state.result_receipt("rhythm:sleep:20")
    assert receipt["owner"] == "telegram:3" and receipt["done"] and receipt["recorded_only"]
    assert receipt["reply"] == "" and receipt["result_text"].startswith("Investigation saved.")

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
    assert state.result_receipt("rhythm:sleep:20")["result_text"].startswith("Investigation saved.")


def test_failed_interval_stays_held_rather_than_retried(tmp_path):
    class Failing(EditingCognition):
        def run(self, request, *, execution_id=None):
            self.calls += 1
            request()
            raise RuntimeExecutionError("provider failed")

    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path, Failing())
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert list(procedures.due_world_rhythms(now=NOW)) == []
    assert list(procedures.due_world_rhythms(now=NOW + DAY)) == []
    assert procedures.world_rhythm_observations(now=NOW + DAY)["sleep"]["progress"] == "held"
    assert not (checkpoint.world.root / "decision.md").exists()


def _replying(output, *, write=None, notify=None):
    def edit(request):
        if notify:
            import os
            from test_task_calls import call
            request.on_process_started(os.getpid(), None)
            receipt = call(request.task_call_socket, operation="notify", key="notice", text=notify)
            assert receipt["accepted"]
        for path, text in (write or {}).items():
            (request.cwd / path).write_text(text)

    class Replying(EditingCognition):
        def run(self, request, *, execution_id=None):
            return replace(super().run(request, execution_id=execution_id), output=output)
    return Replying(before_return=edit)


# Every reply here reached gg's World topic under the old rule, which sent any
# reply that was not blank: the word joiner and the parenthetical before
# ac3a18b9, and "SILENT", "(empty)", "<br>" and the staging sentence after it.
@pytest.mark.parametrize("output", [
    "", " \n", "\u2060", "\u200b\ufeff ", "SILENT", "(empty)", "<br>",
    "*(Empty reply \u2014 the brief carries the morning.)*",
    "Staging pass complete. No new episodes since the last deep sleep.",
    "NOTIFY: NONE", "NOTIFY:\n\u2060",
])
def test_a_world_rhythm_that_does_not_ask_to_notify_sends_nothing(tmp_path, output):
    import time
    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path, _replying(output))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert (checkpoint.world.root / "decision.md").exists()
    receipt = state.result_receipt("rhythm:sleep:20")
    assert receipt["done"] and receipt["reply"] == ""
    assert state.pending_result_receipts() == []
    assert list(procedures.due_world_rhythms(now=NOW)) == []
    # Silence is recorded where it can be counted, never lost.
    recorded = bool(output.strip())
    assert receipt["recorded_only"] is recorded
    assert state.recorded_not_sent(time.time() - 60) == (["rhythm:sleep:20"] if recorded else [])


@pytest.mark.parametrize("output,message", [
    ("Filed the night.\nNOTIFY: The calendar write failed again.", "The calendar write failed again."),
    ("**NOTIFY:** Sleep did not complete.\nREM wrote no brief.", "Sleep did not complete.\nREM wrote no brief."),
    ("- notify: One thing needs you.", "One thing needs you."),
])
def test_a_world_rhythm_does_not_interpret_notification_markers(tmp_path, output, message):
    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path, _replying(output))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    receipt = state.result_receipt("rhythm:sleep:20")
    assert receipt["reply"] == "" and receipt["done"] and receipt["recorded_only"]
    assert receipt["result_text"] == output.strip()


def test_writing_a_dated_brief_does_not_send_it(tmp_path):
    brief = "2026-10-03\nGood morning, V.\n" + "x" * 12050
    config, state, checkpoint, service, cognition, procedures = _rhythm(
        tmp_path, _replying("Brief written.", write={"morning_brief.md": brief}))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert (checkpoint.world.root / "morning_brief.md").read_text() == brief
    assert state.result_receipt("rhythm:sleep:20")["reply"] == ""
    assert not state.pending_result_receipts()


def test_removed_deliver_key_is_not_silently_accepted(tmp_path):
    (tmp_path / "world").mkdir()
    data = _config(tmp_path, tmp_path / "world").model_dump(mode="json")
    data["rhythms"]["sleep"]["deliver"] = "morning_brief.md"
    with pytest.raises(ValueError, match="Extra inputs"):
        StewardConfig.model_validate(data)


def test_shipped_1puni_configuration_has_no_retired_delivery_key():
    from pathlib import Path
    import yaml
    config = StewardConfig.model_validate(yaml.safe_load(
        Path("instances/1puni/steward.yaml").read_text()))
    assert config.rhythms["rem"].after == "sleep"
    assert all("deliver" not in rhythm.model_dump() for rhythm in config.rhythms.values())


def test_pass_runs_the_rhythm_and_delivers_its_reply_to_the_owner_topic(tmp_path):
    config, state, checkpoint, service, cognition, procedures = _rhythm(
        tmp_path, _replying("Investigation saved.", notify="Investigation saved for the morning."))
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
    assert (chat, topic) == (1, 3) and source.startswith("notify:")
    assert text == "Investigation saved for the morning."
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


def test_rhythm_command_reports_the_obligation_and_refuses_an_extra_run(tmp_path):
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
    assert "no interrupted obligation" in commands("rhythm", "run sleep", 1, 3, 7)
    assert state.tasks.all() == []


def _night(root, world_root, rhythms, **procedure):
    """Sleep, then REM, then Dream Away: a chain of world rhythms."""
    config = _config(root, world_root).model_dump()
    config["procedures"]["sleep"] |= procedure
    config["rhythms"] = rhythms
    return StewardConfig.model_validate(config)


CHAIN = {
    "sleep": {"schedule": DAY, "procedure": "sleep", "input": "world", "owner": "telegram:3"},
    "rem": {"after": "sleep", "procedure": "sleep", "input": "world", "owner": None},
    "dream-away": {"after": "rem", "procedure": "sleep", "input": "world", "owner": None},
}


def _chain(tmp_path, cognition=None, rhythms=CHAIN):
    state, checkpoint, service, cognition = runtime(tmp_path, cognition)
    config = _night(tmp_path, checkpoint.world.root, rhythms)
    return config, state, checkpoint, service, cognition, Procedures(
        config, state, {}, world=checkpoint.world)


def test_the_pass_starts_one_world_rhythm_at_a_time_in_configured_order(tmp_path):
    # Staging and REM both came due the moment Sleep finished, and started in
    # the same second. They are one owner now: REM runs, then Staging.
    staging = {"schedule": 3600, "procedure": "sleep", "input": "world", "owner": None}
    rhythms = {"sleep": CHAIN["sleep"] | {"owner": None}, "rem": CHAIN["rem"], "staging": staging}
    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path, rhythms=rhythms)
    daemon = StewardDaemon(config, tmp_path / "steward.yaml")
    daemon._procedures = procedures
    ran = []
    run = procedures.run_world_rhythm
    procedures.run_world_rhythm = lambda service, name, key: (ran.append(name), run(service, name, key))
    queued = []
    kernel = SimpleNamespace(
        dispatch=SimpleNamespace(submit=lambda key, work: queued.append((key, work)),
                                 reap=lambda: None),
        owners=lambda: (), tasks=SimpleNamespace(flush_inputs=lambda: None),
    )
    step = daemon._pass(state, service, kernel, SimpleNamespace(), None)
    for _ in range(4):
        queued.clear()
        step()
        world = [work for key, work in queued if key[0] == "rhythm"]
        assert [key for key, _ in queued if key[0] == "rhythm"] == ([("rhythm", "world")] if world else [])
        for work in world:
            work()
    assert ran == ["sleep", "rem", "staging"]
    assert cognition.calls == 3


def test_a_chain_runs_each_rhythm_after_its_predecessor_completes(tmp_path):
    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path)
    ran = []
    for _ in range(4):
        for name, key in list(procedures.due_world_rhythms(now=NOW)):
            ran.append(key)
            procedures.run_world_rhythm(service, name, key)
    # Each dependent keys on its predecessor's interval, and one poll never
    # starts a dependent whose predecessor has not yet been accepted.
    assert ran == ["rhythm:sleep:20", "rhythm:rem:20", "rhythm:dream-away:20"]
    assert cognition.calls == 3

    # A restarted controller reads the same settled chain and repeats nothing.
    state, checkpoint, service, cognition = runtime(tmp_path, cognition)
    restarted = Procedures(config, state, {}, world=checkpoint.world)
    assert list(restarted.due_world_rhythms(now=NOW + 600)) == []
    assert list(restarted.due_world_rhythms(now=NOW + DAY)) == [("sleep", "rhythm:sleep:21")]
    assert cognition.calls == 3


def test_a_dependent_waits_for_an_accepted_predecessor(tmp_path):
    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path)
    # Started but not accepted: the dependent has nothing to follow yet.
    state.open_conversation(ConversationId("rhythm:sleep"), provider="codex", profile="balanced")
    state.start_turn(ConversationId("rhythm:sleep"), "rhythm:sleep:20", "harness:rhythm", "Sleep.")
    assert list(procedures.due_world_rhythms(now=NOW)) == []


def test_a_failed_predecessor_holds_the_chain_until_explicit_continuation(tmp_path):
    class Failing(EditingCognition):
        def run(self, request, *, execution_id=None):
            self.calls += 1
            request()
            raise RuntimeExecutionError("provider failed")

    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path, Failing())
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert list(procedures.due_world_rhythms(now=NOW)) == []
    assert list(procedures.due_world_rhythms(now=NOW + DAY)) == []
    from test_world_durability import runtime
    state, checkpoint, service, success = runtime(tmp_path)
    procedures = Procedures(config, state, {}, world=checkpoint.world)
    procedures.continue_world_rhythm("sleep", now=NOW + DAY)
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    # The old interval's dependent wakes ahead of today's root interval.
    assert list(procedures.due_world_rhythms(now=NOW + DAY)) == [
        ("rem", "rhythm:rem:20"), ("sleep", "rhythm:sleep:21")]
    procedures.run_world_rhythm(service, "rem", "rhythm:rem:20")
    assert next(procedures.due_world_rhythms(now=NOW + DAY)) == ("dream-away", "rhythm:dream-away:20")
    procedures.run_world_rhythm(service, "dream-away", "rhythm:dream-away:20")
    assert success.calls == 3
    assert cognition.calls == 1


def test_a_later_accepted_interval_settles_an_older_interrupted_one(tmp_path):
    # gg's inbox and night chain stopped on interruptions from days earlier,
    # although later accepted runs had already read past them.
    class Failing(EditingCognition):
        def run(self, request, *, execution_id=None):
            self.calls += 1
            request()
            raise RuntimeExecutionError("provider failed")

    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path)
    for name in CHAIN:
        procedures.run_world_rhythm(service, name, f"rhythm:{name}:21")
    state, checkpoint, service, failing = runtime(tmp_path, Failing())
    procedures = Procedures(config, state, {}, world=checkpoint.world)
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert failing.calls == 1

    # Neither the interrupted night nor its dependents hold today's chain.
    state, checkpoint, service, success = runtime(tmp_path)
    procedures = Procedures(config, state, {}, world=checkpoint.world)
    assert list(procedures.due_world_rhythms(now=NOW + 2 * DAY)) == [("sleep", "rhythm:sleep:22")]
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:22")
    assert list(procedures.due_world_rhythms(now=NOW + 2 * DAY)) == [("rem", "rhythm:rem:22")]
    assert success.calls == 1


def test_a_dependent_accepted_before_its_receipt_replays_after_restart(tmp_path, monkeypatch):
    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path)
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")

    def crash(_receipt):
        raise Crash

    monkeypatch.setattr(state, "save_result_receipt", crash)
    with pytest.raises(Crash):
        procedures.run_world_rhythm(service, "rem", "rhythm:rem:20")
    monkeypatch.undo()
    state, checkpoint, service, _ = runtime(tmp_path, cognition)
    procedures = Procedures(config, state, {}, world=checkpoint.world)
    # The accepted turn, not its receipt, is what the next rhythm follows.
    assert list(procedures.due_world_rhythms(now=NOW)) == [
        ("rem", "rhythm:rem:20"), ("dream-away", "rhythm:dream-away:20")]
    procedures.run_world_rhythm(service, "rem", "rhythm:rem:20")
    assert cognition.calls == 2
    assert [name for name, _ in procedures.due_world_rhythms(now=NOW)] == ["dream-away"]


def test_an_offset_moves_where_the_interval_starts(tmp_path):
    rhythms = {"sleep": CHAIN["sleep"] | {"offset": 3600}, "rem": CHAIN["rem"]}
    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path, rhythms=rhythms)
    assert list(procedures.due_world_rhythms(now=DAY * 20 + 3599)) == [("sleep", "rhythm:sleep:19")]
    assert list(procedures.due_world_rhythms(now=DAY * 20 + 3600)) == [("sleep", "rhythm:sleep:20")]


@pytest.mark.parametrize(("rhythms", "error"), [
    ({"rem": CHAIN["rem"]}, "must follow a configured world rhythm"),
    ({"sleep": CHAIN["sleep"] | {"after": "rem"}, "rem": CHAIN["rem"]}, "exactly one of schedule or after"),
    ({"sleep": CHAIN["rem"] | {"after": "rem"}, "rem": CHAIN["rem"]}, "cycle"),
    ({"sleep": CHAIN["rem"] | {"after": "sleep"}}, "cycle"),
    ({"sleep": {k: v for k, v in CHAIN["sleep"].items() if k != "schedule"}},
     "exactly one of schedule or after"),
    ({"sleep": CHAIN["sleep"] | {"offset": DAY}}, "offset"),
    ({"sleep": CHAIN["sleep"] | {"paths": ["../outside"]}}, "inside the world"),
    ({"sleep": CHAIN["sleep"] | {"paths": ["/episodes"]}}, "inside the world"),
])
def test_configuration_refuses_an_unfollowable_chain(tmp_path, rhythms, error):
    with pytest.raises(ValueError, match=error):
        _night(tmp_path, tmp_path / "world", rhythms)


def test_after_and_paths_belong_to_world_rhythms(tmp_path):
    config = _config(tmp_path, tmp_path / "world").model_dump()
    config["repositories"] = {"app": {"path": str(tmp_path / "app"),
                                      "remote_url": "https://example.com/app.git"}}
    config["procedures"]["review"] = config["procedures"]["sleep"] | {"access": "read-only"}
    review = {"schedule": DAY, "procedure": "review", "input": "repositories/app/main", "owner": None}
    config["rhythms"] = {"review": review, "rem": CHAIN["rem"] | {"after": "review"}}
    with pytest.raises(ValueError, match="must follow a configured world rhythm"):
        StewardConfig.model_validate(config)
    config["rhythms"] = {"review": review | {"paths": ["episodes/"]}}
    with pytest.raises(ValueError, match="only to world rhythms"):
        StewardConfig.model_validate(config)


class Consolidating(EditingCognition):
    """A sleep that also rewrites the episodes it reads."""

    def run(self, request, *, execution_id=None):
        if callable(request):
            request = request()
        (request.cwd / "episodes").mkdir(exist_ok=True)
        (request.cwd / "episodes" / "digest.md").write_text(f"Digest {self.calls}.\n")
        return super().run(request, execution_id=execution_id)


def _world_commit(world_root, name):
    import subprocess

    path = world_root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name)
    subprocess.run(["git", "add", name], cwd=world_root, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@x", "commit", "-q", "-m", name],
                   cwd=world_root, check=True)


def test_paths_gate_calls_no_model_without_new_episodes(tmp_path):
    rhythms = {"sleep": CHAIN["sleep"] | {"paths": ["episodes/", "episodes.md"]}}
    config, state, checkpoint, service, cognition, procedures = _chain(
        tmp_path, Consolidating(), rhythms=rhythms)
    # No accepted run yet: the first interval runs.
    assert list(procedures.due_world_rhythms(now=NOW)) == [("sleep", "rhythm:sleep:20")]
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert (checkpoint.world.root / "episodes" / "digest.md").exists()

    # Its own write under its paths, and a change elsewhere, are not input.
    _world_commit(checkpoint.world.root, "notes/unrelated.md")
    assert list(procedures.due_world_rhythms(now=NOW + DAY)) == []
    assert list(procedures.due_world_rhythms(now=NOW + DAY + 3000)) == []

    # A new episode later in the same interval is: the first poll that sees it runs.
    _world_commit(checkpoint.world.root, "episodes/2026-09-27.md")
    assert list(procedures.due_world_rhythms(now=NOW + DAY + 3600)) == [("sleep", "rhythm:sleep:21")]
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:21")
    assert cognition.calls == 2
    _world_commit(checkpoint.world.root, "episodes.md")
    assert list(procedures.due_world_rhythms(now=NOW + 2 * DAY)) == [("sleep", "rhythm:sleep:22")]
    # A cursor Git can no longer read admits the run rather than silencing it.
    assert checkpoint.world.changed("0" * 40, ("episodes/",))


def _world_git(world_root, *args):
    import subprocess

    subprocess.run(["git", *args], cwd=world_root, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@x", "commit", "-q", "-m",
                    " ".join(args)], cwd=world_root, check=True)


def test_paths_gate_ignores_archiving_and_deletion_but_not_new_content(tmp_path):
    # Staging's cursor is its own last run. Sleep then archives what it
    # consolidated and deletes nothing new into the paths: nothing to stage.
    rhythms = {"staging": CHAIN["sleep"] | {"paths": ["episodes/", "episodes.md"]}}
    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path, rhythms=rhythms)
    root = checkpoint.world.root
    _world_commit(root, "episodes/2026-09-27.md")
    _world_commit(root, "episodes/2026-09-26.md")
    _world_commit(root, "episodes.md")
    procedures.run_world_rhythm(service, "staging", "rhythm:staging:20")

    (root / "episodes" / "archive").mkdir()
    _world_git(root, "mv", "episodes/2026-09-27.md", "episodes/archive/2026-09-27.md")
    _world_git(root, "rm", "-q", "episodes/2026-09-26.md")
    _world_git(root, "mv", "episodes.md", "index.md")
    assert list(procedures.due_world_rhythms(now=NOW + DAY)) == []

    # An appended episode is new content, and so is a new one.
    (root / "episodes" / "archive" / "2026-09-27.md").write_text("more\n")
    _world_git(root, "add", "episodes/archive/2026-09-27.md")
    assert list(procedures.due_world_rhythms(now=NOW + DAY)) == [("staging", "rhythm:staging:21")]
    procedures.run_world_rhythm(service, "staging", "rhythm:staging:21")
    _world_commit(root, "episodes/2026-09-28.md")
    assert list(procedures.due_world_rhythms(now=NOW + 2 * DAY)) == [("staging", "rhythm:staging:22")]


def test_a_gated_predecessor_that_did_not_run_does_not_start_its_dependent(tmp_path):
    rhythms = {"sleep": CHAIN["sleep"] | {"paths": ["episodes/"]}, "rem": CHAIN["rem"]}
    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path, rhythms=rhythms)
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    procedures.run_world_rhythm(service, "rem", "rhythm:rem:20")
    assert list(procedures.due_world_rhythms(now=NOW + DAY)) == []
    assert cognition.calls == 2


def test_world_rhythm_leads_with_its_preference_and_falls_back_unless_pinned(tmp_path):
    seen = []
    config, state, checkpoint, service, cognition, procedures = _chain(
        tmp_path, EditingCognition(before_return=seen.append), rhythms={"sleep": CHAIN["sleep"]})
    service._provider_order = ("claude", "codex", "glm")
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert seen[-1].provider_order == ("codex", "claude", "glm")
    assert (seen[-1].model.model, seen[-1].model.effort) == ("night-model", "high")

    procedures.config = _night(tmp_path, checkpoint.world.root, {"sleep": CHAIN["sleep"]}, fallback=False)
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:21")
    assert seen[-1].provider_order == ("codex",)


def test_rhythm_list_names_a_dependent_by_its_predecessor(tmp_path):
    from typing import cast

    from steward_harness.config.schema import UntrustedExecutionConfig
    from steward_harness.conversations import ConversationService
    from steward_harness.daemon import KernelCommands
    from steward_harness.repository_reconciler import RepositoryReconciler
    from steward_harness.runtime.execution import UntrustedExecutionBroker

    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path)
    commands = KernelCommands(
        config, state, cast(ConversationService, SimpleNamespace()),
        cast(RepositoryReconciler, SimpleNamespace()), {},
        UntrustedExecutionBroker(UntrustedExecutionConfig()), procedures=procedures,
    )
    assert "rem: after sleep, sleep, world, owner=retained only; this interval: not run yet" in (
        commands("rhythm", "list", 1, 3, 7))


def test_moving_or_deleting_an_episode_is_not_new_input(tmp_path):
    import subprocess

    rhythms = {"sleep": CHAIN["sleep"] | {"paths": ["episodes/"]}}
    config, state, checkpoint, service, cognition, procedures = _chain(
        tmp_path, Consolidating(), rhythms=rhythms)
    root = checkpoint.world.root
    _world_commit(root, "episodes/2026-09-27.md")
    _world_commit(root, "episodes/2026-09-28.md")
    cursor = checkpoint.world.input_cursor()

    def git(*args):
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@x", *args],
                       cwd=root, check=True, capture_output=True)

    # Archiving an episode is a rename within the paths: nothing new to read.
    (root / "episodes" / "archive").mkdir()
    git("mv", "episodes/2026-09-27.md", "episodes/archive/2026-09-27.md")
    git("commit", "-q", "-m", "archive")
    assert not checkpoint.world.changed(cursor, ("episodes/",))
    # Neither is a deletion.
    git("rm", "-q", "episodes/2026-09-28.md")
    git("commit", "-q", "-m", "prune")
    assert not checkpoint.world.changed(cursor, ("episodes/",))
    # A new episode is.
    _world_commit(root, "episodes/2026-09-29.md")
    assert checkpoint.world.changed(cursor, ("episodes/",))


def test_a_world_rhythm_is_bounded_by_its_procedure_budget_and_own_deadline(tmp_path):
    seen = []
    config, state, checkpoint, service, cognition, procedures = _rhythm(
        tmp_path, EditingCognition(before_return=seen.append))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    # Unconfigured, a rhythm keeps the conversational deadline and is unmetered.
    assert seen[-1].token_budget is None and seen[-1].timeout_seconds == service._timeout_seconds

    sleep = config.procedures["sleep"].model_copy(update={"token_budget": 250_000, "timeout_seconds": 5400})
    procedures.config = config.model_copy(update={"procedures": {"sleep": sleep}})
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:21")
    assert (seen[-1].token_budget, seen[-1].timeout_seconds) == (250_000, 5400)


def test_short_interval_cannot_starve_the_night_chain_and_staging(tmp_path, monkeypatch):
    """A 301s inbox used to win every poll and exclude every later rhythm."""
    inbox = CHAIN['sleep'] | {'schedule': 300, 'owner': None}
    staging = CHAIN['sleep'] | {'schedule': 3600, 'owner': None}
    rhythms = {'inbox': inbox, **CHAIN, 'staging': staging}
    rhythms['sleep'] = rhythms['sleep'] | {'offset': 3600}
    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path, rhythms=rhythms)
    clock = [NOW]
    monkeypatch.setattr('steward_harness.procedures.time.time', lambda: clock[0])
    ran = []
    for _ in range(6):
        name, key = next(iter(procedures.due_world_rhythms()))
        ran.append(name)
        procedures.advance_world_rhythm(service)
        clock[0] += 301
    assert ran == ['inbox', 'sleep', 'rem', 'dream-away', 'staging', 'inbox']
    assert cognition.calls == 6
    # Restart derives the same selection; no fairness cursor is persisted.
    restarted = Procedures(config, state, {}, world=checkpoint.world)
    assert list(restarted.due_world_rhythms()) == list(procedures.due_world_rhythms())


def test_queued_world_work_rechecks_bucket_and_pause_at_execution(tmp_path, monkeypatch):
    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path)
    daemon = StewardDaemon(config, tmp_path / 'steward.yaml')
    daemon._procedures = procedures
    queued = []
    kernel = SimpleNamespace(
        dispatch=SimpleNamespace(submit=lambda key, work: queued.append((key, work)), reap=lambda: None),
        owners=lambda: (), tasks=SimpleNamespace(flush_inputs=lambda: None))
    clock = [NOW]
    monkeypatch.setattr('steward_harness.procedures.time.time', lambda: clock[0])
    step = daemon._pass(state, service, kernel, SimpleNamespace(), None)
    step()
    work, = [work for key, work in queued if key == ('rhythm', 'world')]
    clock[0] += DAY
    work()
    assert state.turn_for_source(ConversationId('rhythm:sleep'), 'rhythm:sleep:20') is None
    assert state.turn_for_source(ConversationId('rhythm:sleep'), 'rhythm:sleep:21').state == 'completed'
    clock[0] += DAY
    monkeypatch.setattr(state, 'paused', lambda: True)
    work()
    assert cognition.calls == 1


def test_observation_distinguishes_offset_active_failure_and_blocked_chain(tmp_path):
    rhythms = CHAIN | {'sleep': CHAIN['sleep'] | {'offset': 3600}}
    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path, rhythms=rhythms)
    observed = procedures.world_rhythm_observations(now=NOW + 9)
    assert observed['sleep']['due_at'] == NOW
    assert observed['sleep']['overdue_seconds'] == 9
    assert observed['rem']['progress'] == 'awaiting_predecessor'
    owner = ConversationId('rhythm:sleep')
    state.open_conversation(owner, provider='codex', profile='balanced')
    turn, _ = state.start_turn(owner, 'rhythm:sleep:20', 'harness:rhythm', 'synthetic personal evidence')
    assert procedures.world_rhythm_observations(now=NOW + 3600)['sleep']['progress'] == 'recovery_held'
    state.interrupt_turn(turn.turn_id, 'synthetic provider failure')
    observed = procedures.world_rhythm_observations(now=NOW + 3600)
    assert observed['sleep']['progress'] == 'held'
    assert observed['rem']['progress'] == 'predecessor_held'
    assert not observed['sleep']['eligible']
    assert list(procedures.due_world_rhythms(now=NOW + 3600)) == []
    assert not procedures.world_rhythm_observations(now=NOW + DAY)['sleep']['eligible']


def test_observation_uses_world_input_guard_and_retains_completion_timing(tmp_path):
    rhythms = {'sleep': CHAIN['sleep'] | {'paths': ['episodes/']}}
    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path, rhythms=rhythms)
    procedures.run_world_rhythm(service, 'sleep', 'rhythm:sleep:20')
    settled = procedures.world_rhythm_observations(now=NOW)['sleep']
    assert settled['progress'] == 'scheduled'
    assert settled['started_at'] and settled['completed_at']
    _world_commit(checkpoint.world.root, 'maintenance/service.md')
    waiting = procedures.world_rhythm_observations(now=NOW + DAY)['sleep']
    assert waiting['progress'] == 'awaiting_input'
    assert waiting['observed_at'] == NOW + DAY
    _world_commit(checkpoint.world.root, 'episodes/personal.md')
    assert procedures.world_rhythm_observations(now=NOW + DAY)['sleep']['progress'] == 'overdue'


def test_missing_policy_holds_interval_instead_of_starving_other_work(tmp_path):
    rhythms = {'inbox': CHAIN['sleep'] | {'schedule': 300}, 'sleep': CHAIN['sleep']}
    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path, rhythms=rhythms)
    policy = config.procedures['sleep'].instructions
    __import__('pathlib').Path(policy).unlink()
    procedures.run_world_rhythm(service, 'inbox', f'rhythm:inbox:{NOW // 300}')
    observed = procedures.world_rhythm_observations(now=NOW)
    assert observed['inbox']['progress'] == 'held'
    assert list(procedures.due_world_rhythms(now=NOW)) == [('sleep', 'rhythm:sleep:20')]
    assert cognition.calls == 0
    # The hold survives restart and rollover without starving other rhythms.
    restarted = Procedures(config, state, {}, world=checkpoint.world)
    assert 'inbox' not in dict(restarted.due_world_rhythms(now=NOW))
    assert 'inbox' not in dict(restarted.due_world_rhythms(now=NOW + 300))


@pytest.mark.parametrize("failure", ["timeout", "cancelled", "unavailable"])
def test_explicit_continuation_preserves_attempt_evidence_and_captured_intent(tmp_path, failure):
    from pathlib import Path
    from steward_harness.runtime.contracts import RuntimeUnavailable
    from steward_harness.runtime.process import ProcessTimeout

    seen = []
    class Interrupted(EditingCognition):
        def run(self, request, *, execution_id=None):
            self.calls += 1
            if failure == "unavailable":
                raise RuntimeUnavailable("provider unavailable before tools")
            request = request()
            seen.append(request)
            (request.cwd / "partial.md").write_text("retained partial work")
            (request.cwd / "native-evidence.txt").write_text("external action may have happened")
            if failure == "timeout":
                raise ProcessTimeout("deadline after edits")
            raise RuntimeExecutionError("operator cancelled native execution")

    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path, Interrupted())
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    failed = state.turn_for_source(ConversationId("rhythm:sleep"), "rhythm:sleep:20")
    assert failed.state == "interrupted"
    # Polls, restart and rollover never mean permission to repeat the provider.
    state, checkpoint, service, _ = runtime(tmp_path, cognition)
    procedures = Procedures(config, state, {}, world=checkpoint.world)
    assert list(procedures.due_world_rhythms(now=NOW + 10 * DAY)) == []
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:30")
    assert cognition.calls == 1
    Path(config.procedures["sleep"].instructions).write_text("CHANGED POLICY MUST NOT REPLACE INTENT")

    def inspect(request):
        assert "Consolidate the world." in request.prompt
        assert "CHANGED POLICY" not in request.prompt
        assert str(failed.turn_id) in request.prompt
        assert "External actions may already have happened" in request.prompt
        if seen:
            assert request.cwd == seen[0].cwd
            assert (request.cwd / "partial.md").read_text() == "retained partial work"
            assert (request.cwd / "native-evidence.txt").read_text() == "external action may have happened"

    success = EditingCognition(inspect)
    state, checkpoint, service, _ = runtime(tmp_path, success)
    procedures = Procedures(config, state, {}, world=checkpoint.world)
    procedures.continue_world_rhythm("sleep", now=NOW + DAY)
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    attempts = state.rhythm_turns(ConversationId("rhythm:sleep"))
    assert len(attempts) == 2
    assert attempts[0] == failed  # The failure itself was not rewritten.
    assert attempts[1].state == "completed"
    assert state.result_receipt("rhythm:sleep:20")
    assert not state.result_receipt(attempts[1].source_event_key)
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    procedures.continue_world_rhythm("sleep", now=NOW)
    assert success.calls == 1


def test_uncertain_provider_crash_holds_without_replay_or_starving_other_rhythms(tmp_path):
    def crash(request):
        (request.cwd / "uncertain.md").write_text("possible external action")
        raise Crash()

    config, state, checkpoint, service, cognition, procedures = _chain(
        tmp_path, EditingCognition(crash), rhythms={"sleep": CHAIN["sleep"], "staging": CHAIN["sleep"]})
    with pytest.raises(Crash):
        procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    state, checkpoint, service, _ = runtime(tmp_path, cognition)
    procedures = Procedures(config, state, {}, world=checkpoint.world)
    assert list(procedures.due_world_rhythms(now=NOW + DAY)) == [("staging", "rhythm:staging:21")]
    answer = procedures.continue_world_rhythm("sleep", now=NOW + DAY)
    assert "recovery held" in answer
    assert cognition.calls == 1
    assert len(state.claimed_turns()) == 1


def test_continuation_acceptance_crash_recovers_old_receipt_without_provider_replay(tmp_path, monkeypatch):
    def fail(request):
        raise RuntimeExecutionError("stopped after edits")
    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path, EditingCognition(fail))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    cognition.before_return = lambda request: None
    monkeypatch.setattr(state, "save_result_receipt", lambda _: (_ for _ in ()).throw(Crash()))
    procedures.continue_world_rhythm("sleep", now=NOW)
    with pytest.raises(Crash):
        procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    monkeypatch.undo()
    state, checkpoint, service, _ = runtime(tmp_path, cognition)
    procedures = Procedures(config, state, {}, world=checkpoint.world)
    assert list(procedures.due_world_rhythms(now=NOW + DAY)) == [
        ("sleep", "rhythm:sleep:20"), ("rem", "rhythm:rem:20")]
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert cognition.calls == 2
    assert state.result_receipt("rhythm:sleep:20")


def test_manual_and_automatic_world_runs_share_exclusion(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    def hold(request):
        entered.set()
        assert release.wait(10)

    config, state, checkpoint, service, cognition, procedures = _chain(
        tmp_path, EditingCognition(hold), rhythms={"sleep": CHAIN["sleep"], "staging": CHAIN["sleep"]})
    owner = ConversationId("rhythm:staging")
    state.open_conversation(owner, provider="codex", profile="balanced")
    failed, _ = state.start_turn(owner, "rhythm:staging:20", "harness:rhythm", "Stage episodes.")
    state.interrupt_turn(failed.turn_id, "provider unavailable")
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(procedures.run_world_rhythm, service, "sleep", "rhythm:sleep:20")
        try:
            assert entered.wait(10)
            other = Procedures(config, state, {}, world=checkpoint.world)
            assert "held" in other.continue_world_rhythm("staging", now=NOW)
            other.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
            assert cognition.calls == 1
            assert len(state.rhythm_turns(owner)) == 1
        finally:
            release.set()
        running.result()
    cognition.before_return = lambda request: None
    procedures.continue_world_rhythm("staging", now=NOW)
    procedures.run_world_rhythm(service, "staging", "rhythm:staging:20")
    assert cognition.calls == 2


def test_rhythm_command_continues_held_obligation_under_original_key(tmp_path, monkeypatch):
    from steward_harness.daemon import KernelCommands
    from steward_harness.config.schema import UntrustedExecutionConfig
    from steward_harness.runtime.execution import UntrustedExecutionBroker

    def fail(request):
        raise RuntimeExecutionError("provider failed")
    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path, EditingCognition(fail))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    cognition.before_return = lambda request: None
    commands = KernelCommands(config, state, service, SimpleNamespace(), {},
                              UntrustedExecutionBroker(UntrustedExecutionConfig()), procedures=procedures)
    monkeypatch.setattr("steward_harness.procedures.time.time", lambda: NOW + DAY)
    listing = commands("rhythm", "list", 1, 3, 7)
    assert "obligation=rhythm:sleep:20, held" in listing
    assert "rhythm:sleep:20: continuation queued" in commands("rhythm", "run sleep", 1, 3, 7)
    assert cognition.calls == 1  # The command does no provider work.
    assert "continuation_queued" in commands("rhythm", "run sleep", 1, 3, 7)
    state, checkpoint, service, _ = runtime(tmp_path, cognition)
    state.interrupt_abandoned_turns()
    procedures = Procedures(config, state, {}, world=checkpoint.world)
    assert len(state.rhythm_turns(ConversationId("rhythm:sleep"))) == 2
    assert procedures.world_rhythm_observations(now=NOW + DAY)["sleep"]["progress"] == "continuation_queued"
    procedures.advance_world_rhythm(service)
    assert state.result_receipt("rhythm:sleep:20")
    assert cognition.calls == 2


def test_retained_provider_output_recovers_after_rollover_without_native_replay(tmp_path, monkeypatch):
    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path)
    monkeypatch.setattr(service, "_prepare_completion", lambda _: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    monkeypatch.undo()
    state, checkpoint, service, _ = runtime(tmp_path, cognition)
    procedures = Procedures(config, state, {}, world=checkpoint.world)
    assert procedures.world_rhythm_observations(now=NOW + DAY)["sleep"]["progress"] == "acceptance_pending"
    assert next(procedures.due_world_rhythms(now=NOW + DAY)) == ("sleep", "rhythm:sleep:20")
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert cognition.calls == 1
    assert state.result_receipt("rhythm:sleep:20")
    assert next(procedures.due_world_rhythms(now=NOW + DAY)) == ("rem", "rhythm:rem:20")


def test_policy_captured_after_admission_failure_survives_later_failures(tmp_path):
    from pathlib import Path
    def fail(request):
        raise RuntimeExecutionError("provider failed after policy capture")
    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path, EditingCognition(fail))
    policy = Path(config.procedures["sleep"].instructions)
    policy.unlink()
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert cognition.calls == 0
    policy.write_text("Original recovered policy.")
    procedures.continue_world_rhythm("sleep", now=NOW)
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert cognition.calls == 1
    policy.write_text("Replacement policy must not replace captured intent.")
    def inspect(request):
        assert "Original recovered policy." in request.prompt
        assert "Replacement policy" not in request.prompt
    cognition.before_return = inspect
    procedures.continue_world_rhythm("sleep", now=NOW)
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert cognition.calls == 2
    assert state.result_receipt("rhythm:sleep:20")


def test_queued_continuation_defers_before_checkout_without_losing_authorization(tmp_path, monkeypatch):
    from steward_harness.lease import Busy

    def fail(request):
        raise RuntimeExecutionError("interrupted after edits")
    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path, EditingCognition(fail))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    procedures.continue_world_rhythm("sleep", now=NOW)
    reserved = state.rhythm_turns(ConversationId("rhythm:sleep"))[-1]
    monkeypatch.setattr(checkpoint, "checkout", lambda **_: (_ for _ in ()).throw(Busy("world owned")))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert state.get_turn(reserved.turn_id) == reserved
    assert procedures.world_rhythm_observations(now=NOW)["sleep"]["progress"] == "continuation_queued"
    monkeypatch.undo()
    cognition.before_return = lambda request: None
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert state.result_receipt("rhythm:sleep:20")
    assert cognition.calls == 2


def test_claimed_continuation_crash_cannot_reexecute_its_reserved_source(tmp_path):
    def fail(request):
        raise RuntimeExecutionError("interrupted after edits")
    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path, EditingCognition(fail))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    procedures.continue_world_rhythm("sleep", now=NOW)
    cognition.before_return = lambda _: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    state, checkpoint, service, _ = runtime(tmp_path, cognition)
    state.interrupt_abandoned_turns()
    procedures = Procedures(config, state, {}, world=checkpoint.world)
    assert list(procedures.due_world_rhythms(now=NOW + DAY)) == []
    procedures.continue_world_rhythm("sleep", now=NOW + DAY)
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert cognition.calls == 2
    assert len(state.claimed_turns()) == 1
    assert not state.result_receipt("rhythm:sleep:20")


def test_pause_keeps_a_continuation_queued_and_prevents_new_authorization(tmp_path, monkeypatch):
    def fail(request):
        raise RuntimeExecutionError("provider unavailable")
    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path, EditingCognition(fail))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    state.set_paused(True)
    assert "paused" in procedures.continue_world_rhythm("sleep", now=NOW)
    assert len(state.rhythm_turns(ConversationId("rhythm:sleep"))) == 1
    state.set_paused(False)
    procedures.continue_world_rhythm("sleep", now=NOW)
    state.set_paused(True)
    monkeypatch.setattr("steward_harness.procedures.time.time", lambda: NOW + DAY)
    procedures.advance_world_rhythm(service)
    assert cognition.calls == 1
    assert procedures.world_rhythm_observations()["sleep"]["progress"] == "continuation_queued"
    state.set_paused(False)
    cognition.before_return = lambda request: None
    procedures.advance_world_rhythm(service)
    assert cognition.calls == 2
    assert state.result_receipt("rhythm:sleep:20")


def test_continuation_replays_prior_notification_receipt_through_native_tool(tmp_path):
    import os
    from steward_harness.runtime.process import ProcessTimeout
    from test_task_calls import call

    notices = []
    sources = []
    def notify_then_interrupt(request):
        request.on_process_started(os.getpid(), None)
        source = sources[0] if sources else request.execution_id
        sources.append(request.execution_id)
        receipt = call(request.task_call_socket, operation="notify", key="night-status",
                       text="The night's work is retained.", source_id=source)
        assert receipt["accepted"], receipt
        notices.append(receipt)
        if len(sources) == 1:
            raise ProcessTimeout("deadline after durable notification")

    config, state, checkpoint, service, cognition, procedures = _rhythm(
        tmp_path, EditingCognition(notify_then_interrupt))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert len(state.pending_result_receipts()) == 1
    assert not notices[0]["replayed"]
    procedures.continue_world_rhythm("sleep", now=NOW + DAY)
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert sources[0] != sources[1]
    assert notices[1]["replayed"]
    assert notices[1]["receipt"] == notices[0]["receipt"]
    assert notices[1]["owner"] == "telegram:3"
    assert len(state.pending_result_receipts()) == 1
    assert state.result_receipt("rhythm:sleep:20")["done"]
    assert state.tasks.all() == []


def test_a_dependent_added_later_starts_at_its_predecessors_newest_interval():
    from steward_harness.procedures import world_rhythm_interval

    rhythms = {"staging": SimpleNamespace(after=None, offset=0, schedule=300),
               "chaos": SimpleNamespace(after="staging", offset=0, schedule=None)}
    accepted = [SimpleNamespace(state="completed")]
    staging = {20: accepted, 21: accepted, 22: accepted}
    receipt = lambda _key: True
    # New: only the newest accepted predecessor interval, not its history.
    assert world_rhythm_interval(rhythms, "chaos", 23 * 300 + 1, {}, staging, receipt) == 22
    # Established: every predecessor interval since its own newest run.
    assert world_rhythm_interval(rhythms, "chaos", 23 * 300 + 1, {20: accepted}, staging, receipt) == 21
