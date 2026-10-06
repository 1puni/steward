"""Git/operator journeys for collapsed integration and executable targets."""
from task_tool_fixtures import TaskOutput
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json

from steward_harness.config.schema import ProcedureConfig, ProcedureRhythmConfig, TargetConfig, CommandSpec
from steward_harness.procedures import Procedures
from steward_harness.targets import Targets
from steward_harness.state import TaskSpec, TaskStatus
from test_git_tasks import harness
from test_task_no_changes import InvestigationAdapter
from test_task_runner_kernel import _git, _repository


def test_two_old_base_tasks_conflict_resolves_in_owning_cognition_then_linear_publish(tmp_path):
    bare, clone = _repository(tmp_path)
    (clone / "intent.txt").write_text("original\n")
    _git("add", ".", cwd=clone)
    _git("commit", "-m", "original intent", cwd=clone)
    _git("push", "origin", "main", cwd=clone)
    original_main = _git("rev-parse", "main", cwd=bare)
    adapter = InvestigationAdapter()
    state, runner, statuses, reconciler = harness(tmp_path / "state", bare, clone, adapter)
    first, _ = state.tasks.create(TaskSpec("app", "First", "Preserve first intent", priority=1))
    second, _ = state.tasks.create(TaskSpec("app", "Second", "Preserve second intent"))
    execute = adapter.execute
    def edit(request):
        worktree = Path(request.cwd)
        if "Repair attempt:" in request.prompt:
            base = state.tasks.read(second)[1].repair.base
            # Scripted native cognition resolves the real conflict. The publisher
            # never executes this reasoning or edits candidate content.
            import subprocess
            subprocess.run(["git", "merge", "--no-commit", base], cwd=worktree, capture_output=True)
            (worktree / "intent.txt").write_text("first and second\n")
            _git("add", "intent.txt", cwd=worktree)
            _git("commit", "-m", "preserve both intents", cwd=worktree)
        else:
            (worktree / "intent.txt").write_text("first\n" if str(first) in request.prompt else "second\n")
        return execute(request)
    adapter.execute = edit
    runner.prepare(first)
    runner.prepare(second)
    originals = [state.tasks.get(task).work_sha for task in (first, second)]
    assert reconciler.publish_repository("app") == first
    assert reconciler.publish_repository("app") is None
    assert "repair queued" in reconciler.last_outcome["app"]
    assert state.tasks.queued() == (second,)
    runner.prepare(second)
    assert "Rebase conflict" in adapter.requests[-1].prompt
    assert reconciler.publish_repository("app") == second
    candidate = state.tasks.get(second).landed
    assert _git("show", "main:intent.txt", cwd=bare) == "first and second"
    assert _git("rev-list", "--count", f"{original_main}..main", cwd=bare) == "2"
    assert _git("rev-list", "--min-parents=2", f"{original_main}..main", cwd=bare) == ""
    assert candidate == _git("rev-parse", "main", cwd=bare)
    for task, original in zip((first, second), originals):
        assert state.tasks.contains(original, state.tasks.read(task)[0])
    assert state.tasks.get(second).status is TaskStatus.DONE


def setup_procedures(tmp_path, state, runner):
    instructions = tmp_path / "review.md"
    instructions.write_text("Review changes for security. State a verdict with supporting evidence.")
    config = SimpleNamespace(procedures={
        name: ProcedureConfig(instructions=str(instructions), provider="claude",
                              model={"model": model}, access="read-only")
        for name, model in (("security-one", "model-one"), ("security-two", "model-two"))
    }, rhythms={}, targets={}, repositories=runner.repositories)
    return config, Procedures(config, state, runner.transports)


