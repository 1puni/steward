"""Operator journeys over real Git, with a scripted native provider."""
from dataclasses import replace
from pathlib import Path

import pytest

from steward_harness.cognition import Cognition
from steward_harness.config.schema import RepositoryConfig, UntrustedExecutionConfig
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.repository_reconciler import RepositoryReconciler
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import ConversationId, StateDatabase, TaskId, TaskSpec, TaskStatus
from steward_harness.task_runner import TaskRunner
from steward_harness.task_store import GitTaskStore
from test_task_no_changes import InvestigationAdapter
from test_task_runner_kernel import _git, _repository


def harness(root, bare, clone, adapter):
    root.mkdir(exist_ok=True)
    state = StateDatabase(root / "state.db")
    state.tasks.default_provider = "claude"
    repository = RepositoryConfig(path=str(clone), remote_url=str(bare))
    transport = ControllerGitTransport(root / "state.db", "app", str(bare), "main", allow_local=True)
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    runner = TaskRunner(state=state, repositories={"app": repository}, transports={"app": transport},
                        broker=broker, worktrees_root=root / "worktrees", cognition=Cognition({"claude": adapter}),
                        provider_fallbacks=(), timeout_seconds=30)
    reconciler = RepositoryReconciler(state=state, repositories=runner.repositories,
                                     transports=runner.transports, broker=broker,
                                     worktrees_root=runner.worktrees_root)
    return state, runner, state.tasks, reconciler


def git_only_task(tmp_path, remote):
    author = tmp_path / "task-author"
    _git("init", "-b", "main", str(author), cwd=tmp_path)
    _git("config", "user.name", "Operator", cwd=author)
    _git("config", "user.email", "operator@example.invalid", cwd=author)
    task_id = TaskId("task-" + "a" * 32)
    (author / "task.md").write_text("---\nversion: 1\nrepository: app\ntitle: Inspect consumer\nowner: telegram:17\n---\n\nFind the consumer and explain the result.\n")
    _git("add", "task.md", cwd=author)
    _git("commit", "-m", "Operator request", cwd=author)
    _git("push", str(remote), f"HEAD:refs/heads/tasks/{task_id}", cwd=author)
    return task_id


def test_git_only_intake_fresh_execution_and_result_routing(tmp_path):
    bare, clone = _repository(tmp_path)
    task_remote = tmp_path / "tasks.git"
    _git("init", "--bare", str(task_remote), cwd=tmp_path)
    task_id = git_only_task(tmp_path, task_remote)
    adapter = InvestigationAdapter()
    state, runner, statuses, reconciler = harness(tmp_path / "fresh", bare, clone, adapter)
    state.tasks.remote = str(task_remote)
    state.tasks.sync()
    assert state.lineage(ConversationId.for_task(task_id)) is None
    with state.connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name IN ('tasks','task_state')").fetchall() == []
    assert state.tasks.queued() == (task_id,)
    runner.prepare(task_id)
    assert "Find the consumer" in adapter.requests[0].prompt
    assert reconciler.publish_repository("app") == task_id
    assert state.tasks.get(task_id).status is TaskStatus.DONE
    # The pushed candidate is on the remote; its import ref did its job.
    assert reconciler.transports["app"]._run("for-each-ref", "refs/steward/candidates/") == ""
    owner = state.pending_task_result_conversations()[0]
    assert str(owner) == "telegram:17"
    result = state.pending_task_result_for(owner)
    assert result and "Landed SHA:" in result[1] and "Evidence: src/feed.py." in result[1]
    # A second harness has no old lineage, SQL task, worktree, or agent task ref.
    state.tasks.sync()
    clone2 = tmp_path / "repo2"
    _git("clone", str(bare), str(clone2), cwd=tmp_path)
    other, _, fresh_statuses, _ = harness(tmp_path / "second", bare, clone2, InvestigationAdapter())
    other.tasks.remote = str(task_remote)
    other.tasks.sync()
    fresh_statuses.transports["app"].fetch()
    assert not other.tasks.queued()
    assert other.tasks.get(task_id).status is TaskStatus.DONE
    assert other.lineage(ConversationId.for_task(task_id)) is None


