"""World conflict resolution remains separate from repository publication."""

from __future__ import annotations

from steward_harness.git_reconcile import ResolverTurn

import subprocess
from pathlib import Path

import pytest

from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.git_reconcile import reconcile_git
from steward_harness.landing.worktree import WorktreeManager
from steward_harness.runtime.execution import UntrustedExecutionBroker


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _remote_tree(remote: Path, name: str) -> str:
    return _git(f"--git-dir={remote}", "show", f"main:{name}", cwd=remote.parent)


@pytest.fixture()
def repo(tmp_path: Path) -> dict:
    bare = tmp_path / "origin.git"
    _git("init", "-q", "-b", "main", "--bare", str(bare), cwd=tmp_path)
    clone = tmp_path / "repo"
    _git("clone", "-q", str(bare), str(clone), cwd=tmp_path)
    _git("config", "user.name", "T", cwd=clone)
    _git("config", "user.email", "t@x", cwd=clone)
    (clone / "file.txt").write_text("base\n")
    _git("add", "file.txt", cwd=clone)
    _git("commit", "-q", "-m", "init", cwd=clone)
    _git("push", "-q", "-u", "origin", "main", cwd=clone)
    return {"clone": clone, "worktrees": tmp_path / "worktrees", "tmp": tmp_path}


def _conflicted(repo: dict, branch_side: str, main_side: str, branch: str = "steward/work") -> None:
    """Create a textual conflict between the branch and a moved main."""
    clone = repo["clone"]
    _git("checkout", "-q", "-b", branch, cwd=clone)
    (clone / "file.txt").write_text(branch_side)
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "branch edit", cwd=clone)
    _git("checkout", "-q", "main", cwd=clone)
    (clone / "file.txt").write_text(main_side)
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "main edit", cwd=clone)
    _git("push", "-q", "origin", "main", cwd=clone)


def _broker() -> UntrustedExecutionBroker:
    return UntrustedExecutionBroker(UntrustedExecutionConfig())


def _resolve(repo, resolve_turn=None, max_stops=20):
    """Exercise the world resolver directly, without a repository publisher."""
    manager = WorktreeManager(repo["clone"], repo["worktrees"], execution_broker=_broker())
    candidate = manager.create_worktree(
        "world", branch_name="world/candidate",
        base_sha=_git("rev-parse", "steward/work", cwd=repo["clone"]),
    )
    error = reconcile_git(
        candidate, _git("rev-parse", "main", cwd=repo["clone"]), "world/candidate",
        broker=_broker(), resolve_turn=resolve_turn, max_stops=max_stops,
    )
    return error, candidate


@pytest.mark.parametrize("resolution", ["edit", "delete", "markers", "bound"])
def test_world_resolution_and_failure_preserve_repository_input(repo, resolution):
    _conflicted(repo, "branch side\n", "main side\n")
    calls = []

    def resolve(turn: ResolverTurn):
        calls.append(turn)
        if resolution == "delete":
            (turn.worktree / "file.txt").unlink()
        elif resolution == "edit":
            (turn.worktree / "file.txt").write_text("both sides\n")

    # World reconciliation supplies controller identity, not local git config.
    _git("config", "--unset", "user.name", cwd=repo["clone"])
    _git("config", "--unset", "user.email", cwd=repo["clone"])
    _git("config", "commit.gpgsign", "true", cwd=repo["clone"])
    error, candidate = _resolve(repo, resolve, max_stops=0 if resolution == "bound" else 20)
    if resolution in ("edit", "delete"):
        assert error is None
        assert _git("show", "-s", "--format=%cn <%ce>", "HEAD", cwd=candidate) == "Steward <steward@localhost>"
        assert (candidate / "file.txt").exists() == (resolution == "edit")
    else:
        assert error and ("exhausted" if resolution == "bound" else "resolver output failed") in error
        assert (candidate / "file.txt").read_text() == "branch side\n"
    assert len(calls) == (0 if resolution == "bound" else 1)
    assert _remote_tree(repo["tmp"] / "origin.git", "file.txt") == "main side"
    assert _git("show", "steward/work:file.txt", cwd=repo["clone"]) == "branch side"


