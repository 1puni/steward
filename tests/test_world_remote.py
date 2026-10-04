"""A world with a remote takes in other writers' commits and publishes its own."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from steward_harness.config.schema import UntrustedExecutionConfig, WorldConfig
from steward_harness.git_reconcile import ResolverTurn
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.lease import Lease
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import StateDatabase
from steward_harness.world.git_world import GitWorld
from steward_harness.world.turn_checkpoint import WorldContentConflict, WorldTurnCheckpoint


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@x", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(root: Path, path: str, text: str) -> str:
    (root / path).write_text(text)
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", f"write {path}", cwd=root)
    return _git("rev-parse", "HEAD", cwd=root)


@pytest.fixture
def setup(tmp_path: Path):
    """A world, its bare remote, and a second clone that writes like the Mac."""
    remote = tmp_path / "remote.git"
    world = tmp_path / "world"
    _git("init", "-q", "-b", "main", "--bare", str(remote), cwd=tmp_path)
    _git("init", "-q", "-b", "main", str(world), cwd=tmp_path)
    _commit(world, "shared.md", "one\ntwo\nthree\n")
    _git("push", "-q", str(remote), "HEAD:refs/heads/main", cwd=world)
    other = tmp_path / "other"
    _git("clone", "-q", str(remote), str(other), cwd=tmp_path)
    return tmp_path, remote, world, other


def _checkpoint(tmp_path: Path, world: Path, remote: Path, resolve=lambda _turn: None):
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    return WorldTurnCheckpoint(
        GitWorld(world, execution_broker=broker),
        Lease(tmp_path / "lock", timeout_seconds=5.0),
        tmp_path / "world-turns",
        execution_broker=broker,
        state=StateDatabase(tmp_path / "state.db"),
        resolve_turn=resolve,
        transport=ControllerGitTransport(
            tmp_path / "state.db", "world", str(remote), "main", allow_local=True),
    )


def _remote_main(remote: Path) -> str:
    return _git("rev-parse", "refs/heads/main", cwd=remote)


def test_an_accepted_turn_is_published(setup) -> None:
    tmp_path, remote, world, _other = setup
    head = _commit(world, "turn.md", "GG wrote this\n")

    assert _checkpoint(tmp_path, world, remote).converge() == head
    assert _remote_main(remote) == head


def test_another_writers_push_fast_forwards_the_world(setup) -> None:
    tmp_path, remote, world, other = setup
    pushed = _commit(other, "voice.md", "Guru wrote this\n")
    _git("push", "-q", "origin", "main", cwd=other)

    assert _checkpoint(tmp_path, world, remote).converge() is None
    assert _git("rev-parse", "HEAD", cwd=world) == pushed
    assert (world / "voice.md").read_text() == "Guru wrote this\n"


def test_concurrent_writers_on_different_files_are_merged_and_published(setup) -> None:
    tmp_path, remote, world, other = setup
    theirs = _commit(other, "voice.md", "Guru\n")
    _git("push", "-q", "origin", "main", cwd=other)
    ours = _commit(world, "turn.md", "GG\n")

    published = _checkpoint(tmp_path, world, remote).converge()

    assert published == _git("rev-parse", "HEAD", cwd=world) == _remote_main(remote)
    parents = _git("log", "-1", "--format=%P", published, cwd=world).split()
    assert set(parents) == {ours, theirs}
    assert not list((tmp_path / "world-turns").glob("integration-*"))


def test_an_unresolved_conflict_leaves_world_and_remote_untouched(setup) -> None:
    tmp_path, remote, world, other = setup
    _commit(other, "shared.md", "one\nMAC\nthree\n")
    _git("push", "-q", "origin", "main", cwd=other)
    theirs = _remote_main(remote)
    ours = _commit(world, "shared.md", "one\nGG\nthree\n")

    with pytest.raises(WorldContentConflict, match="shared.md"):
        _checkpoint(tmp_path, world, remote).converge()

    assert _git("rev-parse", "HEAD", cwd=world) == ours
    assert _remote_main(remote) == theirs
    assert not list((tmp_path / "world-turns").glob("integration-*"))


def test_a_resolved_conflict_is_published(setup) -> None:
    tmp_path, remote, world, other = setup
    _commit(other, "shared.md", "one\nMAC\nthree\n")
    _git("push", "-q", "origin", "main", cwd=other)
    _commit(world, "shared.md", "one\nGG\nthree\n")
    turns: list[ResolverTurn] = []

    def resolve(turn: ResolverTurn) -> None:
        turns.append(turn)
        (turn.worktree / "shared.md").write_text("one\nGG\nMAC\nthree\n")

    published = _checkpoint(tmp_path, world, remote, resolve).converge()

    assert [turn.conflicted_files for turn in turns] == [("shared.md",)]
    assert _remote_main(remote) == published
    assert (world / "shared.md").read_text() == "one\nGG\nMAC\nthree\n"


def test_nothing_new_on_either_side_publishes_nothing(setup) -> None:
    tmp_path, remote, world, _other = setup
    before = _remote_main(remote)

    assert _checkpoint(tmp_path, world, remote).converge() is None
    assert _remote_main(remote) == before


def test_world_without_remote_does_not_converge(setup) -> None:
    tmp_path, _remote, world, _other = setup
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    checkpoint = WorldTurnCheckpoint(
        GitWorld(world, execution_broker=broker), Lease(tmp_path / "lock"),
        tmp_path / "world-turns", execution_broker=broker,
        state=StateDatabase(tmp_path / "state.db"),
    )

    assert checkpoint.converge() is None


def test_world_remote_config_validates_branch() -> None:
    assert WorldConfig(root="/srv/world", remote_url="git@github.com:o/w.git").branch == "main"
    with pytest.raises(ValidationError):
        WorldConfig(root="/srv/world", remote_url="git@github.com:o/w.git", branch="-x")
