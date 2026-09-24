"""Retained session environments and disposable world integration checkouts."""

from __future__ import annotations

from state_fixtures import prepare_turn

import json
import subprocess
import threading
from pathlib import Path

import pytest

from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.runtime.contracts import RuntimeRequest, resolve_model
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.process import ProcessController
from steward_harness.runtime.providers.claude import ClaudeRuntime
from steward_harness.state import StateDatabase
from steward_harness.world.git_world import GitWorld
from steward_harness.lease import Lease
from steward_harness.world.turn_checkpoint import WorldTurnCheckpoint, WorldUpdatePending


def _broker() -> UntrustedExecutionBroker:
    return UntrustedExecutionBroker(UntrustedExecutionConfig())


def _git_world(root: Path) -> GitWorld:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@x",
            "commit",
            "--allow-empty",
            "-m",
            "init",
            "-q",
        ],
        cwd=root,
        check=True,
    )
    return GitWorld(root, execution_broker=_broker())


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_path(root: Path, marker: str) -> Path:
    return Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", marker],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )


def _commit_all(root: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@x",
            "commit",
            "-q",
            "-m",
            message,
        ],
        cwd=root,
        check=True,
    )
    return _head(root)


def _episodes(root: Path) -> list[dict[str, str]]:
    """Each accepted turn's exchange, oldest first, read back from its commit."""
    log = subprocess.run(
        ["git", "log", "--reverse", "--format=%(trailers:key=Steward-Turn,valueonly)%x00%B%x01"],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout
    episodes = []
    for record in log.split("\x01"):
        event, _, message = record.strip("\n").partition("\0")
        if not event.strip():
            continue
        body = message.split("\n\nInput:\n", 1)[1].rsplit("\n\nSteward-Turn:", 1)[0]
        user, _, steward = body.partition("\n\nReply:\n")
        episodes.append({"event_id": event.strip(), "user": user, "steward": steward})
    return episodes


def _checkpoint(world: GitWorld, tmp_path: Path) -> WorldTurnCheckpoint:
    lease = Lease(tmp_path / "lock", timeout_seconds=5.0)
    return WorldTurnCheckpoint(
        world,
        lease,
        tmp_path / "world-turns",
        execution_broker=_broker(),
        state=StateDatabase(tmp_path / "state.db"),
        # The daemon always supplies a resolver, so None is a shape production
        # never constructs. This one declines to resolve, which is what these
        # tests are about: an unresolved conflict must retain the candidate
        # and leave the world untouched.
        resolve_turn=lambda _turn: None,
    )


def _finish(checkpoint, turn, event, user, reply):
    state = checkpoint.state
    conversation = state.get_or_create_conversation(
        "telegram",
        event,
        provider="codex",
        profile="balanced",
    )
    source, _ = state.start_turn(conversation.conversation_id, event, "operator", user)
    event_id = str(source.turn_id)
    base, sha = turn.base_sha, checkpoint.retain(turn, event_id, user, reply, event)
    prepare_turn(state, event_id,
        world_root=str(checkpoint.world.root),
        base_sha=base,
        candidate_sha=sha,
        output=reply,
        provider="codex",
        model="model",
        provider_session_id=None,
        profile="balanced",
    )
    checkpoint.apply(
        event_id,
        lambda: state.accept_turn(
            event_id, visible_reply=reply, spec=None, rejection=None
        ),
    )


def test_checkout_creates_a_detached_worktree_at_current_head(tmp_path: Path) -> None:
    world = _git_world(tmp_path / "world")
    checkpoint = _checkpoint(world, tmp_path)

    turn = checkpoint.checkout(workspace_id="conversation-first")

    assert turn.path.is_dir()
    assert turn.path != world.root
    assert _head(turn.path) == _head(world.root)
    assert checkpoint.checkout(workspace_id="conversation-first").path == turn.path


def test_finish_merges_cleanly_when_head_has_not_moved(tmp_path: Path) -> None:
    world = _git_world(tmp_path / "world")
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id="conversation-first")
    (turn.path / "note.md").write_text("hello\n")

    _finish(checkpoint, turn, "turn_" + "a" * 32, "hi", "hello there")

    assert turn.path.is_dir()
    assert not list(checkpoint.worktrees_root.glob("integration-*"))
    assert (world.root / "note.md").read_text() == "hello\n"
    episodes = _episodes(world.root)
    assert len(episodes) == 1
    assert episodes[0]["user"] == "hi"
    assert episodes[0]["steward"] == "hello there"


