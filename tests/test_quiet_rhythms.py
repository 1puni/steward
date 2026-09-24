"""Quiet Git activity batches become one accepted procedure task."""
from dataclasses import replace
import os
import subprocess

import pytest
from pydantic import ValidationError

from steward_harness.config.schema import ProcedureRhythmConfig
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.procedures import Procedures
from steward_harness.state import ConversationId, StateDatabase, TaskSpec
from test_git_tasks import harness
from test_rewrite_convergence import setup_procedures
from test_task_no_changes import InvestigationAdapter
from test_task_runner_kernel import _git, _repository
from test_world_turn_checkpoint import _git_world, _commit_all


def quiet_harness(tmp_path, **callbacks):
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    execute = adapter.execute
    adapter.execute = lambda request: replace(execute(request), output=(
        "Existing findings are owned.\nVERDICT: FAIL\nCOMMIT: reviewed\n"
        "DISPOSITION: idle\nQUESTION: NONE"))
    state, runner, _, _ = harness(tmp_path / "state", bare, clone, adapter)
    config, _ = setup_procedures(tmp_path, state, runner)
    config.rhythms["light"] = ProcedureRhythmConfig(owner=None, schedule={"quiet": 300},
        procedure="security-one", input="repositories/app/main")
    procedures = Procedures(config, state, runner.transports, **callbacks)
    return clone, state, runner, config, procedures, adapter


def commit(clone, name, *, push=True):
    (clone / name).write_text(name)
    _git("add", name, cwd=clone)
    # Deliberately stale author/committer dates cannot shorten observed quiet.
    subprocess.run(["git", "commit", "-q", "-m", name], cwd=clone, check=True,
        env=os.environ | {"GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z",
                          "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"})
    if push:
        _git("push", "origin", "main", cwd=clone)
    return _git("rev-parse", "HEAD", cwd=clone)


def test_no_work_means_no_light_then_nineteen_ticks_reuse_one_batch(tmp_path):
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    for now in range(0, 6000, 300):
        procedures.advance_rhythms(now=now)
    assert not state.tasks.all()
    sha = commit(clone, "changed.txt")
    procedures.advance_rhythms(now=6000)
    procedures.advance_rhythms(now=6299)
    assert not state.tasks.all()
    procedures.advance_rhythms(now=6300)
    task = state.tasks.queued()[0]
    run = state.tasks.read(task)[1].procedure
    assert run.activity["repositories/app/main"] == sha
    assert run.candidate == sha and ":quiet:" in run.event
    runner.prepare(task)
    assert state.tasks.get(task).verdict == "fail"
    for now in range(6600, 12300, 300):
        procedures.advance_rhythms(now=now)
    reopened = StateDatabase(state.path)
    restarted = Procedures(config, reopened, runner.transports)
    for now in (13000, 14000):
        restarted.advance_rhythms(now=now)
    assert len(reopened.tasks.all()) == len(adapter.requests) == 1
    # An explicit request remains a new task on unchanged inputs.
    manual = restarted.request(run.name, "app", run.candidate, run.base, event="manual:requested")
    assert manual != task


def test_other_repository_and_world_activity_reset_same_quiet_window(tmp_path):
    world = _git_world(tmp_path / "world")
    clone, state, runner, _, procedures, _ = quiet_harness(tmp_path, world=world)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other_bare, other_clone = _repository(other_root)
    runner.transports["other"] = ControllerGitTransport(state.path, "other", str(other_bare), "main", allow_local=True)
    procedures.advance_rhythms(now=0)
    first = commit(clone, "one.txt")
    procedures.advance_rhythms(now=10)
    other = commit(other_clone, "two.txt")
    procedures.advance_rhythms(now=200)
    (world.root / "observation.md").write_text("An operator's new observation.\n")
    world_sha = _commit_all(world.root, "operator observation")
    procedures.advance_rhythms(now=450)
    procedures.advance_rhythms(now=749)
    assert not state.tasks.all()
    procedures.advance_rhythms(now=750)
    task = state.tasks.queued()[0]
    activity = state.tasks.read(task)[1].procedure.activity
    assert activity["repositories/app/main"] == first
    assert activity["repositories/other/main"] == other
    assert activity["world"] == world_sha
    runner.prepare(task)
    # A world-only commit starts another batch without a repository change.
    (world.root / "decision.md").write_text("New independent intent.\n")
    _commit_all(world.root, "world-only activity")
    procedures.advance_rhythms(now=800)
    procedures.advance_rhythms(now=1099)
    assert len(state.tasks.all()) == 1
    procedures.advance_rhythms(now=1100)
    assert len(state.tasks.all()) == 2


