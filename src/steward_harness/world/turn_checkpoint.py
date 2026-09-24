"""Retained session workspaces and exact, recoverable world checkpoints.

Conversations and rhythms keep their working environment across executions.
Only integration uses disposable checkouts. The world lease serializes Git
acceptance, while independent sessions can work concurrently.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from steward_harness.git import (
    IN_PROGRESS_GIT_MARKERS,
    ISOLATED_GIT_ENV,
    run_agent_git,
    steward_commit_argv,
)
from steward_harness.git_reconcile import ResolveTurn, reconcile_git
from steward_harness.state import StateDatabase
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.world.git_world import BASE_TRAILER, TURN_TRAILER, GitWorld
from steward_harness.lease import Lease

_WORKSPACE_PREFIX = "session-"


class WorldUpdatePending(RuntimeError):
    """An exact world application must be recovered before another writer."""


class WorldContentConflict(RuntimeError):
    """A retained candidate needs content resolution, not replayed cognition."""


@dataclass(frozen=True, slots=True)
class WorldTurnWorktree:
    """An execution's input revision in its owner's retained checkout."""

    path: Path
    base_sha: str


class WorldTurnCheckpoint:
    """Isolate world edits, retain their objects, and accept one exact revision."""

    def __init__(
        self,
        world: GitWorld,
        lease: Lease,
        worktrees_root: str | Path,
        *,
        execution_broker: UntrustedExecutionBroker,
        state: StateDatabase,
        resolve_turn: ResolveTurn | None = None,
    ) -> None:
        self.world = world
        self.lease = lease
        self.worktrees_root = execution_broker.resolve_path(worktrees_root)
        self.broker = execution_broker
        self.state = state
        self.resolve_turn = resolve_turn
        created = execution_broker.run(
            ["mkdir", "-p", str(self.worktrees_root)],
            cwd=self.world.root,
            timeout=30,
        )
        if created.returncode != 0 or not execution_broker.is_directory(self.worktrees_root):
            raise ValueError(
                f"Agent could not create world-turn worktree root: {self.worktrees_root}"
            )

    def startup_cleanup(self) -> None:
        """Remove integration scratch; owner workspaces keep their local work.

        Only startup knows there are no live peer integrations. During
        execution, each apply() owns its own checkout cleanup.
        """
        with self.lease:
            for path in self.broker.child_directories(self.worktrees_root):
                if path.name.startswith("integration-"):
                    self._remove_worktree(path)
            # Removal falls back to deleting the checkout when Git cannot write
            # its administrative files on a full disk. Prune stale registrations
            # after space has been reclaimed.
            self._git(self.world.root, "worktree", "prune", check=False)

    def workspace(self, workspace_id: str) -> Path:
        """The owner's retained checkout, whether or not it exists yet."""
        if not workspace_id:
            raise ValueError("a retained workspace requires an owner")
        return self.worktrees_root / (
            _WORKSPACE_PREFIX + hashlib.sha256(workspace_id.encode()).hexdigest())

    def checkout(self, *, workspace_id: str) -> WorldTurnWorktree:
        """Resume an owner's workspace at its retained input revision.

        New workspaces start at the accepted world. Clean retained workspaces
        merge accepted knowledge; interrupted local work remains available.
        """
        if not workspace_id:
            raise ValueError("a retained workspace requires an owner")
        with self.lease:
            sha = self.world.input_cursor()
            path = self.workspace(workspace_id)
            if self.broker.path_exists(path):
                self._refresh_workspace(path, sha)
                sha = self._git(path, "rev-parse", "HEAD").stdout.strip()
            else:
                self._git(self.world.root, "worktree", "add", "--detach", str(path), sha)
        return WorldTurnWorktree(path, sha)

    def _refresh_workspace(self, path: Path, head: str) -> None:
        """Bring accepted knowledge into a clean checkout; never erase local work."""
        common = Path(self._git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
        expected = Path(self._git(self.world.root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
        top = Path(self._git(path, "rev-parse", "--show-toplevel").stdout.strip())
        if self.broker.is_symlink(path) or top != path or common != expected:
            raise WorldUpdatePending(f"retained workspace no longer belongs to its world: {path}")
        # Interrupted tools, ignored environments, and uncommitted edits belong
        # to the session. Resume them as-is; acceptance will reconcile later.
        if self._git(path, "status", "--porcelain").stdout.strip():
            return
        for marker in IN_PROGRESS_GIT_MARKERS:
            git_path = Path(self._git(path, "rev-parse", "--path-format=absolute", "--git-path", marker).stdout.strip())
            if self.broker.path_exists(git_path):
                return
        # Only completed acceptance for this world authorizes retiring the
        # original candidate history after a rebase. Trailers alone can also
        # appear in unaccepted native commits.
        local = set(self._git(path, "rev-list", "HEAD", f"^{head}").stdout.splitlines())
        with self.state.connect() as connection:
            accepted = local & {row[0] for row in connection.execute(
                "SELECT candidate_sha FROM turns WHERE state='completed' AND world_root=? "
                "AND candidate_sha IS NOT NULL", (str(self.world.root),),
            )}
        consumed = accepted and not self._git(
            path, "rev-list", "HEAD", "--not", head, *sorted(accepted),
        ).stdout.strip()
        command = (
            ("checkout", "--detach", "--no-overwrite-ignore", head)
            if consumed else
            steward_commit_argv("merge", "--no-edit", "--no-overwrite-ignore", head)
        )
        result = self._git(path, *command, check=False)
        if result.returncode:
            conflicted = self._git(
                path, "diff", "--name-only", "--diff-filter=U", check=False,
            ).stdout.splitlines()
            # Leaving the conflict in place is worse than not merging at all:
            # the next refresh reads a dirty tree, returns above, and hands the
            # session a checkout of conflict markers to commit as its own work.
            merge_head = Path(self._git(
                path, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD",
            ).stdout.strip())
            if self.broker.path_exists(merge_head):
                self._git(path, "merge", "--abort")
            detail = ", ".join(conflicted) or (
                result.stderr.strip() or result.stdout.strip()
            )
            raise WorldContentConflict(
                f"accepted world conflicts with retained workspace: {path}: {detail}"
            )

    def retain(
        self,
        turn: WorldTurnWorktree,
        event_id: str,
        user_text: str,
        reply_text: str,
        source: str,
    ) -> str:
        """Commit the complete candidate in its owner's checkout before SQL preparation."""
        if self.state.prepared_turn(event_id) is not None:
            raise WorldUpdatePending("prepared world turns must replay their retained candidate")
        GitWorld(turn.path, execution_broker=self.broker).finish(
            event_id, user_text, reply_text, base=turn.base_sha, source=source,
        )
        return self._git(turn.path, "rev-parse", "HEAD").stdout.strip()

    def _applied(self, event_id: str, base: str, head: str) -> bool:
        """Did this turn's commit already reach the world since its base?"""
        closed = self._git(self.world.root, "log", "--format=%(trailers:key="
                           f"{TURN_TRAILER},valueonly)", f"{base}..{head}", "--").stdout
        return event_id in closed.split()

    def apply(
        self,
        event_id: str,
        finalize: Callable[[], object],
    ) -> object:
        """Apply or recognize one retained revision, finalizing before releasing the lease.

        The world branch only fast-forwards under the lease, so its history
        since the turn's base answers whether the turn already applied.
        """
        for _round in range(2):
            with self.lease:
                row = self.state.prepared_turn(event_id)
                if row is None or row["world_root"] != str(self.world.root):
                    raise ValueError("prepared turn belongs to another world")
                if row["state"] == "completed":
                    return finalize()
                head = self.world.input_cursor()
                if self._applied(event_id, row["base_sha"], head):
                    return finalize()
                if head == row["base_sha"]:
                    self._apply_revision(row["candidate_sha"])
                    return finalize()
                integration = self.worktrees_root / f"integration-{uuid.uuid4().hex}"
                self._git(
                    self.world.root, "worktree", "add", "--detach",
                    str(integration), row["candidate_sha"],
                )
            try:
                error = reconcile_git(
                    integration, head, event_id, broker=self.broker,
                    resolve_turn=self.resolve_turn,
                )
                if error is not None:
                    raise WorldContentConflict(
                        f"world content conflict for {event_id}; candidate retained: {error}"
                    )
                self._rebase_trailer(integration, event_id, head)
                with self.lease:
                    if self.state.prepared_turn(event_id)["state"] == "completed":
                        return finalize()
                    # Another acceptance may have won while the resolver ran.
                    if self.world.input_cursor() != head:
                        continue
                    self._apply_revision(self._git(integration, "rev-parse", "HEAD").stdout.strip())
                    return finalize()
            finally:
                self._remove_worktree(integration)
        raise WorldUpdatePending(f"world moved during reconciliation of {event_id}; candidate retained")

    def _rebase_trailer(self, integration: Path, event_id: str, head: str) -> None:
        """Keep the closing commit's base truthful once it sits on a new one."""
        message = self._git(integration, "log", "-1", "--format=%B").stdout
        if GitWorld(integration, execution_broker=self.broker).trailers("HEAD").get(TURN_TRAILER) != event_id:
            return
        body, _, trailers = message.rstrip("\n").rpartition("\n\n")
        rebased = body + "\n\n" + re.sub(rf"(?m)^{BASE_TRAILER}: .*$", f"{BASE_TRAILER}: {head}", trailers)
        self._git(integration, *steward_commit_argv(
            "commit", "--amend", "--allow-empty", "--quiet", "-F", "-"), input_text=rebased)

    def _apply_revision(self, sha: str) -> None:
        collisions = self._dirty_paths() & self._paths_applied_by(sha)
        if collisions:
            raise WorldUpdatePending(
                "world has uncommitted work on paths this revision applies; "
                f"application retained: {', '.join(sorted(collisions)[:5])}"
            )
        self._git(self.world.root, "-c", "core.fsync=all", "merge", "--ff-only", sha)

    def _paths_applied_by(self, sha: str) -> set[str]:
        """Name every path the fast-forward to `sha` would write."""
        raw = self._git(
            self.world.root, "diff", "--name-only", "-z", "HEAD", sha
        ).stdout
        return {path for path in raw.split("\0") if path}

    def _dirty_paths(self) -> set[str]:
        """Name every path the working tree has diverged on, tracked or not.

        Only a path that is *both* dirty and applied can be lost, so the
        working tree is not required to be clean as a whole. It used to be,
        and that read the world's own delivery artifacts — written into the
        world root by configuration, and committed by nothing but an
        application — as a reason to refuse every application forever.
        """
        raw = self._git(self.world.root, "status", "--porcelain", "-z").stdout
        fields = [field for field in raw.split("\0") if field]
        paths: set[str] = set()
        index = 0
        while index < len(fields):
            entry = fields[index]
            status, path = entry[:2], entry[3:]
            paths.add(path)
            # A rename or copy carries its source in the following field.
            if "R" in status or "C" in status:
                index += 1
                if index < len(fields):
                    paths.add(fields[index])
            index += 1
        return paths

    def _remove_worktree(self, path: Path) -> None:
        if not self.broker.path_exists(path):
            return
        self._git(self.world.root, "worktree", "remove", "--force", str(path), check=False)
        self.broker.run([self.broker.python_executable, "-I", "-c",
            "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)", str(path)],
            cwd=self.world.root, timeout=30)

    def _git(
        self, cwd: Path, *args: str, check: bool = True, input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = run_agent_git(
            self.broker, *args, cwd=cwd, timeout=60, extra_env=ISOLATED_GIT_ENV,
            input_text=input_text,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
            )
        return result


__all__ = ["WorldTurnWorktree", "WorldTurnCheckpoint"]