def test_delivery_artifact_in_the_world_root_does_not_block_application(
    tmp_path: Path,
) -> None:
    """A delivered file is untracked in the world root and touches no revision.

    Telegram writes deliveries into `delivery_roots`, which is configured to a
    directory *inside* the world. Nothing but an application ever commits the
    world root, so requiring a wholly clean tree let the first delivery after
    an accepted turn wedge every later one permanently.
    """
    world = _git_world(tmp_path / "world")
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id="conversation-first")
    delivery = world.root / "artifacts" / "delivery" / "preview.png"
    delivery.parent.mkdir(parents=True)
    delivery.write_bytes(b"\x89PNG delivered mid-turn")

    (turn.path / "note.md").write_text("hello\n")
    _finish(checkpoint, turn, "turn_" + "d" * 32, "hi", "hello there")

    assert (world.root / "note.md").read_text() == "hello\n"
    assert delivery.read_bytes() == b"\x89PNG delivered mid-turn"
    assert len(_episodes(world.root)) == 1


def test_uncommitted_work_on_an_applied_path_still_defers(tmp_path: Path) -> None:
    """The guard narrowed to collisions, so it must still catch a real one."""
    world = _git_world(tmp_path / "world")
    (world.root / "note.md").write_text("committed\n")
    _commit_all(world.root, "seed note.md")
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id="conversation-first")

    (turn.path / "note.md").write_text("from the turn\n")
    # An unlocked writer leaves the same path dirty in the world root.
    (world.root / "note.md").write_text("edited in place, uncommitted\n")
    stable_head = _head(world.root)

    with pytest.raises(WorldUpdatePending, match="uncommitted work on paths"):
        _finish(checkpoint, turn, "turn_" + "e" * 32, "hi", "hello there")

    assert _head(world.root) == stable_head
    assert (world.root / "note.md").read_text() == "edited in place, uncommitted\n"


def test_finish_rebases_past_a_concurrent_non_conflicting_commit(
    tmp_path: Path,
) -> None:
    world = _git_world(tmp_path / "world")
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id="conversation-first")

    # Simulate a second writer (another conversation, or a rhythm) landing
    # while this turn's cognition is still running, unlocked.
    (world.root / "other.md").write_text("from someone else\n")
    interloper = _commit_all(world.root, "steward: checkpoint turn_other")
    assert interloper != turn.path and _head(world.root) != _head(turn.path)

    (turn.path / "note.md").write_text("hello\n")
    _finish(checkpoint, turn, "turn_" + "b" * 32, "hi", "hello there")

    assert (world.root / "other.md").read_text() == "from someone else\n"
    assert (world.root / "note.md").read_text() == "hello\n"
    episodes = _episodes(world.root)
    assert len(episodes) == 1
    assert episodes[0]["user"] == "hi"


@pytest.mark.parametrize("filename", ["shared.md", "memories/glm/MEMORY.md"])
def test_conflict_retains_the_candidate_without_changing_the_world(
    tmp_path: Path, filename: str,
) -> None:
    world = _git_world(tmp_path / "world")
    (world.root / filename).parent.mkdir(parents=True, exist_ok=True)
    (world.root / filename).write_text("value: 0\n")
    _commit_all(world.root, "seed shared.md")
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id="conversation-first")

    # A concurrent writer changes the exact same line this turn also edits.
    (world.root / filename).write_text("value: 1\n")
    _commit_all(world.root, "steward: checkpoint turn_conflict")
    stable_head = _head(world.root)

    (turn.path / filename).write_text("value: 2\n")

    with pytest.raises(RuntimeError, match="world content conflict"):
        _finish(checkpoint, turn, "turn_" + "c" * 32, "hi", "hello there")

    assert _head(world.root) == stable_head
    assert (world.root / filename).read_text() == "value: 1\n"
    assert _episodes(world.root) == []
    leftover = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=world.root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(turn.path) in leftover
    assert (turn.path / filename).read_text() == "value: 2\n"
    assert not list(checkpoint.worktrees_root.glob("integration-*"))