def test_target_requires_exact_input_and_procedure_evidence_and_external_observation(tmp_path):
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, _, _ = harness(tmp_path / "state", bare, clone, adapter)
    execute = adapter.execute
    adapter.execute = lambda request: replace(execute(request), output=TaskOutput('VERDICT: PASS', subject='review', disposition='idle'))
    config, procedures = setup_procedures(tmp_path, state, runner)
    driver = tmp_path / "driver"
    observed = tmp_path / "observed.json"
    driver.write_text("#!/usr/bin/env python3\n" + f'''import json, sys
from pathlib import Path
request = json.load(sys.stdin)
path = Path({str(observed)!r})
if sys.argv[1] == "observe":
    print(path.read_text() if path.exists() else json.dumps(dict(revision=None, ready=False)))
else:
    path.write_text(json.dumps(dict(revision=request["revision"], ready=True)))
''')
    driver.chmod(0o755)
    config.targets["production"] = TargetConfig(ref="repositories/app/main", driver=str(driver), requires=tuple(config.procedures))
    targets = Targets(config, state, runner.transports, procedures)
    assert "awaiting" in targets.advance("production")
    tasks = state.tasks.queued()
    assert len(tasks) == 2
    for task in tasks:
        runner.prepare(task)
    assert {request.resolved.model for request in adapter.requests} == {"model-one", "model-two"}
    assert all(request.sandbox_mode == "read-only" for request in adapter.requests)
    assert "satisfied" in targets.advance("production")
    assert not state.tasks.queued()
    (clone / "new.txt").write_text("new candidate")
    _git("add", ".", cwd=clone)
    _git("commit", "-m", "move desired ref", cwd=clone)
    _git("push", "origin", "main", cwd=clone)
    assert "awaiting" in targets.advance("production")
    assert len(state.tasks.queued()) == 2
    # Instructions are accepted policy too. Same candidate, changed instructions
    # requires another pair of independent runs.
    Path(config.procedures["security-one"].instructions).write_text("New review obligations")
    assert "awaiting" in targets.advance("production")
    assert len(state.tasks.queued()) == 4


def test_blocked_target_keeps_observing_without_applying_or_sticking_to_old_ref(tmp_path):
    bare, clone = _repository(tmp_path)
    state, runner, _, _ = harness(tmp_path / "state", bare, clone, InvestigationAdapter())
    config, procedures = setup_procedures(tmp_path, state, runner)
    initial = _git("rev-parse", "HEAD", cwd=clone)
    applied = tmp_path / "applied"
    driver = tmp_path / "driver"
    driver.write_text("#!/usr/bin/env python3\n" + f'''import json, sys
from pathlib import Path
request = json.load(sys.stdin)
path = Path({str(applied)!r})
if sys.argv[1] == "observe":
    print(json.dumps(dict(revision=path.read_text() if path.exists() else None,
        ready=path.exists(), blocked=request["revision"] == {initial!r},
        details="operator repair required")))
else:
    path.write_text(request["revision"])
''')
    driver.chmod(0o755)
    config.targets["production"] = TargetConfig(ref="repositories/app/main", driver=str(driver))
    targets = Targets(config, state, runner.transports, procedures)
    for _ in range(3):
        assert "blocked at" in targets.advance("production")
    assert not applied.exists()
    _git("commit", "--allow-empty", "-m", "new desired revision", cwd=clone)
    _git("push", "origin", "main", cwd=clone)
    assert "satisfied" in targets.advance("production")
    assert applied.read_text() == _git("rev-parse", "HEAD", cwd=clone)


def test_rhythm_runs_same_procedure_as_a_task(tmp_path):
    bare, clone = _repository(tmp_path)
    state, runner, _, _ = harness(tmp_path / "state", bare, clone, InvestigationAdapter())
    config, procedures = setup_procedures(tmp_path, state, runner)
    config.rhythms["weekly"] = ProcedureRhythmConfig(owner=None, schedule=604800, procedure="security-one", input="repositories/app/main")
    procedures.advance_rhythms(now=700000)
    procedures.advance_rhythms(now=700001)
    assert len(state.tasks.queued()) == 1
    task = state.tasks.get(state.tasks.queued()[0])
    assert "security-one" in task.brief