def test_answer_retry_cancel_and_pending_input_survive_native_sessions(tmp_path):
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter("ask")
    state, runner, statuses, _ = harness(tmp_path / "state", bare, clone, adapter)
    task_id, _ = state.tasks.create(TaskSpec("app", "Inspect", "Durable request"))
    runner.prepare(task_id)
    assert state.tasks.get(task_id).status is TaskStatus.WAITING
    state.tasks.answer(task_id, "Use the indexed consumer.")
    assert state.tasks.queued() == (task_id,)
    assert tuple((k, t) for _, k, t, _ in state.tasks.get(task_id).pending) == (("answer", "Use the indexed consumer."),)
    state.tasks.cancel(task_id, "Change of scope")
    assert state.tasks.cancelled(task_id)
    assert state.tasks.queued() == ()
    state.tasks.retry(task_id, "Keep the earlier answer.")
    adapter.disposition = "idle"
    with state.connect(write=True) as connection:
        connection.execute("DELETE FROM conversations")
    runner.prepare(task_id)
    assert not tuple((k, t) for _, k, t, _ in state.tasks.get(task_id).pending)
    assert "Use the indexed consumer" in adapter.requests[-1].prompt
    # A fresh session is given every accepted input, consumed ones included.
    assert "Keep the earlier answer" in adapter.requests[-1].prompt
    assert state.tasks.get(task_id).brief == "Durable request"
    assert not state.tasks.cancelled(task_id)


def test_concurrent_operator_note_is_retained_for_the_next_slice(tmp_path):
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, statuses, _ = harness(tmp_path / "state", bare, clone, adapter)
    task_id, _ = state.tasks.create(TaskSpec("app", "Revise plan", "Old plan: change the parser."))
    execute = adapter.execute
    def edit(request):
        state.tasks.note(task_id, "Also retain this late observation.")
        return execute(request)
    adapter.execute = edit
    runner.prepare(task_id)
    task = state.tasks.get(task_id)
    assert task.brief == "Old plan: change the parser."
    assert task.repository == "app"
    assert state.tasks.read(task_id)[1].owner is None
    assert state.tasks.queued() == (task_id,)
    assert tuple((k, t) for _, k, t, _ in state.tasks.get(task_id).pending)[0][1] == "Also retain this late observation."


def test_rebased_push_crash_before_retip_recovers_exact_candidate(tmp_path, monkeypatch):
    bare, clone = _repository(tmp_path)
    state, runner, statuses, reconciler = harness(tmp_path / "state", bare, clone, InvestigationAdapter())
    task_id, _ = state.tasks.create(TaskSpec("app", "Inspect", "Find the consumer."))
    runner.prepare(task_id)
    work = state.tasks.get(task_id).work_sha
    (clone / "other.txt").write_text("concurrent main change")
    _git("add", "other.txt", cwd=clone)
    _git("commit", "-m", "move main", cwd=clone)
    _git("push", "origin", "main", cwd=clone)
    import steward_harness.repository_reconciler as reconciler_module
    original = reconciler_module.publish
    def crash(*args):
        original(*args)
        raise RuntimeError("crash after remote push")
    monkeypatch.setattr(reconciler_module, "publish", crash)
    with pytest.raises(RuntimeError, match="crash after remote push"):
        reconciler.publish_repository("app")
    monkeypatch.setattr(reconciler_module, "publish", original)
    # Nothing was recorded after the push; the landing commit names its work.
    candidate = state.tasks.get(task_id).landed
    assert candidate is not None and candidate != work
    assert _git("rev-parse", f"tasks/{task_id}", cwd=clone) == work
    assert _git("rev-parse", "main", cwd=bare) == candidate
    assert state.tasks.get(task_id).status is TaskStatus.DONE
    # A subsequent poll must not create a second commit or push.
    reconciler.publish_repository("app")
    assert _git("rev-parse", "main", cwd=bare) == candidate