def test_native_memory_follows_turn_acceptance_and_next_checkout(tmp_path):
    world = _git_world(tmp_path / "world")
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id="conversation-memory")
    runtime = ClaudeRuntime(
        native_home=tmp_path / "private-native",
        controller=ProcessController(_broker()),
    )
    command = runtime._command(RuntimeRequest(
        execution_id="native-memory", resolved=resolve_model("claude", "fast"),
        provider_session_id=None, prompt="remember", cwd=turn.path,
        timeout_seconds=10, sandbox_mode="workspace-write",
    ), None)
    memory = Path(json.loads(command[command.index("--settings") + 1])["autoMemoryDirectory"])
    relative = Path("memories/claude/MEMORY.md")
    assert memory == (turn.path / relative).parent
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("scope-owned knowledge\n")
    assert not (world.root / relative).exists()

    _finish(checkpoint, turn, "native-memory", "remember", "saved")

    assert (world.root / relative).read_text() == "scope-owned knowledge\n"
    next_turn = checkpoint.checkout(workspace_id="conversation-memory")
    assert next_turn.path == turn.path
    assert (next_turn.path / relative).read_text() == "scope-owned knowledge\n"


def test_restart_retains_only_durably_unprepared_owner_work(tmp_path: Path) -> None:
    world = _git_world(tmp_path / "world")
    checkpoint = _checkpoint(world, tmp_path)
    conversation = checkpoint.state.get_or_create_conversation(
        "telegram", "unfinished", provider="codex", profile="balanced"
    )
    source, _ = checkpoint.state.start_turn(
        conversation.conversation_id, "unfinished", "operator", "keep this"
    )
    workspace_id = f"conversation-{conversation.conversation_id}"
    turn = checkpoint.checkout(workspace_id=workspace_id)
    (turn.path / "note.md").write_text("never committed\n")
    checkpoint.state.interrupt_turn(source.turn_id, "provider stopped")
    original_head = _head(world.root)

    restarted = _checkpoint(world, tmp_path)
    restarted.startup_cleanup()
    resumed = restarted.checkout(workspace_id=workspace_id)

    assert resumed.path == turn.path
    assert (resumed.path / "note.md").read_text() == "never committed\n"
    assert _head(world.root) == original_head
    assert _episodes(world.root) == []


def test_startup_cleanup_removes_integration_worktree_left_after_acceptance(
    tmp_path: Path, monkeypatch,
) -> None:
    world = _git_world(tmp_path / "world")
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id="conversation-first")
    (turn.path / "note.md").write_text("accepted\n")
    # A concurrent world commit makes acceptance integrate in scratch.
    (world.root / "other.md").write_text("concurrent\n")
    _commit_all(world.root, "concurrent")
    # Reproduce process loss after acceptance, before apply's scratch cleanup.
    with monkeypatch.context() as patch:
        patch.setattr(checkpoint, "_remove_worktree", lambda _path: None)
        _finish(checkpoint, turn, "first", "save", "saved")
    integration, = checkpoint.worktrees_root.glob("integration-*")
    assert integration.is_dir()

    restarted = _checkpoint(world, tmp_path)
    restarted.startup_cleanup()

    assert not integration.exists()
    assert turn.path.exists()
    leftover = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=world.root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(integration) not in leftover
    assert str(turn.path) in leftover


