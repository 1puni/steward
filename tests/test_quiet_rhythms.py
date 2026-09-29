"""A rhythm admits a run only for input none of its finished runs has seen."""
from dataclasses import replace
import os
import subprocess

import pytest
from pydantic import ValidationError

from steward_harness.config.schema import ProcedureRhythmConfig
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.procedures import Procedures
from steward_harness.state import StateDatabase, TaskSpec
from test_git_tasks import harness
from test_rewrite_convergence import setup_procedures
from test_task_no_changes import InvestigationAdapter
from test_task_runner_kernel import _git, _repository


def quiet_harness(tmp_path):
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
    procedures = Procedures(config, state, runner.transports)
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


def refresh(runner):
    """What the daemon's target observations do between rhythm buckets."""
    for transport in runner.transports.values():
        transport.fetch()


def peer(tmp_path, state, runner):
    root = tmp_path / "peer"
    root.mkdir()
    bare, clone = _repository(root)
    from steward_harness.config.schema import RepositoryConfig
    runner.repositories["peer"] = RepositoryConfig(path=str(clone), remote_url=str(bare))
    runner.transports["peer"] = ControllerGitTransport(state.path, "peer", str(bare), "main", allow_local=True)
    return clone


def test_quiet_rhythm_runs_once_its_input_settles_and_never_again_while_unchanged(tmp_path):
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    procedures.advance_rhythms(now=0)
    sha = commit(clone, "changed.txt")
    refresh(runner)
    procedures.advance_rhythms(now=100)
    procedures.advance_rhythms(now=399)
    assert not state.tasks.all()
    procedures.advance_rhythms(now=400)
    task = state.tasks.queued()[0]
    run = state.tasks.read(task)[1].procedure
    assert run.candidate == sha and run.activity is None and ":quiet:" in run.event
    runner.prepare(task)
    assert state.tasks.get(task).verdict == "fail"
    for now in range(700, 12300, 300):
        procedures.advance_rhythms(now=now)
    reopened = StateDatabase(state.path)
    restarted = Procedures(config, reopened, runner.transports)
    for now in (13000, 14000):
        restarted.advance_rhythms(now=now)
    assert len(reopened.tasks.all()) == len(adapter.requests) == 1
    # A new commit is new input: it runs once it has been quiet for the period.
    newer = commit(clone, "newer.txt")
    refresh(runner)
    restarted.advance_rhythms(now=14100)
    restarted.advance_rhythms(now=14399)
    assert len(reopened.tasks.all()) == 1
    restarted.advance_rhythms(now=14400)
    assert reopened.tasks.read(reopened.tasks.queued()[0])[1].procedure.candidate == newer
    # An explicit request remains a new task on unchanged inputs.
    manual = restarted.request(run.name, "app", run.candidate, run.base, event="manual:requested")
    assert manual != task


def test_interval_rhythm_admits_only_when_its_input_changed(tmp_path):
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    config.rhythms = {"hourly": ProcedureRhythmConfig(owner=None, schedule=100,
        procedure="security-one", input="repositories/app/main")}
    procedures.advance_rhythms(now=100)
    first = state.tasks.queued()[0]
    runner.prepare(first)
    for now in (150, 200, 300, 450):
        procedures.advance_rhythms(now=now)
    assert len(state.tasks.all()) == len(adapter.requests) == 1
    sha = commit(clone, "changed.txt")
    refresh(runner)
    procedures.advance_rhythms(now=460)
    second = state.tasks.queued()[0]
    assert state.tasks.read(second)[1].procedure.event == "rhythm:hourly:4"
    assert state.tasks.read(second)[1].procedure.candidate == sha
    runner.prepare(second)
    # At most one run per interval, however far the input moves inside it.
    commit(clone, "again.txt")
    procedures.advance_rhythms(now=470)
    assert len(state.tasks.all()) == 2
    procedures.advance_rhythms(now=500)
    assert len(state.tasks.all()) == 3