def test_workspace_write_rhythm_executes_maintenance_and_publishes_ordinary_work(tmp_path):
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, statuses, reconciler = harness(tmp_path / "state", bare, clone, adapter)
    config, procedures = setup_procedures(tmp_path, state, runner)
    instructions = tmp_path / "maintenance.md"
    instructions.write_text("Refresh maintenance.txt with the verified maintenance result.")
    config.procedures["maintenance"] = ProcedureConfig(instructions=str(instructions), provider="claude",
                                                       model={"model": "maintenance-model"}, access="workspace-write")
    config.rhythms["daily"] = ProcedureRhythmConfig(owner=None, schedule=86400, procedure="maintenance", input="repositories/app/main")
    execute = adapter.execute
    def maintain(request):
        assert instructions.read_text() in request.prompt
        assert "VERDICT:" not in request.prompt
        assert "Review the complete candidate tree" not in request.prompt
        assert request.sandbox_mode == "workspace-write"
        assert request.resolved.model == "maintenance-model"
        (Path(request.cwd) / "maintenance.txt").write_text("verified maintenance result")
        return execute(request)
    adapter.execute = maintain
    procedures.advance_rhythms(now=100000)
    task = state.tasks.queued()[0]
    runner.prepare(task)
    assert state.tasks.get(task).verdict is None
    procedures.advance_rhythms(now=200000)
    assert len(state.tasks.all()) == 1  # still owes publication
    assert reconciler.publish_repository("app") == task
    assert _git("show", "main:maintenance.txt", cwd=bare) == "verified maintenance result"
    assert state.tasks.get(task).status is TaskStatus.DONE


def test_rhythm_reopened_idle_blocks_overlap_and_cancellation_allows_future_bucket(tmp_path):
    from steward_harness.task_lock import task_lock
    bare, clone = _repository(tmp_path)
    state, runner, statuses, _ = harness(tmp_path / "state", bare, clone, InvestigationAdapter())
    config, procedures = setup_procedures(tmp_path, state, runner)
    config.rhythms["review"] = ProcedureRhythmConfig(owner=None, schedule=100, procedure="security-one", input="repositories/app/main")
    procedures.advance_rhythms(now=100)
    task = state.tasks.queued()[0]
    runner.prepare(task)
    # A pending input reopens the idle review for another slice.
    state.tasks.input(task, "note", "Review the additional evidence")
    procedures.advance_rhythms(now=200)
    assert len(state.tasks.all()) == 1
    state.tasks.cancel(task)
    with task_lock(runner.state.tasks.locks_root, task):
        procedures.advance_rhythms(now=300)
        assert len(state.tasks.all()) == 1
    procedures.advance_rhythms(now=150)
    assert len(state.tasks.all()) == 1
    procedures.advance_rhythms(now=300)
    assert len(state.tasks.all()) == 2


def _transcribing(adapter, *, also_write=None):
    """Make the adapter append a session transcript the way a real provider does."""
    execute = adapter.execute

    def run(request):
        result = execute(request)
        projects = Path(request.cwd) / "artefacts" / result.resolved.provider / "projects" / "a"
        projects.mkdir(parents=True, exist_ok=True)
        (projects / "session.jsonl").write_text('{"turn": 1}\n')
        if also_write is not None:
            (Path(request.cwd) / also_write).write_text("review must not change product")
        return replace(result, output=TaskOutput('VERDICT: PASS', subject='review', disposition='idle'))

    adapter.execute = run


def test_read_only_procedure_accepts_its_own_session_transcript(tmp_path):
    """A reviewer necessarily writes its transcript into the tree it is reading.

    Providers map their session directory into the worktree deliberately, so
    rejecting that change made the rhythm unsatisfiable by retry rather than
    catching anything.
    """
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, _, _ = harness(tmp_path / "state", bare, clone, adapter)
    _, procedures = setup_procedures(tmp_path, state, runner)
    candidate = runner.transports["app"].fetch()
    task = procedures.request("security-one", "app", candidate, candidate)
    _transcribing(adapter)
    runner.prepare(task)
    assert "changed its input" not in (state.tasks.get(task).reason or "")
    prompt = adapter.requests[0].prompt
    assert "Do not stage or commit" in prompt
    assert "harness writes and commits the account" in prompt
    assert "yours to write in as well" not in prompt
    assert "Commit cohesive progress" not in prompt
    assert "## Durable repository knowledge" not in prompt


