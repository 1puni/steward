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
    assert "start a line with `NOTIFY:`" in request.prompt
    # A rhythm owns no transport, so its turn cannot admit the task it proposed.
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


def _replying(output, *, write=None):
    def edit(request):
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
def test_a_world_rhythm_sends_what_follows_its_notify_line(tmp_path, output, message):
    config, state, checkpoint, service, cognition, procedures = _rhythm(tmp_path, _replying(output))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    receipt = state.result_receipt("rhythm:sleep:20")
    assert receipt["reply"] == message and not receipt["done"] and not receipt["recorded_only"]
    assert receipt["result_text"] == output.strip()


def _delivering(tmp_path, cognition):
    config, state, checkpoint, service, cognition, _ = _rhythm(tmp_path, cognition)
    data = config.model_dump(mode="json")
    data["rhythms"]["sleep"]["deliver"] = "morning_brief.md"
    config = StewardConfig.model_validate(data)
    return config, state, checkpoint, service, Procedures(config, state, {}, world=checkpoint.world)


def test_a_rhythm_that_rewrote_its_deliver_file_sends_the_file(tmp_path):
    brief = "Good morning, V.\n\nTwo days to Wednesday.\n"
    config, state, checkpoint, service, procedures = _delivering(
        tmp_path, _replying("<br>", write={"morning_brief.md": brief}))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    receipt = state.result_receipt("rhythm:sleep:20")
    assert receipt["reply"] == brief.strip() and not receipt["done"]
    # The file is the message whatever the reply says, a NOTIFY line included.
    state, checkpoint, service, _ = runtime(
        tmp_path, _replying("NOTIFY: See the brief.", write={"morning_brief.md": brief + "More.\n"}))
    procedures = Procedures(config, state, {}, world=checkpoint.world)
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:21")
    assert state.result_receipt("rhythm:sleep:21")["reply"] == (brief + "More.").strip()


def test_an_unchanged_deliver_file_is_not_resent(tmp_path):
    brief = "Good morning, V.\n"
    config, state, checkpoint, service, procedures = _delivering(
        tmp_path, _replying("", write={"morning_brief.md": brief}))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert state.result_receipt("rhythm:sleep:20")["reply"] == brief.strip()
    # The next night leaves it as it was: only an explicit notification is sent.
    state, checkpoint, service, _ = runtime(
        tmp_path, _replying("NOTIFY: Sleep did not complete.", write={"morning_brief.md": brief}))
    procedures = Procedures(config, state, {}, world=checkpoint.world)
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:21")
    assert state.result_receipt("rhythm:sleep:21")["reply"] == "Sleep did not complete."


def test_a_long_deliver_file_is_cut_with_a_pointer(tmp_path):
    from steward_harness.procedures import DELIVERY_LIMIT
    config, state, checkpoint, service, procedures = _delivering(
        tmp_path, _replying("", write={"morning_brief.md": "x" * (DELIVERY_LIMIT + 50)}))
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    reply = state.result_receipt("rhythm:sleep:20")["reply"]
    assert reply.startswith("x" * DELIVERY_LIMIT)
    assert reply.endswith("[morning_brief.md continues in the world.]")


def test_deliver_names_a_file_inside_the_world(tmp_path):
    (tmp_path / "world").mkdir()
    data = _config(tmp_path, tmp_path / "world").model_dump(mode="json")
    data["rhythms"]["sleep"]["deliver"] = "../outside.md"
    with pytest.raises(ValueError, match="inside the world"):
        StewardConfig.model_validate(data)


def test_pass_runs_the_rhythm_and_delivers_its_reply_to_the_owner_topic(tmp_path):
    config, state, checkpoint, service, cognition, procedures = _rhythm(
        tmp_path, _replying("Investigation saved.\nNOTIFY: Investigation saved for the morning."))
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
    assert [name for name, _ in procedures.due_world_rhythms(now=NOW)] == ["sleep"]


def test_a_failed_predecessor_ends_the_chain_for_the_interval(tmp_path):
    class Failing(EditingCognition):
        def run(self, request, *, execution_id=None):
            self.calls += 1
            request()
            raise RuntimeExecutionError("provider failed")

    config, state, checkpoint, service, cognition, procedures = _chain(tmp_path, Failing())
    procedures.run_world_rhythm(service, "sleep", "rhythm:sleep:20")
    assert list(procedures.due_world_rhythms(now=NOW)) == []
    assert list(procedures.due_world_rhythms(now=NOW + DAY)) == [("sleep", "rhythm:sleep:21")]
    assert cognition.calls == 1


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