def test_repository_rhythm_is_not_retriggered_by_steward_bookkeeping(tmp_path):
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    other = peer(tmp_path, state, runner)
    config.rhythms["daily"] = ProcedureRhythmConfig(owner=None, schedule=86400,
        procedure="security-two", input="repositories/app/main")
    procedures.advance_rhythms(now=0)
    procedures.advance_rhythms(now=300)
    for task in state.tasks.queued():
        runner.prepare(task)
    assert len(state.tasks.all()) == 2
    # Accepting and executing ordinary work, a sibling rhythm's evidence and
    # another repository's commits are not this repository's input.
    product, _ = state.tasks.create(TaskSpec("app", "Investigate", "Investigate a real obligation."))
    runner.prepare(product)
    commit(other, "elsewhere.txt")
    for now in (400, 800, 86400, 86800):
        procedures.advance_rhythms(now=now)
    assert len(state.tasks.all()) == 3


def test_org_rhythm_input_is_every_remote_head_and_ordinary_task_work(tmp_path):
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    peer_clone = peer(tmp_path, state, runner)
    anchor = _git("rev-parse", "HEAD", cwd=clone)
    config.rhythms = {"org": ProcedureRhythmConfig(owner=None, schedule=100, workdir=str(tmp_path),
        procedure="security-one", input="repositories/app/main")}
    procedures.advance_rhythms(now=100)
    first = state.tasks.queued()[0]
    assert state.tasks.read(first)[1].procedure.activity == {
        "repositories/app/main": anchor,
        "repositories/peer/main": _git("rev-parse", "HEAD", cwd=peer_clone)}
    runner.prepare(first)
    # Its own evidence and the acceptance of new work are bookkeeping.
    procedures.advance_rhythms(now=200)
    product, _ = state.tasks.create(TaskSpec("app", "Investigate", "Investigate a real obligation."))
    procedures.advance_rhythms(now=300)
    assert len(state.tasks.all()) == 2
    # Accepted native work is input, though the anchor has not moved.
    runner.prepare(product)
    procedures.advance_rhythms(now=400)
    second = state.tasks.queued()[0]
    run = state.tasks.read(second)[1]
    assert run.procedure.candidate == anchor
    assert run.procedure.activity[f"tasks/{product}"] == state.tasks.read(product)[1].work
    runner.prepare(second)
    account = adapter.requests[-1].prompt
    assert "Execute security-one across the organisation" in account
    assert f"tasks/{product}" not in account and "repositories/peer" not in account
    # A branch pushed anywhere is input; deleting one is not.
    sha = commit(peer_clone, "feature.txt", push=False)
    _git("push", "origin", f"{sha}:refs/heads/feature", cwd=peer_clone)
    procedures.advance_rhythms(now=500)
    third = state.tasks.queued()[0]
    assert state.tasks.read(third)[1].procedure.activity["repositories/peer/feature"] == sha
    runner.prepare(third)
    _git("push", "origin", ":refs/heads/feature", cwd=peer_clone)
    procedures.advance_rhythms(now=600)
    assert len(state.tasks.all()) == 4


def test_org_rhythm_paths_narrow_its_input_to_what_it_is_for(tmp_path):
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    peer_clone = peer(tmp_path, state, runner)
    config.rhythms = {"org": ProcedureRhythmConfig(owner=None, schedule=100, workdir=str(tmp_path),
        procedure="security-one", input="repositories/app/main", paths=("repositories/peer/main",))}
    procedures.advance_rhythms(now=100)
    first = state.tasks.queued()[0]
    assert state.tasks.read(first)[1].procedure.activity == {
        "repositories/peer/main": _git("rev-parse", "HEAD", cwd=peer_clone)}
    runner.prepare(first)
    # Accepted task work and a branch outside the paths are readable, not input.
    product, _ = state.tasks.create(TaskSpec("app", "Investigate", "Investigate a real obligation."))
    runner.prepare(product)
    sha = commit(peer_clone, "feature.txt", push=False)
    _git("push", "origin", f"{sha}:refs/heads/feature", cwd=peer_clone)
    procedures.advance_rhythms(now=200)
    procedures.advance_rhythms(now=300)
    assert len(state.tasks.all()) == 2
    # The line it watches moving is.
    moved = commit(peer_clone, "release.txt")
    procedures.advance_rhythms(now=400)
    second = state.tasks.queued()[0]
    assert state.tasks.read(second)[1].procedure.activity == {"repositories/peer/main": moved}