def test_org_reflection_refreshes_sibling_refs_without_moving_local_work(tmp_path):
    from steward_harness.config.schema import RepositoryConfig
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    peer_root = tmp_path / "peer"
    peer_root.mkdir()
    remote, sibling = _repository(peer_root)
    repository = RepositoryConfig(path=str(sibling), remote_url=str(remote))
    transport = ControllerGitTransport(state.path, "peer", str(remote), "main", allow_local=True)
    runner.repositories["peer"] = repository
    runner.transports["peer"] = transport
    local = _git("rev-parse", "HEAD", cwd=sibling)
    newer = commit(sibling, "newer.txt")
    _git("checkout", "--detach", local, cwd=sibling)
    (sibling / "uncommitted.txt").write_text("preserve me")
    config.rhythms["light"] = config.rhythms["light"].model_copy(update={"workdir": str(tmp_path)})
    procedures.advance_rhythms(now=0)
    commit(clone, "changed.txt")
    procedures.advance_rhythms(now=10)
    procedures.advance_rhythms(now=310)
    task = state.tasks.queued()[0]
    execute = adapter.execute

    def inspect(request):
        assert request.cwd == tmp_path
        assert _git("rev-parse", "refs/steward/remote/main", cwd=sibling) == newer
        assert _git("show", "refs/steward/remote/main:newer.txt", cwd=sibling) == "newer.txt"
        assert _git("rev-parse", "HEAD", cwd=sibling) == local
        assert (sibling / "uncommitted.txt").read_text() == "preserve me"
        return execute(request)

    adapter.execute = inspect
    runner.prepare(task)
    assert state.tasks.get(task).status.value == "done"


def test_native_unpublished_work_resets_timer_and_own_review_refs_do_not(tmp_path):
    native = {}
    clone, state, runner, _, procedures, _ = quiet_harness(tmp_path, native_heads=lambda: dict(native))
    procedures.advance_rhythms(now=0)
    sha = commit(clone, "native-one.txt", push=False)
    native["native:app:refs/heads/work"] = sha
    procedures.advance_rhythms(now=100)
    native["native:app:refs/heads/work"] = commit(clone, "native-two.txt", push=False)
    procedures.advance_rhythms(now=300)
    procedures.advance_rhythms(now=599)
    assert not state.tasks.all()
    procedures.advance_rhythms(now=600)
    task = state.tasks.queued()[0]
    runner.prepare(task)
    native[f"native:app:refs/heads/tasks/{task}"] = state.tasks.read(task)[1].work
    procedures.advance_rhythms(now=700)
    procedures.advance_rhythms(now=1100)
    assert len(state.tasks.all()) == 1
    # Removing a ref and adding an alias are not new commits.
    old = native.pop("native:app:refs/heads/work")
    procedures.advance_rhythms(now=1200)
    native["native:app:refs/heads/alias"] = old
    procedures.advance_rhythms(now=1300)
    procedures.advance_rhythms(now=1600)
    assert len(state.tasks.all()) == 1