def test_workspace_reconciliation_reclaims_space_when_git_cleanup_cannot_write(
    tmp_path: Path, monkeypatch,
) -> None:
    world = _git_world(tmp_path / "world")
    checkpoint = _checkpoint(world, tmp_path)
    obsolete = checkpoint.worktrees_root / f"integration-{'f' * 32}"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(obsolete), "HEAD"],
        cwd=world.root, check=True, capture_output=True,
    )
    (obsolete / "large-provider-record").write_text("reclaim me\n")
    git = checkpoint._git

    def full_disk(cwd, *args, check=True):
        if args[:2] == ("worktree", "remove"):
            return subprocess.CompletedProcess(args, 1, "", "out of diskspace")
        return git(cwd, *args, check=check)

    monkeypatch.setattr(checkpoint, "_git", full_disk)
    checkpoint.startup_cleanup()

    assert not obsolete.exists()


def test_concurrent_turns_from_real_threads_both_land(tmp_path: Path) -> None:
    world = _git_world(tmp_path / "world")
    checkpoint = _checkpoint(world, tmp_path)
    barrier = threading.Barrier(2, timeout=10)
    errors: list[BaseException] = []

    def run_turn(name: str) -> None:
        try:
            turn = checkpoint.checkout(workspace_id=f"conversation-{name}")
            # Force both turns to be cognating (unlocked) at the same time,
            # so their eventual finish() calls genuinely race for the lease
            # rather than happening to run one after the other.
            barrier.wait()
            (turn.path / f"{name}.md").write_text(f"from {name}\n")
            _finish(checkpoint, turn, f"turn-{name}", name, f"done {name}")
        except BaseException as error:  # noqa: BLE001 - surfaced via `errors`
            errors.append(error)

    threads = [
        threading.Thread(target=run_turn, args=(name,))
        for name in ("aaaaaaaa", "bbbbbbbb")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors, errors
    assert not any(thread.is_alive() for thread in threads)
    assert (world.root / "aaaaaaaa.md").read_text() == "from aaaaaaaa\n"
    assert (world.root / "bbbbbbbb.md").read_text() == "from bbbbbbbb\n"
    episodes = _episodes(world.root)
    assert {entry["user"] for entry in episodes} == {"aaaaaaaa", "bbbbbbbb"}
    leftover = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=world.root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert len(list(checkpoint.worktrees_root.glob("session-*"))) == 2
    assert not list(checkpoint.worktrees_root.glob("integration-*"))
    assert "integration-" not in leftover


def test_completed_sessions_retain_environment_and_refresh_accepted_world(tmp_path):
    world = _git_world(tmp_path / "world")
    (world.root / ".gitignore").write_text(".venv/\n")
    _commit_all(world.root, "ignore local environment")
    checkpoint = _checkpoint(world, tmp_path)
    first = checkpoint.checkout(workspace_id="conversation-first")
    second = checkpoint.checkout(workspace_id="conversation-second")
    (second.path / ".venv").mkdir()
    artifact = second.path / ".venv" / "installed"
    artifact.write_text("keep this environment")
    inode = artifact.stat().st_ino
    (first.path / "first.svg").write_text("first")
    (second.path / "second.svg").write_text("second")
    _finish(checkpoint, first, "first", "create first", "done")
    _finish(checkpoint, second, "second", "create second", "done")
    checkpoint.startup_cleanup()
    assert first.path.exists() and second.path.exists()
    assert artifact.stat().st_ino == inode
    resumed = checkpoint.checkout(workspace_id="conversation-second")
    assert (resumed.path / "first.svg").read_text() == "first"
    (resumed.path / "second.svg").write_text("revised")
    _finish(checkpoint, resumed, "revision", "revise second", "done")
    assert (world.root / "first.svg").read_text() == "first"
    assert (world.root / "second.svg").read_text() == "revised"
    assert len(_episodes(world.root)) == 3
    checkpoint.startup_cleanup()
    assert first.path.exists() and second.path.exists()
    assert artifact.read_text() == "keep this environment"
    assert artifact.stat().st_ino == inode


def test_world_refresh_cannot_overwrite_an_ignored_local_environment(tmp_path):
    world = _git_world(tmp_path / "world")
    (world.root / ".gitignore").write_text(".venv/\n")
    _commit_all(world.root, "ignore environments")
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id="conversation-environment")
    (turn.path / ".venv").mkdir()
    installed = turn.path / ".venv" / "installed"
    installed.write_text("local environment")
    (world.root / ".venv").mkdir()
    (world.root / ".venv" / "installed").write_text("other content")
    subprocess.run(["git", "add", "--force", ".venv/installed"], cwd=world.root, check=True)
    _commit_all(world.root, "another writer tracks the same path")
    with pytest.raises(RuntimeError, match="conflicts with retained workspace"):
        checkpoint.checkout(workspace_id="conversation-environment")
    assert installed.read_text() == "local environment"


