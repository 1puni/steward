"""Landing engine: work-branch integration, conflicts, gates, fast-forward pushes."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from steward_harness.config.schema import (
    CommandSpec,
    RepositoryConfig,
    StewardConfig,
    UntrustedExecutionConfig,
)
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.landing.merger import Tested
from steward_harness.landing.checkpoint import (
    WorktreeCheckpointer,
    commit_subject,
    parse_tick_closure,
)
from steward_harness.landing.gates import GateRunner
from steward_harness.landing.merger import (
    ValidationFailure,
    PromotionEngine,
    publish,
)
from steward_harness.landing.worktree import WorktreeError, WorktreeManager
from steward_harness.runtime.execution import UntrustedExecutionBroker


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


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
    return {"clone": clone, "bare": bare, "worktrees": tmp_path / "worktrees"}


def _commit_all(repo_path: Path, message: str) -> None:
    _git("add", "-A", cwd=repo_path)
    _git("commit", "-q", "-m", message, cwd=repo_path)


def _branch(repo: dict, name: str) -> None:
    _git("checkout", "-q", "-b", name, cwd=repo["clone"])


def _push_main(repo: dict, clone: Path | None = None) -> None:
    _git("push", "-q", "origin", "main", cwd=clone or repo["clone"])


def _origin_main_tree(repo: dict, name: str) -> str:
    return _git(
        f"--git-dir={repo['bare']}", "show", f"main:{name}", cwd=repo["clone"]
    )


def _origin_git(repo: dict, *args: str) -> str:
    return _git(f"--git-dir={repo['bare']}", *args, cwd=repo["clone"])


def _config_for(repo: dict, tmp_path: Path, **kwargs) -> StewardConfig:
    repository = RepositoryConfig(
        path=str(repo["clone"]), remote_url=str(repo["bare"]), **kwargs
    )
    return StewardConfig(
        identity={"name": "t", "slug": "t"},
        provider={
            "state_db": str(tmp_path / "state.db"),
            "workdir": str(tmp_path / "work"),
        },
        repositories={"app": repository},
    )


def _transport(repo: dict, tmp_path: Path) -> ControllerGitTransport:
    return ControllerGitTransport(
        tmp_path / "state.db",
        "app",
        str(repo["bare"]),
        "main",
        allow_local=True,
    )


def _broker() -> UntrustedExecutionBroker:
    return UntrustedExecutionBroker(UntrustedExecutionConfig())


def _engine(
    repo: dict, tmp_path: Path, **kwargs,
) -> PromotionEngine:
    return PromotionEngine(
        _config_for(repo, tmp_path, **kwargs).repositories["app"],
        tmp_path / "worktrees",
        transport=_transport(repo, tmp_path),
        execution_broker=_broker(),
    )


def _prepare(engine: PromotionEngine, repo: dict, branch: str) -> Tested | ValidationFailure:
    base = engine.transport.sync_remote_to_agent(
        repo["clone"], engine.execution_broker
    )
    branch_tip = subprocess.run(
        ["git", "show-ref", "--verify", "--hash", f"refs/heads/{branch}"],
        cwd=repo["clone"],
        check=False,
        capture_output=True,
        text=True,
    )
    work = branch_tip.stdout.strip() if branch_tip.returncode == 0 else "0" * 40
    return engine.prepare(branch, work, base)


def _land(engine: PromotionEngine, repo: dict, branch: str) -> str | None:
    prepared = _prepare(engine, repo, branch)
    if isinstance(prepared, ValidationFailure):
        return prepared.reason
    return None if publish(engine.transport, prepared) else "not landed"


@pytest.mark.parametrize(
    ("proposal", "expected"),
    [
        ("COMMIT: feat: preserve work", "feat: preserve work"),
        ("COMMIT: NONE", "steward: Fallback title"),
        ("unparseable prose", "steward: Fallback title"),
    ],
)
def test_commit_subject_uses_a_deterministic_fallback(
    proposal: str, expected: str
) -> None:
    assert commit_subject(proposal, "Fallback title") == expected


def test_tick_closure_is_typed_and_rejects_questionless_ask() -> None:
    closure = parse_tick_closure(
        "COMMIT: feat: partial\nDISPOSITION: continue\nQUESTION: NONE",
        "Fallback",
    )
    assert closure.disposition == "continue"

    with pytest.raises(ValueError, match="requires a question"):
        parse_tick_closure(
            "COMMIT: feat: work\nDISPOSITION: ask\nQUESTION: NONE", "Fallback"
        )


@pytest.mark.parametrize("gate_cwd", ["..", "../../outside"])
def test_gate_working_directory_cannot_escape_candidate(
    tmp_path: Path, gate_cwd: str
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()

    result = GateRunner.run_command(
        CommandSpec(argv=("true",), cwd=gate_cwd),
        candidate,
        broker=_broker(),
    )

    assert result.passed is False
    assert "escapes candidate root" in result.stderr


def test_gate_rejects_a_dirty_candidate(repo: dict) -> None:
    (repo["clone"] / "dirty.txt").write_text("not committed\n")

    passed, results = GateRunner.run_all((), repo["clone"], broker=_broker())

    assert passed is False
    assert results[-1].command == GateRunner.INTEGRITY_CHECK
    assert "not clean" in results[-1].stderr


def test_gate_cannot_change_the_candidate_it_approves(repo: dict) -> None:
    gate = CommandSpec(
        argv=(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('changed-by-gate').write_text('unsafe')",
        )
    )

    passed, results = GateRunner.run_all(
        (gate,), repo["clone"], broker=_broker()
    )

    assert results[0].passed is True
    assert passed is False
    assert results[-1].command == GateRunner.INTEGRITY_CHECK
    assert "mutated its input" in results[-1].stderr


def test_rebase_lands_work_on_moved_main(repo: dict, tmp_path: Path) -> None:
    _branch(repo, "steward/work")
    (repo["clone"] / "work.txt").write_text("task work\n")
    _commit_all(repo["clone"], "task work")
    work_tip = _git("rev-parse", "steward/work", cwd=repo["clone"])

    _git("checkout", "-q", "main", cwd=repo["clone"])
    (repo["clone"] / "main.txt").write_text("main moved\n")
    _commit_all(repo["clone"], "main moved")
    _push_main(repo)

    engine = _engine(repo, tmp_path)
    tested = _prepare(engine, repo, "steward/work")
    assert isinstance(tested, Tested)
    assert publish(engine.transport, tested)
    assert _origin_main_tree(repo, "work.txt") == "task work"
    assert _origin_main_tree(repo, "main.txt") == "main moved"
    assert tested.tested_sha == _git(
        f"--git-dir={repo['bare']}", "rev-parse", "main", cwd=repo["clone"]
    )
    # The work branch itself is untouched.
    assert _git("rev-parse", "steward/work", cwd=repo["clone"]) == work_tip
    # History stays linear: no merge commits on main.
    merges = _git("log", "--merges", "--format=%H", "origin/main", cwd=repo["clone"])
    assert merges == ""


def test_plain_fast_forward_when_main_unmoved(repo: dict, tmp_path: Path) -> None:
    _branch(repo, "steward/work")
    (repo["clone"] / "work.txt").write_text("plain ff\n")
    _commit_all(repo["clone"], "plain work")
    result = _land(_engine(repo, tmp_path), repo, "steward/work")
    assert result is None
    assert _origin_main_tree(repo, "work.txt") == "plain ff"


def test_landing_does_not_run_repository_hooks(repo: dict, tmp_path: Path) -> None:
    _branch(repo, "steward/work")
    (repo["clone"] / "work.txt").write_text("hook-safe\n")
    _commit_all(repo["clone"], "work")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    sentinel = tmp_path / "hook-ran"
    script = f"#!/bin/sh\ntouch '{sentinel}'\n"
    for name in ("post-checkout", "pre-rebase", "post-rewrite", "pre-push"):
        hook = hooks / name
        hook.write_text(script)
        hook.chmod(0o755)
    _git("config", "core.hooksPath", str(hooks), cwd=repo["clone"])

    result = _land(_engine(repo, tmp_path), repo, "steward/work")

    assert result is None, result
    assert not sentinel.exists()


def test_checkpoint_does_not_run_hooks_or_fsmonitor(repo: dict, tmp_path: Path) -> None:
    root = repo["clone"]
    (root / "checkpoint.txt").write_text("safe\n")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    sentinel = tmp_path / "hook-ran"
    hook = hooks / "post-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
    hook.chmod(0o755)
    fsmonitor = tmp_path / "fsmonitor"
    fsmonitor.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
    fsmonitor.chmod(0o755)
    _git("config", "core.hooksPath", str(hooks), cwd=root)
    _git("config", "core.fsmonitor", str(fsmonitor), cwd=root)
    checkpoint = WorktreeCheckpointer(root, execution_broker=_broker())

    assert checkpoint.stage(expected_branch="main")
    checkpoint.commit("safe checkpoint")

    assert not sentinel.exists()


def test_recreating_worktree_preserves_existing_task_branch(repo: dict) -> None:
    manager = WorktreeManager(
        repo["clone"], repo["worktrees"], execution_broker=_broker()
    )
    first = manager.create_worktree(
        "task-1",
        branch_name="tasks/task-1",
        base_sha=_git("rev-parse", "HEAD", cwd=repo["clone"]),
    )
    (first / "retained.txt").write_text("kept\n")
    _commit_all(first, "retained task work")
    retained = _git("rev-parse", "HEAD", cwd=first)
    manager.remove_worktree("task-1")

    (repo["clone"] / "main-moved.txt").write_text("new base\n")
    _commit_all(repo["clone"], "advance main")
    _push_main(repo)

    recovered = manager.create_worktree("task-1", branch_name="tasks/task-1")
    try:
        assert _git("rev-parse", "HEAD", cwd=recovered) == retained
        assert (recovered / "retained.txt").read_text() == "kept\n"
        assert not (recovered / "main-moved.txt").exists()
    finally:
        manager.remove_worktree("task-1")


def test_worktree_manager_never_contacts_the_remote(repo: dict, tmp_path: Path) -> None:
    sentinel = tmp_path / "remote-contacted"
    upload_pack = tmp_path / "upload-pack"
    upload_pack.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 1\n")
    upload_pack.chmod(0o755)
    _git("config", "remote.origin.uploadpack", str(upload_pack), cwd=repo["clone"])

    manager = WorktreeManager(
        repo["clone"], repo["worktrees"], execution_broker=_broker()
    )
    manager.create_worktree(
        "local-only",
        branch_name="tasks/local-only",
        base_sha=_git("rev-parse", "HEAD", cwd=repo["clone"]),
    )
    try:
        assert not sentinel.exists()
    finally:
        manager.remove_worktree("local-only")


@pytest.mark.parametrize("base", [None, "origin/main"])
def test_new_worktree_requires_an_exact_base_sha(repo: dict, base: str | None) -> None:
    manager = WorktreeManager(
        repo["clone"], repo["worktrees"], execution_broker=_broker()
    )

    with pytest.raises(WorktreeError, match="exact lowercase SHA-1 base"):
        manager.create_worktree(
            "no-authority", branch_name="tasks/no-authority", base_sha=base
        )


def test_recreating_worktree_reuses_dirty_crash_leftover(repo: dict) -> None:
    manager = WorktreeManager(
        repo["clone"], repo["worktrees"], execution_broker=_broker()
    )
    first = manager.create_worktree(
        "task-crash",
        branch_name="tasks/task-crash",
        base_sha=_git("rev-parse", "HEAD", cwd=repo["clone"]),
    )
    (first / "partial.txt").write_text("recover me\n")

    recovered = manager.create_worktree(
        "task-crash", branch_name="tasks/task-crash"
    )
    try:
        assert recovered == first
        assert (recovered / "partial.txt").read_text() == "recover me\n"
        assert "?? partial.txt" in _git("status", "--short", cwd=recovered)
    finally:
        manager.remove_worktree("task-crash")


def test_recreating_worktree_refuses_unregistered_same_branch_repo(repo: dict) -> None:
    manager = WorktreeManager(
        repo["clone"], repo["worktrees"], execution_broker=_broker()
    )
    impostor = repo["worktrees"] / "task-impostor"
    impostor.mkdir(parents=True)
    _git("init", "-q", "-b", "tasks/task-impostor", cwd=impostor)
    (impostor / "keep.txt").write_text("do not delete\n")

    with pytest.raises(WorktreeError, match="refusing to remove"):
        manager.create_worktree(
            "task-impostor", branch_name="tasks/task-impostor"
        )

    assert (impostor / "keep.txt").read_text() == "do not delete\n"


def test_cleanup_refuses_to_delete_an_unregistered_path(repo: dict) -> None:
    manager = WorktreeManager(
        repo["clone"], repo["worktrees"], execution_broker=_broker()
    )
    unrelated = repo["worktrees"] / "not-ours"
    unrelated.mkdir(parents=True)
    (unrelated / "keep.txt").write_text("keep me\n")

    with pytest.raises(WorktreeError, match="Refusing to remove"):
        manager.remove_worktree("not-ours")

    assert (unrelated / "keep.txt").read_text() == "keep me\n"


def test_unresolved_conflict_aborts_cleanly(repo: dict, tmp_path: Path) -> None:
    _branch(repo, "steward/work")
    (repo["clone"] / "file.txt").write_text("branch side\n")
    _commit_all(repo["clone"], "branch edit")

    _git("checkout", "-q", "main", cwd=repo["clone"])
    (repo["clone"] / "file.txt").write_text("main side\n")
    _commit_all(repo["clone"], "main edit")
    _push_main(repo)

    result = _land(_engine(repo, tmp_path), repo, "steward/work")

    assert result is not None
    assert "Rebase conflict" in result
    # No rebase left half-finished anywhere.
    assert not (repo["clone"] / ".git" / "rebase-merge").exists()
    status = _git("status", "--porcelain", cwd=repo["clone"])
    assert status == ""


def test_missing_work_branch_is_a_clean_error(repo: dict, tmp_path: Path) -> None:
    result = _land(_engine(repo, tmp_path), repo, "steward/ghost")
    assert result is not None
    assert "work branch not found" in result


def test_landing_rejects_a_branch_moved_after_its_exact_tip_was_authorized(
    repo: dict, tmp_path: Path
) -> None:
    _branch(repo, "steward/work")
    (repo["clone"] / "authorized.txt").write_text("authorized\n")
    _commit_all(repo["clone"], "authorized work")
    authorized = _git("rev-parse", "HEAD", cwd=repo["clone"])
    engine = _engine(repo, tmp_path)
    base = engine.transport.sync_remote_to_agent(
        repo["clone"], engine.execution_broker
    )

    (repo["clone"] / "unauthorized.txt").write_text("moved later\n")
    _commit_all(repo["clone"], "unauthorized branch movement")
    result = engine.prepare("steward/work", authorized, base)

    assert result is not None
    assert isinstance(result, ValidationFailure)
    assert "changed after" in result.reason
    with pytest.raises(subprocess.CalledProcessError):
        _origin_main_tree(repo, "authorized.txt")


def test_branch_without_task_commits_cannot_be_reported_as_landed(
    repo: dict, tmp_path: Path
) -> None:
    _branch(repo, "steward/no-op")

    result = _land(_engine(
        repo, tmp_path, gates=[CommandSpec(argv=("false",))],
    ), repo, "steward/no-op")

    assert result is not None
    assert "no commits to land" in result


def test_gate_failure_blocks_push(repo: dict, tmp_path: Path) -> None:
    _branch(repo, "steward/work")
    (repo["clone"] / "work.txt").write_text("gated\n")
    _commit_all(repo["clone"], "gated work")
    engine = _engine(
        repo, tmp_path,
        gates=[CommandSpec(argv=("false",))],
    )
    result = _land(engine, repo, "steward/work")
    assert result is not None
    assert "Gate failed" in result
    with pytest.raises(subprocess.CalledProcessError):
        _origin_main_tree(repo, "work.txt")  # nothing landed


def test_prepare_returns_retained_evidence_without_pushing(repo, tmp_path):
    base = _origin_git(repo, "rev-parse", "main")
    _branch(repo, "steward/work")
    (repo["clone"] / "work.txt").write_text("durable evidence\n")
    _commit_all(repo["clone"], "work")
    gate = CommandSpec(argv=(
        sys.executable, "-c",
        "import sys; print('approved'); print('diagnostic', file=sys.stderr)",
    ))
    engine = _engine(repo, tmp_path, gates=[gate])

    tested = _prepare(engine, repo, "steward/work")

    assert isinstance(tested, Tested)
    assert tested.base_sha == base
    assert _origin_git(repo, "rev-parse", "main") == base
    assert not list(repo["worktrees"].glob("landing-*"))
    # Neither the scratch checkout nor the worker branch is needed for push.
    _git("checkout", "main", cwd=repo["clone"])
    _git("branch", "-D", "steward/work", cwd=repo["clone"])
    assert publish(_transport(repo, tmp_path), tested)
    assert _origin_git(repo, "rev-parse", "main") == tested.tested_sha


@pytest.mark.parametrize("changed", [False, True])
def test_push_race_returns_evidence_without_retesting(repo, tmp_path, monkeypatch, changed):
    _branch(repo, "steward/work")
    (repo["clone"] / "work.txt").write_text("candidate\n")
    _commit_all(repo["clone"], "work")
    engine = _engine(repo, tmp_path, gates=[CommandSpec(argv=("true",))])
    tested = _prepare(engine, repo, "steward/work")
    pushes = []

    def reject(*args):
        pushes.append(args)
        if changed:
            _git("checkout", "main", cwd=repo["clone"])
            (repo["clone"] / "other.txt").write_text("racing writer\n")
            _commit_all(repo["clone"], "race")
            _push_main(repo)
        return False

    monkeypatch.setattr(engine.transport, "push_candidate", reject)
    monkeypatch.setattr(GateRunner, "run_all", lambda *_a, **_k: pytest.fail("retested"))
    assert not publish(engine.transport, tested)
    assert pushes == [(tested.tested_sha, tested.base_sha)]
    assert _origin_git(repo, "rev-parse", "main") != tested.tested_sha
    assert _git("show", "steward/work:work.txt", cwd=repo["clone"]) == "candidate"


def test_failed_gate_returns_bounded_redacted_diagnostics_and_never_edits(repo, tmp_path):
    _branch(repo, "steward/work")
    (repo["clone"] / "work.txt").write_text("candidate\n")
    _commit_all(repo["clone"], "work")
    candidate = _git("rev-parse", "HEAD", cwd=repo["clone"])
    engine = _engine(repo, tmp_path, gates=[CommandSpec(argv=(
        sys.executable, "-c",
        "import sys; print('missing ready.txt'); print('Authorization: Bearer ' + ('sensitive-' * 400), file=sys.stderr); sys.exit(1)",
    ))])
    result = _prepare(engine, repo, "steward/work")
    assert isinstance(result, ValidationFailure)
    # Command argv is trusted configuration; output must redact credentials.
    assert "stdout: missing ready.txt" in result.reason
    assert "stderr: Authorization=[redacted]" in result.reason
    assert "sensitive-sensitive" not in result.reason
    assert len(result.reason) < 3000
    assert _git("rev-parse", "HEAD", cwd=repo["clone"]) == candidate
    assert _origin_git(repo, "rev-parse", "main") != candidate


def test_gate_deadline_remains_a_failed_gate(tmp_path: Path) -> None:
    result = GateRunner.run_command(
        CommandSpec(argv=(sys.executable, "-c", "import time; time.sleep(60)"), timeout_seconds=1),
        tmp_path, broker=_broker(),
    )
    assert not result.passed
    assert result.exit_code == 124
    assert "timed out" in result.stderr


def test_reconciled_merge_collapses_before_gates_and_retains_native_history(repo, tmp_path):
    root = repo['clone']
    _branch(repo, 'steward/work')
    (root / 'file.txt').write_text('branch side\n')
    _commit_all(root, 'branch edit')
    _git('checkout', 'main', cwd=root)
    (root / 'file.txt').write_text('main side\n')
    _commit_all(root, 'main edit')
    _push_main(repo)
    _git('checkout', 'steward/work', cwd=root)
    merged = subprocess.run(['git', 'merge', 'main'], cwd=root, capture_output=True)
    assert merged.returncode == 1
    (root / 'file.txt').write_text('both sides reconciled\n')
    _commit_all(root, 'reconcile main')
    candidate = _git('rev-parse', 'HEAD', cwd=root)
    engine = _engine(repo, tmp_path, gates=[CommandSpec(argv=('true',))])
    tested = _prepare(engine, repo, 'steward/work')
    assert isinstance(tested, Tested)
    assert publish(engine.transport, tested)
    assert tested.tested_sha == _origin_git(repo, 'rev-parse', 'main')
    assert tested.tested_sha != candidate
    assert len(_origin_git(repo, 'rev-list', '--parents', '-n', '1', 'main').split()) == 2
    assert _origin_main_tree(repo, 'file.txt') == 'both sides reconciled'
    assert _git('rev-parse', 'HEAD', cwd=root) == candidate


def test_clean_rebase_has_identity_in_fresh_agent_repo(repo, tmp_path):
    root = repo["clone"]
    _branch(repo, "steward/work")
    (root / "file.txt").write_text("branch side\n")
    _commit_all(root, "branch edit")
    _git("checkout", "main", cwd=root)
    (root / "other.txt").write_text("main side\n")
    _commit_all(root, "main edit")
    _push_main(repo)
    _git("config", "--unset", "user.name", cwd=root)
    _git("config", "--unset", "user.email", cwd=root)
    _git("config", "commit.gpgsign", "true", cwd=root)
    assert _land(_engine(repo, tmp_path), repo, "steward/work") is None
    assert _origin_git(repo, "show", "-s", "--format=%cn <%ce>", "main") == "Steward <steward@localhost>"


def test_rebase_setup_failure_keeps_stage_and_remote(repo, tmp_path, monkeypatch):
    import steward_harness.git_reconcile as reconciliation

    root = repo["clone"]
    _branch(repo, "steward/work")
    (root / "file.txt").write_text("branch side\n")
    _commit_all(root, "branch edit")
    _git("checkout", "main", cwd=root)
    (root / "file.txt").write_text("main side\n")
    _commit_all(root, "main edit")
    _push_main(repo)
    base = _origin_git(repo, "rev-parse", "main")
    original = reconciliation.run_agent_git

    def failed_git(*args, **kwargs):
        if "rebase" in args and "--merge" in args:
            return subprocess.CompletedProcess(args, 128, "", "fatal: committer identity unknown")
        return original(*args, **kwargs)

    monkeypatch.setattr(reconciliation, "run_agent_git", failed_git)
    result = _prepare(_engine(repo, tmp_path), repo, "steward/work")
    assert result.reason == "Git rebase failed (exit 128): fatal: committer identity unknown"
    assert _origin_git(repo, "rev-parse", "main") == base