def test_org_rhythm_paths_fetch_only_the_repositories_they_name(tmp_path):
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    peer(tmp_path, state, runner)
    fetched = []
    for name, transport in runner.transports.items():
        fetch = transport.fetch
        transport.fetch = lambda name=name, fetch=fetch: (fetched.append(name), fetch())[1]
    rhythm = ProcedureRhythmConfig(owner=None, schedule=100, workdir=str(tmp_path),
        procedure="security-one", input="repositories/app/main")
    procedures.observe(rhythm)
    assert sorted(fetched) == ["app", "peer"]
    # The input repository is always read; a repository no path names is not.
    fetched.clear()
    _, candidate, _, activity = procedures.observe(rhythm.model_copy(update={"paths": ("tasks/",)}))
    assert fetched == ["app"] and activity == {}
    assert candidate == _git("rev-parse", "HEAD", cwd=clone)
    fetched.clear()
    procedures.observe(rhythm.model_copy(update={"paths": ("repositories/peer/",)}))
    assert sorted(fetched) == ["app", "peer"]


def _spy_fetches(runner):
    fetched = []
    for name, transport in runner.transports.items():
        fetch = transport.fetch
        transport.fetch = lambda name=name, fetch=fetch: (fetched.append(name), fetch())[1]
    return fetched


def _followed_by_a_target(config, repository):
    from steward_harness.config.schema import TargetConfig

    config.targets["site"] = TargetConfig(ref=f"repositories/{repository}/main", driver="/bin/true")


def test_an_idle_bucket_fetches_nothing_across_many_passes(tmp_path):
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    _followed_by_a_target(config, "app")
    config.rhythms = {"hourly": ProcedureRhythmConfig(owner=None, schedule=3600,
        procedure="security-one", input="repositories/app/main")}
    procedures.advance_rhythms(now=3600)
    runner.prepare(state.tasks.queued()[0])
    fetched = _spy_fetches(runner)
    # The pass runs every few seconds; one bucket's first observation fetches
    # the input once, and the target's own observation refreshes it after that.
    for now in range(7200, 10800, 60):
        procedures.advance_rhythms(now=now)
    assert fetched == ["app"]
    assert len(state.tasks.all()) == 1


def test_new_input_the_daemon_fetched_admits_a_run_without_a_rhythm_fetch(tmp_path):
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    _followed_by_a_target(config, "app")
    config.rhythms = {"hourly": ProcedureRhythmConfig(owner=None, schedule=3600,
        procedure="security-one", input="repositories/app/main")}
    procedures.advance_rhythms(now=3600)
    runner.prepare(state.tasks.queued()[0])
    procedures.advance_rhythms(now=7200)
    sha = commit(clone, "shipped.txt")
    procedures.advance_rhythms(now=7300)
    assert len(state.tasks.all()) == 1  # Pushed, but nothing has fetched it yet.
    runner.transports["app"].fetch()  # The target's observation.
    fetched = _spy_fetches(runner)
    procedures.advance_rhythms(now=7400)
    run = state.tasks.read(state.tasks.queued()[0])[1].procedure
    assert run.candidate == sha and run.event == "rhythm:hourly:2"
    assert fetched == []


def test_a_repository_no_target_follows_is_fetched_once_per_bucket(tmp_path):
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    peer_clone = peer(tmp_path, state, runner)
    _followed_by_a_target(config, "app")
    config.rhythms = {"org": ProcedureRhythmConfig(owner=None, schedule=3600, workdir=str(tmp_path),
        procedure="security-one", input="repositories/app/main", paths=("repositories/peer/main",))}
    procedures.advance_rhythms(now=3600)
    runner.prepare(state.tasks.queued()[0])
    fetched = _spy_fetches(runner)
    procedures.advance_rhythms(now=7200)
    moved = commit(peer_clone, "release.txt")
    for now in range(7260, 10800, 60):
        procedures.advance_rhythms(now=now)
    # Nothing else fetches peer, so the rhythm did, once, and saw the old tip.
    assert sorted(fetched) == ["app", "peer"] and len(state.tasks.all()) == 1
    # The next bucket's fetch sees the move: the rhythm is late, never blind.
    procedures.advance_rhythms(now=10800)
    run = state.tasks.read(state.tasks.queued()[0])[1].procedure
    assert run.activity == {"repositories/peer/main": moved}
    assert sorted(fetched) == ["app", "app", "peer", "peer"]


def _org_config(tmp_path, paths):
    from test_world_rhythms import _config

    config = _config(tmp_path, tmp_path / "world").model_dump()
    config["repositories"] = {"app": {"path": str(tmp_path / "app"),
                                      "remote_url": "https://example.com/app.git"}}
    config["procedures"]["review"] = config["procedures"]["sleep"] | {"access": "read-only"}
    config["rhythms"] = {"org": {"schedule": 100, "procedure": "review", "input": "repositories/app/main",
                                 "workdir": str(tmp_path), "owner": None, "paths": paths}}
    return config