@pytest.mark.parametrize("filename", ["tasks.md", "shared tasks.md"])
def test_world_refresh_aborts_a_conflicted_merge_and_names_what_conflicted(tmp_path, filename):
    """A refused refresh must leave the workspace mergeable, and say what blocked it.

    Two independent appends to the same end-of-file region are the ordinary
    shape of a shared list. Parking that conflict in the checkout makes every
    later refresh return early on a dirty tree, so the session resumes on top
    of conflict markers and commits them as its own work.
    """
    world = _git_world(tmp_path / "world")
    (world.root / filename).write_text("- one\n")
    _commit_all(world.root, "a shared list")
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id="conversation-list")

    (turn.path / filename).write_text("- one\n- from the session\n")
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@x",
         "commit", "-qam", "session appends"], cwd=turn.path, check=True,
    )
    session_head = _head(turn.path)
    (world.root / filename).write_text("- one\n- from the world\n")
    _commit_all(world.root, "another writer appends")
    accepted_head = _head(world.root)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="conflicts with retained workspace") as failure:
            checkpoint.checkout(workspace_id="conversation-list")
        assert filename in str(failure.value)
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=turn.path,
            capture_output=True, text=True, check=True,
        )
        assert status.stdout == ""
        assert not _git_path(turn.path, "MERGE_HEAD").exists()
        assert _head(turn.path) == session_head
        assert _head(world.root) == accepted_head
        assert (turn.path / filename).read_text() == "- one\n- from the session\n"
        assert (world.root / filename).read_text() == "- one\n- from the world\n"


def test_world_refresh_leaves_an_interrupted_revert_for_its_session(tmp_path):
    """An unfinished Git operation belongs to its session, not to acceptance."""
    world = _git_world(tmp_path / "world")
    (world.root / "shared.md").write_text("base\n")
    _commit_all(world.root, "base content")
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id="interrupted-revert")

    # A provider killed mid-`git revert` leaves the marker behind with a clean
    # tree, because reverting an empty commit changes no file. Status alone
    # cannot see that the session has unresolved work.
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@x",
         "commit", "-q", "--allow-empty", "-m", "empty change"],
        cwd=turn.path, check=True,
    )
    subprocess.run(["git", "revert", "--no-commit", "HEAD"], cwd=turn.path, check=False)
    session_head = _head(turn.path)
    assert not subprocess.run(
        ["git", "status", "--porcelain"], cwd=turn.path,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert _git_path(turn.path, "REVERT_HEAD").exists()

    (world.root / "shared.md").write_text("accepted\n")
    _commit_all(world.root, "accepted edit")

    resumed = checkpoint.checkout(workspace_id="interrupted-revert")

    assert resumed.path == turn.path
    assert _head(turn.path) == session_head
    assert _git_path(turn.path, "REVERT_HEAD").exists()
    assert (turn.path / "shared.md").read_text() == "base\n"


def test_world_acceptance_preserves_a_sessions_reconciliation_merge(tmp_path):
    world = _git_world(tmp_path / 'world')
    (world.root / 'shared.md').write_text('base\n')
    _commit_all(world.root, 'base content')
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id='reconciled-session')
    (turn.path / 'shared.md').write_text('session side\n')
    _commit_all(turn.path, 'native session edit')
    (world.root / 'shared.md').write_text('accepted side\n')
    accepted = _commit_all(world.root, 'another accepted edit')
    # The same identity every other commit here carries. A merge needs one even
    # to reach its conflict, so leaving it ambient made this test assert the
    # runner's `git config` rather than the reconciliation it is named for.
    conflict = subprocess.run(
        ['git', '-c', 'user.name=T', '-c', 'user.email=t@x', 'merge', accepted],
        cwd=turn.path, capture_output=True,
    )
    assert conflict.returncode == 1, conflict.stderr.decode()
    (turn.path / 'shared.md').write_text('both sides reconciled\n')
    merged = _commit_all(turn.path, 'native reconciliation')

    _finish(checkpoint, turn, 'reconciled', 'save', 'saved')

    assert (world.root / 'shared.md').read_text() == 'both sides reconciled\n'
    subprocess.run(['git', 'merge-base', '--is-ancestor', merged, 'HEAD'], cwd=world.root, check=True)
    assert turn.path.is_dir()