def test_task_status_and_decisions_do_not_open_sql(tmp_path, monkeypatch):
    """Task state is Git; only the provider session a slice ran on is SQL."""
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter("ask")
    state, runner, statuses, _ = harness(tmp_path / "state", bare, clone, adapter)
    task_id, _ = state.tasks.create(TaskSpec("app", "Inspect", "Standalone Git request"))
    runner.prepare(task_id)
    def sql_forbidden(*args, **kwargs):
        raise AssertionError("task operation opened SQL")
    monkeypatch.setattr(state, "connect", sql_forbidden)
    assert state.tasks.get(task_id).status is TaskStatus.WAITING
    state.tasks.note(task_id, "Retain context")
    state.tasks.answer(task_id, "Use the source")
    state.tasks.cancel(task_id, "Stop now")
    state.tasks.retry(task_id, "Continue safely")
    assert state.tasks.queued() == (task_id,)
    assert [kind for _, kind, _, _ in state.tasks.get(task_id).pending] == ["note", "answer", "retry"]


def test_product_work_ref_cannot_admit_or_publish_task(tmp_path):
    bare, clone = _repository(tmp_path)
    state, _, _, reconciler = harness(tmp_path / "state", bare, clone, InvestigationAdapter())
    base = _git("rev-parse", "main", cwd=bare)
    _git("checkout", "-b", "steward", cwd=clone)
    (clone / "unaccepted.txt").write_text("not an accepted request")
    _git("add", ".", cwd=clone)
    _git("commit", "-m", "unaccepted idle work\n\nDisposition: idle", cwd=clone)
    _git("branch", "tasks/agent-invented-task", cwd=clone)
    assert not state.tasks.queued()
    assert state.tasks.all() == []
    assert reconciler.publish_repository("app") is None
    assert _git("rev-parse", "main", cwd=bare) == base


def test_a_saved_session_that_proves_missing_still_sees_consumed_corrections(tmp_path):
    """Every slice prompt carries every accepted input, not only the pending ones."""
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter("ask")
    state, runner, _, _ = harness(tmp_path / "state", bare, clone, adapter)
    task_id, _ = state.tasks.create(TaskSpec("app", "Inspect", "Durable request"))
    runner.prepare(task_id)
    state.tasks.answer(task_id, "Scope correction: only the indexed consumer.")
    runner.prepare(task_id)
    assert state.tasks.get(task_id).pending == ()
    # A saved session exists now, yet the provider may start fresh.
    state.bind_conversation_provider(state.tasks.get(task_id).session_id, "claude", "saved")
    state.tasks.answer(task_id, "Also check the cache.")
    runner.prepare(task_id)
    assert len(adapter.requests) == 3
    assert "Scope correction: only the indexed consumer." in adapter.requests[-1].prompt
    assert "Also check the cache." in adapter.requests[-1].prompt


def test_control_bytes_in_accepted_text_cannot_break_the_task_record(tmp_path):
    store = GitTaskStore(tmp_path / "tasks.git")
    task, _ = store.create(TaskSpec("app", "Inspect", "The complete request"))
    store.note(task, "before\x01after\x00end")
    other, _ = store.create(TaskSpec("app", "Other", "Another request"))
    assert [text for _, _, text, _ in store.get(task).pending] == ["before\x01after\ufffdend"]
    assert {t.task_id for t in store.all()} == {task, other}


def test_a_running_first_slice_cannot_be_retargeted(tmp_path):
    from steward_harness.task_lock import task_lock
    store = GitTaskStore(tmp_path / "tasks.git")
    task, _ = store.create(TaskSpec("app", "Inspect", "The complete request"))
    with task_lock(store.locks_root, task):
        with pytest.raises(RuntimeError, match="unstarted"):
            store.retarget(task, "other")
    assert store.retarget(task, "other").repository == "other"


def test_work_merged_without_a_landing_commit_is_done_not_repaired(tmp_path):
    """An operator merge (or a landing from before the trailer) still lands the work."""
    bare, clone = _repository(tmp_path)
    adapter = InvestigationAdapter()
    state, runner, _, reconciler = harness(tmp_path / "state", bare, clone, adapter)
    task_id, _ = state.tasks.create(TaskSpec("app", "Inspect", "Find the consumer."))
    runner.prepare(task_id)
    work = state.tasks.get(task_id).work_sha
    _git("push", "-q", str(bare), f"{work}:refs/heads/main", cwd=clone)
    assert reconciler.publish_repository("app") is None
    task = state.tasks.get(task_id)
    assert task.status is TaskStatus.DONE and task.landed == work
    assert state.tasks.queued() == () and len(adapter.requests) == 1