@pytest.mark.parametrize("paths", [["repositories/app/main"], ["repositories/app/"], ["tasks/"]])
def test_org_rhythm_paths_accept_configured_repositories_and_tasks(tmp_path, paths):
    from steward_harness.config.schema import StewardConfig

    StewardConfig.model_validate(_org_config(tmp_path, paths))


@pytest.mark.parametrize("paths", [["repositories/typo/main"], ["episodes/"]])
def test_org_rhythm_paths_refuse_a_prefix_that_would_silence_it(tmp_path, paths):
    from steward_harness.config.schema import StewardConfig

    with pytest.raises(ValidationError, match="must name tasks/ or a configured"):
        StewardConfig.model_validate(_org_config(tmp_path, paths))


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
    refresh(runner)
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


def test_restart_with_changed_work_waits_full_quiet_and_daily_recurs_on_new_input(tmp_path):
    clone, state, runner, config, procedures, _ = quiet_harness(tmp_path)
    config.rhythms["daily"] = ProcedureRhythmConfig(owner=None, schedule=86400,
        procedure="security-two", input="repositories/app/main")
    procedures.advance_rhythms(now=0)
    daily = state.tasks.queued()[0]
    runner.prepare(daily)
    commit(clone, "first.txt")
    refresh(runner)
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


def test_large_activity_stays_in_git_metadata_and_reflection_starts_at_org_root(tmp_path):
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    root = clone.parent
    config.rhythms["light"] = config.rhythms["light"].model_copy(update={"workdir": str(root)})
    procedures.advance_rhythms(now=0)
    sha = commit(clone, "new-evidence.txt")
    # A real organisation's many branches exceed the old brief limit.
    _git("push", "origin", *(f"{sha}:refs/heads/feature-{i}" for i in range(200)), cwd=clone)
    refresh(runner)
    procedures.advance_rhythms(now=10)
    procedures.advance_rhythms(now=310)
    task = state.tasks.queued()[0]
    definition = StateDatabase(state.path).tasks.read(task)[1]
    assert definition.procedure.workdir == str(root)
    assert all(definition.procedure.activity[f"repositories/app/feature-{i}"] == sha for i in range(200))
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
    restarted = Procedures(config, StateDatabase(state.path), runner.transports)
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
    procedures.advance_rhythms(now=100)
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


def test_failed_input_fetch_skips_the_poll_and_retries_next_poll(tmp_path, caplog):
    clone, state, runner, _, procedures, _ = quiet_harness(tmp_path)
    procedures.advance_rhythms(now=0)
    sha = commit(clone, "pending.txt")
    refresh(runner)
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
        assert "Rhythm light input unavailable this poll" in caplog.text
        assert "controller Git fetch failed with exit 128" in caplog.text
        assert "does not appear to be a git repository" in caplog.text
    finally:
        unavailable.rename(remote)
    procedures.advance_rhythms(now=311)
    task = state.tasks.queued()[0]
    assert state.tasks.read(task)[1].procedure.candidate == sha
    procedures.advance_rhythms(now=312)
    assert len(state.tasks.all()) == 1


def test_input_programming_fault_propagates(tmp_path, monkeypatch):
    _, _, runner, _, procedures, _ = quiet_harness(tmp_path)

    def broken_fetch():
        raise TypeError("activity defect")

    monkeypatch.setattr(runner.transports["app"], "fetch", broken_fetch)
    with pytest.raises(TypeError, match="activity defect"):
        procedures.advance_rhythms(now=0)


def test_failed_org_sample_does_not_skip_healthy_interval_rhythm(tmp_path):
    _, state, runner, config, procedures, _ = quiet_harness(tmp_path)
    # The organisation rhythm observes every repository, while the interval
    # rhythm only needs its own healthy input repository.
    runner.transports["offline"] = ControllerGitTransport(
        state.path, "offline", str(tmp_path / "missing.git"), "main", allow_local=True)
    config.rhythms["light"] = config.rhythms["light"].model_copy(update={"workdir": str(tmp_path)})
    config.rhythms["interval"] = ProcedureRhythmConfig(
        owner=None, schedule=100, procedure="security-one", input="repositories/app/main")
    procedures.advance_rhythms(now=100)
    task = state.tasks.queued()[0]
    assert state.tasks.read(task)[1].procedure.event == "rhythm:interval:1"
    assert len(state.tasks.all()) == 1
    assert procedures._quiet == {}