def test_retry_after_unprepared_commit_replaces_only_that_attempt(tmp_path, monkeypatch):
    world = _git_world(tmp_path / "world")
    world.finish("earlier-event", "earlier input", "accepted history",
                 base=_head(world.root), source="earlier")
    checkpoint = _checkpoint(world, tmp_path)
    state = checkpoint.state
    conversation = state.get_or_create_conversation(
        "telegram", "retry", provider="codex", profile="balanced",
    )
    source, _ = state.start_turn(conversation.conversation_id, "retry", "operator", "input")
    event_id = str(source.turn_id)
    worktree = checkpoint.checkout(workspace_id=conversation.conversation_id.workspace)
    (worktree.path / "first.txt").write_text("work from the first attempt")
    # The first attempt commits but is never prepared (a crash before SQL).
    checkpoint.retain(worktree, event_id, "input", "first reply", "retry")
    assert state.prepared_turn(event_id) is None
    assert _episodes(worktree.path)[-1]["steward"] == "first reply"

    resumed = checkpoint.checkout(workspace_id=conversation.conversation_id.workspace)
    (resumed.path / "second.txt").write_text("work from the resumed attempt")
    base, candidate = resumed.base_sha, checkpoint.retain(resumed, event_id, "input", "resumed reply", "retry")
    prepare_turn(state, event_id, world_root=str(world.root), base_sha=base, candidate_sha=candidate,
        output="resumed reply", provider="codex", model="model",
        provider_session_id=None, profile="balanced",
    )
    with pytest.raises(WorldUpdatePending, match="prepared world turns"):
        checkpoint.retain(resumed, event_id, "input", "must not replace prepared output", "retry")
    checkpoint.apply(event_id, lambda: state.accept_turn(
        event_id, visible_reply="resumed reply", spec=None, rejection=None,
    ))
    assert (world.root / "first.txt").read_text() == "work from the first attempt"
    assert (world.root / "second.txt").read_text() == "work from the resumed attempt"
    entries = _episodes(world.root)
    assert [entry["steward"] for entry in entries] == ["accepted history", "resumed reply"]
    assert sum(entry["event_id"] == event_id for entry in entries) == 1


@pytest.mark.parametrize("checkpoints", [1, 2])
def test_refresh_advances_accepted_candidate_history_after_world_revises_it(tmp_path, checkpoints):
    world = _git_world(tmp_path / "world")
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id="accepted-session")
    # Force acceptance to rebase, leaving the session's original candidate
    # outside world ancestry. Include native commits covered by the receipt.
    (world.root / "other.md").write_text("concurrent writer\n")
    _commit_all(world.root, "concurrent world change")
    for index in range(checkpoints):
        (turn.path / "note.md").write_text(f"session version {index}\n")
        _commit_all(turn.path, "native edit")
        _finish(checkpoint, turn, f"accepted-{index}", "save", "saved")
    candidate = _head(turn.path)
    (world.root / "note.md").write_text("later accepted revision\n")
    current = _commit_all(world.root, "another turn revises the same paragraph")
    assert checkpoint._git(world.root, "merge-base", "--is-ancestor", candidate, current,
                           check=False).returncode == 1

    # A fresh checkpoint instance must derive consumption from durable receipts.
    resumed = _checkpoint(world, tmp_path).checkout(workspace_id="accepted-session")
    assert resumed.base_sha == current
    assert _head(resumed.path) == current
    assert (resumed.path / "note.md").read_text() == "later accepted revision\n"
    assert not _git_path(resumed.path, "MERGE_HEAD").exists()
    assert checkpoint._git(world.root, "cat-file", "-t", candidate).stdout.strip() == "commit"