def test_nonrhythm_accepted_task_work_is_activity(tmp_path):
    _, state, runner, _, procedures, _ = quiet_harness(tmp_path)
    procedures.advance_rhythms(now=0)
    product, _ = state.tasks.create(TaskSpec("app", "Investigate", "Investigate a real obligation."))
    procedures.advance_rhythms(now=10)
    assert len(state.tasks.all()) == 1  # Admission alone is not a native work commit.
    runner.prepare(product)
    procedures.advance_rhythms(now=100)
    procedures.advance_rhythms(now=399)
    assert len(state.tasks.all()) == 1
    procedures.advance_rhythms(now=400)
    light = [t for t in state.tasks.all() if t.task_id != product][0]
    assert state.tasks.read(light.task_id)[1].procedure.activity[f"tasks/{product}"] == state.tasks.read(product)[1].work


@pytest.mark.parametrize("alias", ["none", "native", "remote"])
def test_own_world_assessment_batch_is_ignored_but_later_operator_commit_is_not(tmp_path, alias):
    world = _git_world(tmp_path / "world")
    callbacks = {"world": world}
    if alias == "native":
        callbacks["native_heads"] = lambda: {
            "native:world:refs/heads/steward": world.input_cursor()}
    clone, state, runner, config, procedures, _ = quiet_harness(tmp_path, **callbacks)
    if alias == "remote":
        branch = _git("branch", "--show-current", cwd=world.root)
        runner.transports["world"] = ControllerGitTransport(
            state.path, "world", str(world.root), branch, allow_local=True)
    procedures.advance_rhythms(now=0)
    commit(clone, "work.txt")
    procedures.advance_rhythms(now=10)
    procedures.advance_rhythms(now=310)
    task = state.tasks.queued()[0]
    runner.prepare(task)
    base = world.input_cursor()
    (world.root / "decision.md").write_text("Assessment changes durable memory.\n")
    _commit_all(world.root, "native assessment work")
    world.finish("turn_" + "a" * 32, "Assess result", "", base=base,
                 source=f"task_result:{task}:revision:done")
    applied = world.input_cursor()
    # A second assessment chains to the first, as blocked then done assessments do.
    world.finish("turn_" + "b" * 32, "Assess completion", "", base=applied,
                 source=f"task_result:{task}:completed:done")
    # The closing commit's trailers identify our own work, accepted or not.
    procedures.advance_rhythms(now=400)
    procedures.advance_rhythms(now=800)
    assert len(state.tasks.all()) == 1
    procedures = Procedures(config, StateDatabase(state.path), runner.transports, **callbacks)
    procedures.advance_rhythms(now=810)
    procedures.advance_rhythms(now=1110)
    assert len(state.tasks.all()) == 1
    (world.root / "decision.md").write_text("Operator: new work.\n")
    _commit_all(world.root, "operator work")
    procedures.advance_rhythms(now=1200)
    procedures.advance_rhythms(now=1500)
    assert len(state.tasks.all()) == 2


def test_restart_with_changed_work_waits_full_quiet_and_daily_still_recurs(tmp_path):
    clone, state, runner, config, procedures, _ = quiet_harness(tmp_path)
    config.rhythms["daily"] = ProcedureRhythmConfig(owner=None, schedule=86400,
        procedure="security-two", input="repositories/app/main")
    procedures.advance_rhythms(now=0)
    daily = state.tasks.queued()[0]
    runner.prepare(daily)
    commit(clone, "first.txt")
    procedures.advance_rhythms(now=100)
    procedures.advance_rhythms(now=400)
    light = state.tasks.queued()[0]
    runner.prepare(light)
    commit(clone, "while-offline.txt")
    restarted = Procedures(config, StateDatabase(state.path), runner.transports)
    restarted.advance_rhythms(now=1000)
    restarted.advance_rhythms(now=1299)
    assert len(state.tasks.all()) == 2
    restarted.advance_rhythms(now=1300)
    newer = state.tasks.queued()[0]
    runner.prepare(newer)
    restarted.advance_rhythms(now=86400)
    assert len(state.tasks.all()) == 4
    assert state.tasks.read(state.tasks.queued()[0])[1].procedure.name == "security-two"