def test_read_only_procedure_still_rejects_product_mutation_beside_a_transcript(tmp_path):
    """The excuse is scoped to the provider's own paths, not to the whole run."""
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, _, _ = harness(tmp_path / "state", bare, clone, adapter)
    _, procedures = setup_procedures(tmp_path, state, runner)
    candidate = runner.transports["app"].fetch()
    task = procedures.request("security-one", "app", candidate, candidate)
    _transcribing(adapter, also_write="injected.txt")
    runner.prepare(task)
    assert "changed its input" in state.tasks.get(task).reason
    assert state.tasks.get(task).verdict is None


def test_interrupted_read_only_review_is_retried_at_its_exact_candidate(tmp_path):
    """A review has no work of its own: what a failed run leaves is never committed.

    The interrupt path autosaves. Committing what a failed provider left in the
    checkout made the task branch differ from the candidate, so every retry was
    refused before cognition ran and the review could only be cancelled.
    """
    from steward_harness.runtime.contracts import RuntimeExecutionError
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, _, _ = harness(tmp_path / "state", bare, clone, adapter)
    _, procedures = setup_procedures(tmp_path, state, runner)
    candidate = runner.transports["app"].fetch()
    task = procedures.request("security-one", "app", candidate, candidate)
    _transcribing(adapter)
    transcribe = adapter.execute
    def sandbox_failed(request):
        transcribe(request)
        (Path(request.cwd) / "partial.txt").write_text("left by the failed run")
        record = Path(request.cwd) / "artefacts" / "codex" / "sessions" / "rollout-failed.jsonl"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{}\n")
        raise RuntimeExecutionError("provider sandbox could not start")
    adapter.execute = sandbox_failed
    runner.prepare(task)
    assert "could not start" in state.tasks.get(task).reason
    branch = state.tasks.get(task).branch
    agent = runner.repositories["app"].path

    def reviewed(revision):
        """The tree a review saw: everything but the harness's native records."""
        return [line for line in _git("ls-tree", "-r", revision, cwd=agent).splitlines()
                if not line.split("\t")[1].startswith("artefacts/")]

    assert reviewed(branch) == reviewed(candidate)
    # The failed run's native record is evidence, and stays on the branch.
    assert "artefacts/codex/sessions/rollout-failed.jsonl" in _git("ls-tree", "-r", "--name-only", branch, cwd=agent)

    adapter.execute = transcribe
    state.tasks.retry(task, "Run the review again")
    count = len(adapter.requests)
    runner.prepare(task)
    assert len(adapter.requests) == count + 1
    assert state.tasks.get(task).verdict == "pass"
    assert procedures.require(("security-one",), "app", candidate, candidate) == ""
    assert reviewed(branch) == reviewed(candidate)
    assert "artefacts/codex/sessions/rollout-failed.jsonl" in _git("ls-tree", "-r", "--name-only", branch, cwd=agent)


def test_failed_requirement_returns_the_reviewers_findings(tmp_path):
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, _, _ = harness(tmp_path / "state", bare, clone, adapter)
    _, procedures = setup_procedures(tmp_path, state, runner)
    candidate = runner.transports["app"].fetch()
    task = procedures.request("security-one", "app", candidate, candidate)
    execute = adapter.execute
    adapter.execute = lambda request: replace(execute(request), output=(
        TaskOutput('The consumer handoff is unverified.\nVERDICT: FAIL', subject='review', disposition='idle')))
    runner.prepare(task)
    failure = procedures.require(("security-one",), "app", candidate, candidate)
    assert "The consumer handoff is unverified." in failure