@pytest.mark.parametrize("remaining", ["commit", "dirty", "operation", "ignored", "pending", "other-world"])
def test_accepted_refresh_preserves_work_without_authority_to_discard_it(tmp_path, remaining):
    world = _git_world(tmp_path / "world")
    (world.root / ".gitignore").write_text("environment\n")
    _commit_all(world.root, "ignore environment")
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id="accepted-session")
    (world.root / "other.md").write_text("concurrent writer\n")
    _commit_all(world.root, "force rebase")
    (turn.path / "note.md").write_text("accepted candidate\n")
    _finish(checkpoint, turn, "accepted", "save", "saved")
    (world.root / "note.md").write_text("later accepted revision\n")
    _commit_all(world.root, "revise accepted paragraph")
    if remaining == "commit":
        (turn.path / "note.md").write_text("unaccepted local commit\n")
        _commit_all(turn.path, "unaccepted work")
    elif remaining == "dirty":
        (turn.path / "note.md").write_text("uncommitted local work\n")
    elif remaining == "operation":
        _git_path(turn.path, "REVERT_HEAD").write_text(_head(turn.path) + "\n")
    elif remaining == "ignored":
        (turn.path / "environment").write_text("local environment\n")
        (world.root / "environment").write_text("world file\n")
        checkpoint._git(world.root, "add", "--force", "environment")
        _commit_all(world.root, "track environment collision")
    else:
        with checkpoint.state.connect(write=True) as connection:
            if remaining == "pending":
                connection.execute("UPDATE turns SET state='running', completed_at=NULL, reply_text=NULL")
            else:
                connection.execute("UPDATE turns SET world_root='/another-world'")
    original = _head(turn.path)
    content = (turn.path / "note.md").read_text()
    if remaining in {"dirty", "operation"}:
        checkpoint.checkout(workspace_id="accepted-session")
    else:
        with pytest.raises(RuntimeError, match="conflicts with retained workspace"):
            checkpoint.checkout(workspace_id="accepted-session")
    assert _head(turn.path) == original
    assert (turn.path / "note.md").read_text() == content
    assert not _git_path(turn.path, "MERGE_HEAD").exists()
    if remaining == "ignored":
        assert (turn.path / "environment").read_text() == "local environment\n"
def test_identical_concurrent_edits_keep_both_exchanges(tmp_path: Path) -> None:
    """A rebase must not drop a closing commit whose change the world already has."""
    world = _git_world(tmp_path / "world")
    checkpoint = _checkpoint(world, tmp_path)
    first = checkpoint.checkout(workspace_id="conversation-first")
    second = checkpoint.checkout(workspace_id="conversation-second")
    for turn in (first, second):
        (turn.path / "same.md").write_text("identical\n")
    _finish(checkpoint, first, "first", "write it", "done")
    _finish(checkpoint, second, "second", "write it too", "also done")
    assert [entry["user"] for entry in _episodes(world.root)] == ["write it", "write it too"]
    assert checkpoint.state.pending_turns() == []


def test_a_rebase_rewrites_only_the_trailer_base_not_the_exchange(tmp_path: Path) -> None:
    world = _git_world(tmp_path / "world")
    checkpoint = _checkpoint(world, tmp_path)
    turn = checkpoint.checkout(workspace_id="conversation-first")
    (turn.path / "mine.md").write_text("mine\n")
    (world.root / "theirs.md").write_text("theirs\n")
    moved = _commit_all(world.root, "concurrent")
    _finish(checkpoint, turn, "first", "quote it", "Steward-Base: quoted, not a trailer")
    assert _episodes(world.root)[-1]["steward"] == "Steward-Base: quoted, not a trailer"
    assert world.trailers("HEAD")["Steward-Base"] == moved
