"""Citations retain exact evidence while making old and new history readable."""

import json
import subprocess

import pytest

from steward_harness.citations import GitCitations, cite
from steward_harness.cli import main


def git(repo, *args, input=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=input,
                          text=True, capture_output=True, check=True).stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2020-03-04T10:11:12+02:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2021-05-06T07:08:09+00:00")
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Original Author")
    git(tmp_path, "config", "user.email", "author@example.test")
    git(tmp_path, "remote", "add", "origin", "git@github.com:example/private-world.git")
    git(tmp_path, "commit", "--allow-empty", "-F", "-", input=(
        "steward: turn turn_original\n\nInput:\nKeep original writing.\n\n"
        "Reply:\n**Preserve originals before editing**\n\nDetails remain here.\n\n"
        "Steward-Turn: turn_original\nSteward-Base: old-base\nSteward-Source: telegram:1:2\n"))
    return tmp_path


def test_legacy_turn_and_hash_link_to_same_unmodified_original(world):
    original = git(world, "rev-parse", "HEAD")
    raw = git(world, "cat-file", "commit", original)
    refs = git(world, "show-ref")
    reader = GitCitations(world)
    turn = reader.resolve("turn_original")
    hashed = reader.resolve(original[:8])
    assert turn.url == hashed.url == f"https://github.com/example/private-world/commit/{original}"
    assert turn.title == "Preserve originals before editing"
    assert turn.subject == "steward: turn turn_original"
    assert turn.authored_at == "2020-03-04T10:11:12+02:00"
    assert turn.committed_at == git(world, "show", "-s", "--format=%cI", original)
    assert "2020-03-04 10:11 +0200" in turn.markdown()
    assert reader.history()[0] == turn
    assert git(world, "cat-file", "commit", original) == raw
    assert git(world, "show-ref") == refs
    assert git(world, "status", "--porcelain") == ""


def test_missing_and_ambiguous_turns_are_not_guessed(world):
    with pytest.raises(ValueError, match="no Steward-Turn"):
        cite("turn_missing", repo=world)
    git(world, "commit", "--allow-empty", "-m", "Duplicate\n\nSteward-Turn: turn_original")
    with pytest.raises(ValueError, match="ambiguous turn"):
        cite("turn_original", repo=world)
    assert "Preserve originals" in cite("turn_original", repo=world, revision="HEAD^")


def test_hash_is_an_object_identity_even_when_a_branch_has_that_name(world):
    original = git(world, "rev-parse", "HEAD")
    git(world, "commit", "--allow-empty", "-m", "Later unrelated work")
    git(world, "branch", original[:8], "HEAD")
    assert GitCitations(world).resolve(original[:8]).commit == original
    blob = git(world, "hash-object", "-w", "--stdin", input="not a commit")
    with pytest.raises(ValueError, match="not a commit"):
        GitCitations(world).resolve(blob)


def test_body_mentions_do_not_become_provenance(world):
    git(world, "commit", "--allow-empty", "-m",
        "Discuss turn_fake\n\nSteward-Turn: turn_fake\n\nAn ordinary quoted example.")
    with pytest.raises(ValueError, match="no Steward-Turn"):
        cite("turn_fake", repo=world)


def test_labels_escape_markdown_and_html(world):
    output = cite("turn_original", repo=world, label="V's [direction] <script> *now*\nagain")
    assert output.startswith("[V's \\[direction\\] &lt;script&gt; \\*now\\* again](https://")


def test_legacy_checkpoint_resolves_without_inventing_a_reply(world):
    git(world, "commit", "--allow-empty", "-m",
        "steward: checkpoint turn_older\n\nSteward-Reply-SHA256: retained-digest")
    item = GitCitations(world).resolve("turn_older")
    assert item.commit == git(world, "rev-parse", "HEAD")
    assert item.title == "Recorded turn"
    assert "2020-03-04" in item.markdown()


def test_task_uses_pinned_document_title_in_store_without_head(world):
    (world / "task.md").write_text("---\ntitle: Make history readable\nrepository: example\n---\nThe request.\n")
    git(world, "add", "task.md")
    git(world, "commit", "-m", "accept task")
    commit = git(world, "rev-parse", "HEAD")
    git(world, "branch", "tasks/task-example", commit)
    git(world, "symbolic-ref", "HEAD", "refs/heads/unborn")
    item = GitCitations(world).resolve("task-example")
    assert item.title == "Make history readable"
    assert item.url == f"https://github.com/example/private-world/blob/{commit}/task.md"


def test_cli_batches_before_printing_and_exposes_original_metadata(world, capsys):
    assert main(["cite", "turn_original", "--repo", str(world), "--json"]) == 0
    item = json.loads(capsys.readouterr().out)[0]
    assert item["subject"] == "steward: turn turn_original"
    assert item["authored_at"] != item["committed_at"]
    assert main(["cite", "turn_original", "turn_missing", "--repo", str(world)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no Steward-Turn" in captured.err
    assert main(["cite", "--history", "1", "--repo", str(world)]) == 0
    assert "Preserve originals before editing" in capsys.readouterr().out


@pytest.mark.parametrize("remote", ["https://token@github.com/a/b", "file:///tmp/repo", "https://github.com/a/b?token=x"])
def test_unsafe_or_non_web_remotes_do_not_become_links(world, remote):
    with pytest.raises(ValueError, match="repository URL"):
        cite("turn_original", repo=world, web_url=remote)