def test_read_only_procedure_rejects_product_mutation_and_retries_at_the_candidate(tmp_path):
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, _, _ = harness(tmp_path / "state", bare, clone, adapter)
    _, procedures = setup_procedures(tmp_path, state, runner)
    candidate = runner.transports["app"].fetch()
    task = procedures.request("security-one", "app", candidate, candidate)
    execute = adapter.execute
    def corrupt(request):
        (Path(request.cwd) / "injected.txt").write_text("review must not change product")
        return replace(execute(request), output=TaskOutput('VERDICT: PASS', subject='review', disposition='idle'))
    adapter.execute = corrupt
    runner.prepare(task)
    assert "changed its input" in state.tasks.get(task).reason
    assert state.tasks.get(task).verdict is None
    state.tasks.retry(task, "Resume the review")
    seen = []
    def review(request):
        seen.append((Path(request.cwd) / "injected.txt").exists())
        return replace(execute(request), output="VERDICT: PASS\nCOMMIT: review\nDISPOSITION: idle\nQUESTION: NONE")
    adapter.execute = review
    runner.prepare(task)
    # The rejected edit was never committed; the retry reviews the candidate.
    assert seen == [False]
    assert procedures.require(("security-one",), "app", candidate, candidate) == ""
    assert _git("rev-parse", "main", cwd=bare) == candidate


def test_signed_artifact_target_verifies_delivered_content_and_signature(tmp_path):
    """Local delivery boundary simulation; no device engagement or live APK release."""
    import subprocess, sys
    bare, clone = _repository(tmp_path)
    state, runner, _, _ = harness(tmp_path / "state", bare, clone, InvestigationAdapter())
    config, procedures = setup_procedures(tmp_path, state, runner)
    private, public = tmp_path / "signing.pem", tmp_path / "verify.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                    "-out", str(private)], check=True, capture_output=True)
    private.chmod(0o600)
    subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)], check=True, capture_output=True)
    delivered = tmp_path / "recipient"
    delivered.mkdir()
    driver = tmp_path / "signed-delivery"
    driver.write_text(f"#!{sys.executable}\n" + f'''import hashlib, json, subprocess, sys
from pathlib import Path
r=json.load(sys.stdin)
root=Path({str(delivered)!r})
payload,manifest,signature=(root/name for name in ('artifact.tar','manifest.json','signature'))
if sys.argv[1]=='apply':
    payload.write_bytes(subprocess.check_output(['git','--git-dir='+r['git_dir'],'archive',r['revision']]))
    manifest.write_text(json.dumps(dict(revision=r['revision'],digest=hashlib.sha256(payload.read_bytes()).hexdigest())))
    subprocess.run(['openssl','dgst','-sha256','-sign',{str(private)!r},'-out',str(signature),str(manifest)],check=True)
else:
    ready=False
    revision=None
    if all(p.exists() for p in (payload,manifest,signature)):
        verified=subprocess.run(['openssl','dgst','-sha256','-verify',{str(public)!r},'-signature',str(signature),str(manifest)],capture_output=True).returncode==0
        content=json.loads(manifest.read_text())
        if verified and content['digest']==hashlib.sha256(payload.read_bytes()).hexdigest():
            revision,ready=content['revision'],True
    print(json.dumps(dict(revision=revision,ready=ready)))
''')
    driver.chmod(0o755)
    config.targets["artifact"] = TargetConfig(ref="repositories/app/main", driver=str(driver))
    targets = Targets(config, state, runner.transports, procedures)
    revision = _git("rev-parse", "main", cwd=bare)
    assert "satisfied" in targets.advance("artifact")
    assert targets.call("artifact", "observe", "app", revision).revision == revision
    (delivered / "artifact.tar").write_bytes(b"tampered after delivery")
    assert not targets.call("artifact", "observe", "app", revision).ready
    targets.call("artifact", "apply", "app", revision)
    (delivered / "signature").write_bytes(b"forged signature")
    assert not targets.call("artifact", "observe", "app", revision).ready