@pytest.mark.parametrize("failure", ["operational", "unavailable", "defect"])
def test_world_resolver_errors_are_bounded_but_defects_escape(repo, failure):
    from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeUnavailable

    _conflicted(repo, "branch side\n", "main side\n")

    def fail(turn):
        error = {"operational": RuntimeExecutionError, "unavailable": RuntimeUnavailable, "defect": ValueError}[failure]
        raise error("provider unavailable\nAuthorization: Bearer private-token\n" + "x" * 2000)

    if failure == "defect":
        with pytest.raises(ValueError):
            _resolve(repo, fail)
    else:
        error, _ = _resolve(repo, fail)
        assert "provider unavailable" in error
        assert "private-token" not in error
        assert len(error) < 1200


def test_world_stop_guard_bounds_multiple_conflicts(repo):
    clone = repo["clone"]
    names = ("first.txt", "second.txt", "third.txt")
    for name in names:
        (clone / name).write_text("base\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-qm", "base files", cwd=clone)
    _git("checkout", "-qb", "steward/work", cwd=clone)
    for name in names:
        (clone / name).write_text("work\n")
        _git("commit", "-qam", f"edit {name}", cwd=clone)
    _git("checkout", "-q", "main", cwd=clone)
    for name in names:
        (clone / name).write_text("remote\n")
    _git("commit", "-qam", "conflicting remote", cwd=clone)
    _git("push", "-q", "origin", "main", cwd=clone)
    calls = []

    def resolve(turn):
        calls.append(turn)
        for name in turn.conflicted_files:
            (turn.worktree / name).write_text("resolved\n")

    error, candidate = _resolve(repo, resolve, max_stops=2)
    assert error == "Rebase conflict: exhausted 2 resolution stops"
    assert len(calls) == 2
    for name in names:
        assert (candidate / name).read_text() == "work\n"
        assert _remote_tree(repo["tmp"] / "origin.git", name) == "remote"


@pytest.mark.parametrize('failure', ['return', 'snapshot', 'interrupt'])
def test_pending_native_evidence_survives_reconciliation_failure(repo, failure):
    from steward_harness.runtime.contracts import RuntimeExecutionError
    from steward_harness.runtime.native_evidence import EVIDENCE_PENDING

    clone = repo['clone']
    native_path = Path('artefacts/codex/sessions/fixture.jsonl')
    (clone / native_path).parent.mkdir(parents=True)
    (clone / native_path).write_text('old synthetic native evidence\n')
    (clone / '.gitignore').write_text(EVIDENCE_PENDING + '\n')
    _git('add', '-A', cwd=clone)
    _git('commit', '-qm', 'native fixture', cwd=clone)
    _conflicted(repo, 'branch side\n', 'main side\n')
    written = 'new synthetic tool result before failed snapshot\n'
    candidates = []

    def resolve(turn):
        candidates.append(turn.worktree)
        (turn.worktree / native_path).write_text(written)
        (turn.worktree / EVIDENCE_PENDING).touch()
        if failure == 'snapshot':
            raise RuntimeExecutionError('synthetic final snapshot failure')
        if failure == 'interrupt':
            raise KeyboardInterrupt()

    if failure == 'interrupt':
        with pytest.raises(KeyboardInterrupt):
            _resolve(repo, resolve)
    else:
        error, _ = _resolve(repo, resolve)
        assert error and ('pending' in error or 'snapshot failure' in error)
    candidate = candidates[0]
    assert (candidate / native_path).read_text() == written
    assert (candidate / EVIDENCE_PENDING).exists()
    rebase_state = Path(_git('rev-parse', '--path-format=absolute', '--git-path',
                             'rebase-merge', cwd=candidate))
    assert rebase_state.is_dir(), 'abort must not discard unarchived resolver evidence'
    error = reconcile_git(candidate, 'main', 'world/candidate', broker=_broker(),
                          resolve_turn=lambda _: pytest.fail('pending evidence must block retry'))
    assert error and 'pending' in error
    assert (candidate / native_path).read_text() == written
    assert rebase_state.is_dir()