@pytest.mark.parametrize("schedule", [0, -1, {"quiet": 0}, {"quiet": -1}, {"quiet": 300, "unknown": True}])
def test_schedule_rejects_invalid_policy(schedule):
    with pytest.raises(ValidationError):
        ProcedureRhythmConfig(owner=None, schedule=schedule, procedure="review", input="repositories/app/main")


def test_new_org_input_reaches_cognition_when_anchor_is_unchanged(tmp_path):
    native = {}
    clone, state, runner, config, procedures, adapter = quiet_harness(
        tmp_path, native_heads=lambda: dict(native))
    procedures.advance_rhythms(now=0)
    anchor = _git("rev-parse", "HEAD", cwd=clone)
    titles = []
    for index, now in enumerate((10, 400)):
        sha = commit(clone, f"unpublished-{index}.txt", push=False)
        native["native:app:refs/heads/main"] = sha
        procedures.advance_rhythms(now=now)
        procedures.advance_rhythms(now=now + 300)
        task = state.tasks.queued()[0]
        definition = state.tasks.read(task)[1]
        assert definition.procedure.candidate == anchor
        titles.append(definition.title)
        runner.prepare(task)
        account = adapter.requests[-1].prompt
        assert f"native:app:refs/heads/main: {sha}" not in account
        assert config.repositories["app"].path not in account
        assert definition.procedure.activity["native:app:refs/heads/main"] == sha
        assert "Execute security-one across the organisation" in account
        assert "Execute security-one on candidate" not in account
    assert len(set(titles)) == 2


def test_large_activity_stays_in_git_metadata_and_reflection_starts_at_org_root(tmp_path):
    from pathlib import Path
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    root = clone.parent
    config.rhythms["light"] = config.rhythms["light"].model_copy(update={"workdir": str(root)})
    procedures.advance_rhythms(now=0)
    sha = commit(clone, "new-evidence.txt")
    # A real organisation's many branches exceed the old brief limit.
    activity = {f"native:app:refs/heads/feature-{i}": sha for i in range(200)}
    procedures.native_heads = lambda: activity
    procedures.advance_rhythms(now=10)
    procedures.advance_rhythms(now=310)
    task = state.tasks.queued()[0]
    definition = StateDatabase(state.path).tasks.read(task)[1]
    assert definition.procedure.workdir == str(root)
    assert all(definition.procedure.activity[k] == v for k, v in activity.items())
    assert len(state.tasks.get(task).brief) < 500
    execute = adapter.execute

    def inspect(request):
        assert request.cwd == root
        assert (request.cwd / clone.name / "new-evidence.txt").read_text() == "new-evidence.txt"
        assert "Execute security-one across the organisation" in request.prompt
        assert "Accepted procedure" in request.prompt
        assert "feature-" not in request.prompt
        assert "VERDICT:" not in request.prompt
        assert request.sandbox_mode == "read-only"
        return execute(request)

    adapter.execute = inspect
    runner.prepare(task)
    assert state.tasks.get(task).status.value == "done"
    assert state.tasks.get(task).verdict is None
    assert "Existing findings are owned" in state.tasks.git(
        "show", "-s", "--format=%B", state.tasks.read(task)[1].work)
    restarted = Procedures(config, StateDatabase(state.path), runner.transports,
                           native_heads=lambda: activity)
    restarted.advance_rhythms(now=400)
    restarted.advance_rhythms(now=1000)
    assert len(state.tasks.all()) == 1


def test_refused_rhythm_admission_does_not_stop_other_rhythms(tmp_path, caplog):
    _, state, _, config, procedures, _ = quiet_harness(tmp_path)
    config.rhythms.clear()
    for name, procedure in (("broken", "security-one"), ("healthy", "security-two")):
        config.rhythms[name] = ProcedureRhythmConfig(owner=None, schedule=86400,
            procedure=procedure, input="repositories/app/main")
    from pathlib import Path
    instructions = Path(config.procedures["security-one"].instructions)
    text = instructions.read_text()
    healthy = tmp_path / "healthy.md"
    healthy.write_text(text)
    config.procedures["security-two"] = config.procedures["security-two"].model_copy(
        update={"instructions": str(healthy)})
    instructions.unlink()
    procedures.advance_rhythms(now=100)
    assert "Rhythm broken admission failed" in caplog.text
    assert len(state.tasks.all()) == 1
    instructions.write_text(text)
    procedures.advance_rhythms(now=101)
    assert len(state.tasks.all()) == 2