def test_procedure_run_falls_back_through_the_canonical_provider_order(tmp_path):
    _, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    # The procedure prefers a provider that cannot run; the canonical
    # fallback order still reaches one that can, with that provider's model.
    config.procedures["security-one"] = config.procedures["security-one"].model_copy(
        update={"provider": "codex"})
    runner.provider_fallbacks = ("claude",)
    config.rhythms["interval"] = ProcedureRhythmConfig(
        owner=None, schedule=100, procedure="security-one", input="repositories/app/main")
    procedures.advance_rhythms(now=100)
    task = state.tasks.queued()[0]
    runner.prepare(task)
    assert state.tasks.get(task).status.value == "done"
    assert adapter.requests[-1].resolved.provider == "claude"
    assert adapter.requests[-1].resolved.model != "model-one"


def test_a_pinned_procedure_refuses_every_other_provider(tmp_path):
    _, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    config.procedures["security-one"] = config.procedures["security-one"].model_copy(
        update={"provider": "codex", "fallback": False})
    runner.provider_fallbacks = ("claude",)
    config.rhythms["interval"] = ProcedureRhythmConfig(
        owner=None, schedule=100, procedure="security-one", input="repositories/app/main")
    procedures.advance_rhythms(now=100)
    task = state.tasks.queued()[0]
    assert state.tasks.read(task)[1].procedure.fallback is False
    runner.prepare(task)
    assert state.tasks.get(task).status.value == "blocked"
    assert adapter.requests == []


def test_blocked_rhythm_run_is_superseded_by_the_next_interval(tmp_path):
    _, state, runner, config, procedures, _ = quiet_harness(tmp_path)
    config.rhythms["interval"] = ProcedureRhythmConfig(
        owner=None, schedule=100, procedure="security-one", input="repositories/app/main")
    procedures.advance_rhythms(now=100)
    blocked = state.tasks.queued()[0]
    state.tasks.hold(blocked, "blocked", "No provider can satisfy this turn")
    # The same interval does not re-fire or clear the evidence of its block.
    procedures.advance_rhythms(now=150)
    assert [t.status.value for t in state.tasks.all()] == ["blocked"]
    # A blocked run reported nothing, so its unchanged input is still new.
    procedures.advance_rhythms(now=200)
    assert state.tasks.get(blocked).status.value == "cancelled"
    assert state.tasks.get(blocked).reason == "superseded by rhythm:interval:2"
    fresh = state.tasks.queued()[0]
    assert fresh != blocked
    assert state.tasks.read(fresh)[1].procedure.event == "rhythm:interval:2"


def test_a_running_or_waiting_rhythm_run_still_prevents_overlap(tmp_path):
    _, state, runner, config, procedures, _ = quiet_harness(tmp_path)
    config.rhythms["interval"] = ProcedureRhythmConfig(
        owner=None, schedule=100, procedure="security-one", input="repositories/app/main")
    procedures.advance_rhythms(now=100)
    queued = state.tasks.queued()[0]
    procedures.advance_rhythms(now=200)
    assert [str(t.task_id) for t in state.tasks.all()] == [str(queued)]


def test_org_rhythm_paths_decide_whether_its_input_ref_wakes_it(tmp_path):
    clone, state, runner, config, procedures, adapter = quiet_harness(tmp_path)
    peer(tmp_path, state, runner)
    config.rhythms = {"org": ProcedureRhythmConfig(owner=None, schedule=100, workdir=str(tmp_path),
        procedure="security-one", input="repositories/app/main", paths=("repositories/peer/main",))}
    procedures.advance_rhythms(now=100)
    runner.prepare(state.tasks.queued()[0])
    # Its input ref moving is not what it is for.
    commit(clone, "company.txt")
    procedures.advance_rhythms(now=200)
    assert len(state.tasks.all()) == 1
    # Unless its paths say so.
    config.rhythms["org"] = config.rhythms["org"].model_copy(
        update={"paths": ("repositories/peer/main", "repositories/app/main")})
    procedures.advance_rhythms(now=300)
    assert len(state.tasks.queued()) == 1