def test_owned_rhythm_notification_delivers_without_assessment(tmp_path):
    from test_world_durability import runtime, EditingCognition
    from steward_harness.state import ConversationId
    import pytest
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, statuses, _ = harness(tmp_path, bare, clone, adapter)
    config, procedures = setup_procedures(tmp_path, state, runner)
    owner = ConversationId("desk:steward")
    config.rhythms["reflection"] = ProcedureRhythmConfig(owner=str(owner), schedule=100,
        procedure="security-one", input="repositories/app/main")
    execute = adapter.execute
    adapter.execute = lambda request: replace(execute(request), output=(
        TaskOutput('The private consumer handoff is still outstanding.', subject='reflection findings', notify='The private consumer handoff is still outstanding.')))
    procedures.advance_rhythms(now=100)
    reflection = state.tasks.queued()[0]
    runner.prepare(reflection)
    assert state.tasks.read(reflection)[1].owner == str(owner)
    assert state.lineage(owner) is None  # configured owner needs no prior local session
    world_state, checkpoint, service, cognition = runtime(tmp_path, EditingCognition(), repositories={"app"})
    service._state.tasks.transports = runner.transports
    assert world_state.pending_task_result_conversations() == (owner,)
    def unavailable(text, key):
        # The retained run's message is sent before any assessment.
        assert text.startswith("The private consumer handoff is still outstanding.")
        assert "Investigation saved." not in text
        assert "Task done:" not in text
        receipt = world_state.result_receipt(key)
        assert receipt["notification_source"] == str(reflection)
        raise OSError("transport temporarily unavailable")
    with pytest.raises(OSError, match="temporarily unavailable"):
        service.deliver_task_result(owner, send=unavailable)
    assert not (checkpoint.world.root / "decision.md").exists()
    followups = [task for task in state.tasks.all() if task.task_id != reflection]
    assert not followups
    sent=[]
    service.deliver_task_result(owner, send=lambda text, key: sent.append((text, key)))
    assert len(sent) == 1 and cognition.calls == 0
    assert len(state.tasks.all()) == 1  # transport retry cannot repeat admission
    assert not world_state.pending_task_result_conversations()


def test_quiet_rhythm_result_is_retained_evidence_and_sends_nothing(tmp_path):
    from test_world_durability import runtime, EditingCognition
    from steward_harness.state import ConversationId
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, statuses, _ = harness(tmp_path, bare, clone, adapter)
    config, procedures = setup_procedures(tmp_path, state, runner)
    owner = ConversationId("desk:steward")
    config.rhythms["reflection"] = ProcedureRhythmConfig(owner=str(owner), schedule=100,
        procedure="security-one", input="repositories/app/main")
    execute = adapter.execute
    adapter.execute = lambda request: replace(execute(request), output=(
        TaskOutput('', subject='no material change since the previous reflection', disposition='idle')))
    procedures.advance_rhythms(now=100)
    quiet = state.tasks.queued()[0]
    runner.prepare(quiet)
    assert "recorded only" in adapter.requests[-1].prompt
    assert state.tasks.get(quiet).status is TaskStatus.DONE
    assert "no material change" in state.tasks.git(
        "show", "-s", "--format=%s", state.tasks.read(quiet)[1].work)
    world_state, _, service, cognition = runtime(tmp_path, EditingCognition(), repositories={"app"})
    service._state.tasks.transports = runner.transports
    assert world_state.pending_task_result_conversations() == ()
    sent = []
    assert service.deliver_task_result(owner, send=lambda text, key: sent.append(text)) is None
    assert sent == [] and cognition.calls == 0
    # Findings that do not ask to notify are evidence too, not a message.
    adapter.execute = lambda request: replace(execute(request), output=(
        TaskOutput("Nothing material has changed, so I'm reporting no findings.", subject='reflection with no material change', disposition='idle')))
    for number, name in ((1, "handoff.txt"), (2, "handoff.txt")):
        (clone / name).write_text(f"changed {number}\n")
        _git("add", ".", cwd=clone)
        _git("commit", "-m", f"handoff {number}", cwd=clone)
        _git("push", "origin", "main", cwd=clone)
        procedures.advance_rhythms(now=100 * (number + 1))
        runner.prepare(state.tasks.queued()[0])
        if number == 1:
            assert world_state.pending_task_result_conversations() == ()
            assert service.deliver_task_result(owner, send=lambda text, key: sent.append(text)) is None
            assert sent == [] and cognition.calls == 0
            # The same owner still hears a finding it is asked to hear.
            adapter.execute = lambda request: replace(execute(request), output=(
                TaskOutput('A new consumer depends on the unpublished handoff.', subject='reflection findings', notify='A new consumer depends on the unpublished handoff.')))
    assert world_state.pending_task_result_conversations() == (owner,)
    service.deliver_task_result(owner, send=lambda text, key: sent.append(text))
    assert cognition.calls == 0 and len(sent) == 1
    assert sent[0].startswith("A new consumer depends on the unpublished handoff.")