def settled_pass_spawns(tmp_path, backlog):
    """Count the task-store Git processes one idle rhythm pass starts."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _, state, _, _, procedures, _ = quiet_harness(tmp_path)
    for n in range(backlog):
        state.tasks.create(TaskSpec("app", f"settled {n}", "Do the work."))
    procedures.advance_rhythms(now=0)
    spawned, original = [], state.tasks.git
    state.tasks.git = lambda *a, **k: (spawned.append(a[0]), original(*a, **k))[1]
    procedures.advance_rhythms(now=300)
    return spawned


def test_an_idle_rhythm_pass_does_not_grow_with_the_backlog(tmp_path):
    """The lane runs every pass; its cost must not be the tasks ever accepted."""
    small = settled_pass_spawns(tmp_path / "small", 2)
    large = settled_pass_spawns(tmp_path / "large", 12)

    assert len(large) == len(small)
    assert "show" not in large


def test_an_idle_rhythm_pass_asks_the_task_namespace_once(tmp_path):
    spawned = settled_pass_spawns(tmp_path, 12)

    assert spawned.count("for-each-ref") == 1


def test_failed_activity_fetch_skips_sample_and_retries_next_poll(tmp_path, caplog):
    clone, state, runner, _, procedures, _ = quiet_harness(tmp_path)
    procedures.advance_rhythms(now=0)
    sha = commit(clone, "pending.txt")
    procedures.advance_rhythms(now=10)
    previous = dict(procedures._quiet)
    # Remove the actual remote: exercise fetch and its Git stderr, not a stub
    # that could hide a fixture failure before the operation under test.
    from pathlib import Path
    remote = Path(runner.transports["app"].remote_url)
    unavailable = remote.with_name("unavailable.git")
    remote.rename(unavailable)
    try:
        procedures.advance_rhythms(now=310)
        assert not state.tasks.all()
        assert procedures._quiet == previous
        assert "Rhythm activity sample failed" in caplog.text
        assert "controller Git fetch failed with exit 128" in caplog.text
        assert "does not appear to be a git repository" in caplog.text
    finally:
        unavailable.rename(remote)
    procedures.advance_rhythms(now=311)
    task = state.tasks.queued()[0]
    assert state.tasks.read(task)[1].procedure.candidate == sha
    procedures.advance_rhythms(now=312)
    assert len(state.tasks.all()) == 1


def test_activity_programming_fault_propagates(tmp_path, monkeypatch):
    _, _, runner, _, procedures, _ = quiet_harness(tmp_path)

    def broken_fetch():
        raise TypeError("activity defect")

    monkeypatch.setattr(runner.transports["app"], "fetch", broken_fetch)
    with pytest.raises(TypeError, match="activity defect"):
        procedures.advance_rhythms(now=0)


def test_failed_quiet_sample_does_not_skip_healthy_interval_rhythm(tmp_path):
    _, state, runner, config, procedures, _ = quiet_harness(tmp_path)
    # Quiet activity observes every repository, while the interval rhythm
    # only needs its own healthy input repository.
    runner.transports["offline"] = ControllerGitTransport(
        state.path, "offline", str(tmp_path / "missing.git"), "main", allow_local=True)
    config.rhythms["interval"] = ProcedureRhythmConfig(
        owner=None, schedule=100, procedure="security-one", input="repositories/app/main")
    procedures.advance_rhythms(now=100)
    task = state.tasks.queued()[0]
    assert state.tasks.read(task)[1].procedure.event == "rhythm:interval:1"
    assert len(state.tasks.all()) == 1
    assert procedures._quiet == {}
