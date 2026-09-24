"""The accepted Git namespace, rather than a work branch, grants task intake."""
import pytest

from steward_harness.state import TaskId, TaskSpec
from steward_harness.task_store import GitTaskStore
from test_task_runner_kernel import _git


def test_identity_is_unambiguous_across_repositories_and_source_replay(tmp_path):
    store = GitTaskStore(tmp_path / "tasks.git")
    first, _ = store.create(TaskSpec("app", "Same title", "First request"), source="event:one")
    second, _ = store.create(TaskSpec("other", "Same title", "Second request"), source="event:two")
    assert first != second
    replay, created = store.create(TaskSpec("app", "Same title", "First request"), source="event:one")
    assert replay == first and not created
    assert len(store.all()) == 2


def test_remote_decision_divergence_is_not_silently_overwritten(tmp_path):
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote), cwd=tmp_path)
    first = GitTaskStore(tmp_path / "one.git")
    second = GitTaskStore(tmp_path / "two.git")
    first.remote = second.remote = str(remote)
    task, _ = first.create(TaskSpec("app", "Inspect", "The complete request"))
    first.sync()
    second.sync()
    first.input(task, "note", "First concurrent decision")
    second.input(task, "note", "Second concurrent decision")
    first.sync()
    local = second.read(task)[0]
    with pytest.raises(RuntimeError, match="divergent task decisions"):
        second.sync()
    assert second.read(task)[0] == local
    assert [text for _, _, text, _ in second.get(task).pending] == ["Second concurrent decision"]


def test_authorized_ref_still_requires_a_valid_document(tmp_path):
    store = GitTaskStore(tmp_path / "tasks.git")
    blob = store.git("hash-object", "-w", "--stdin", input_text="No durable request schema")
    tree = store.git("mktree", input_text=f"100644 blob {blob}\ttask.md\n")
    sha = store.git("commit-tree", tree, input_text="invalid task")
    store.git("update-ref", "refs/heads/tasks/bad-task", sha)
    with pytest.raises(ValueError, match="frontmatter"):
        store.read(TaskId("bad-task"))