def test_rhythm_notification_cannot_trigger_assessment_side_effects(tmp_path):
    from test_world_durability import runtime, EditingCognition
    from steward_harness.state import ConversationId
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, statuses, _ = harness(tmp_path, bare, clone, adapter)
    config, procedures = setup_procedures(tmp_path, state, runner)
    owner = ConversationId("desk:steward")
    config.rhythms["reflection"] = ProcedureRhythmConfig(owner=str(owner), schedule=100,
        procedure="security-one", input="repositories/app/main")
    execute = adapter.execute
    adapter.execute = lambda request: replace(execute(request), output=(
        TaskOutput('The public consumer still reads the old feed.', subject='steward: inspect consumer', notify='The public consumer still reads the old feed.')))
    procedures.advance_rhythms(now=100)
    runner.prepare(state.tasks.queued()[0])
    world_state, checkpoint, service, _ = runtime(
        tmp_path, EditingCognition(repository="unconfigured"), repositories={"app"})
    service._state.tasks.transports = runner.transports
    sent=[]
    service.deliver_task_result(owner, send=lambda text, key: sent.append(text))
    assert not (checkpoint.world.root / "decision.md").exists()
    assert len(state.tasks.all()) == 1
    assert sent == ["The public consumer still reads the old feed."]
    with world_state.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM turns WHERE operator_id='harness:task-result'").fetchone() is None



def test_scheduled_review_scope_does_not_become_full_tree_release_evidence(tmp_path):
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, statuses, _ = harness(tmp_path / "state", bare, clone, adapter)
    config, procedures = setup_procedures(tmp_path, state, runner)
    instructions = "Inspect changed obligations and repair progress; reuse unchanged evidence."
    Path(config.procedures["security-one"].instructions).write_text(instructions)
    config.rhythms["progress"] = ProcedureRhythmConfig(owner=None, schedule=100,
        procedure="security-one", input="repositories/app/main")
    execute = adapter.execute
    adapter.execute = lambda request: replace(execute(request), output=(
        TaskOutput('No changed obligations.\nVERDICT: PASS', subject='progress checked', disposition='idle')))
    procedures.advance_rhythms(now=100)
    scheduled = state.tasks.queued()[0]
    runner.prepare(scheduled)
    assert instructions in adapter.requests[0].prompt
    assert "Review the complete candidate tree" not in adapter.requests[0].prompt
    definition = state.tasks.read(scheduled)[1]
    assert state.tasks.get(scheduled).verdict == "pass"
    assert state.tasks.get(scheduled).status is TaskStatus.DONE
    # Same procedure and exact input, but progress evidence cannot satisfy a
    # release requirement. The existing distinct source identity makes it fresh.
    assert procedures.require(["security-one"], "app", definition.procedure.candidate,
                              definition.procedure.base) is None
    required = state.tasks.queued()[0]
    assert required != scheduled
    runner.prepare(required)
    assert "Review the complete candidate tree" in adapter.requests[-1].prompt
    assert procedures.require(["security-one"], "app", definition.procedure.candidate,
                              definition.procedure.base) == ""
