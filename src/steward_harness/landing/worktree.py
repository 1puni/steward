"""Isolated Git worktree management for retained steward tasks."""

from __future__ import annotations

from pathlib import Path

from steward_harness.git import git_operation_paths, agent_git, run_agent_git, validate_object_id
from steward_harness.runtime.execution import UntrustedExecutionBroker


class WorktreeError(RuntimeError):
    """Raised when a git worktree operation fails."""


class WorktreeManager:
    """Creates, validates, and cleans up isolated git worktrees for tasks."""

    def __init__(
        self,
        repo_path: str | Path,
        worktrees_root: str | Path,
        *,
        execution_broker: UntrustedExecutionBroker,
    ) -> None:
        self.repo_path = execution_broker.resolve_path(repo_path)
        self.worktrees_root = execution_broker.resolve_path(worktrees_root)
        self.execution_broker = execution_broker
        if not execution_broker.is_directory(self.repo_path):
            raise ValueError(f"Repository path must exist: {self.repo_path}")
        created = execution_broker.run(
            ["mkdir", "-p", str(self.worktrees_root)],
            cwd=self.repo_path,
            timeout=30,
        )
        if created.returncode != 0 or not execution_broker.is_directory(self.worktrees_root):
            raise ValueError(
                f"Agent could not create worktree root: {self.worktrees_root}"
            )

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        return agent_git(
            self.execution_broker,
            *args,
            cwd=cwd or self.repo_path,
            timeout=60,
            error=WorktreeError,
        )

    def create_worktree(
        self,
        task_id: str,
        *,
        branch_name: str,
        base_sha: str | None = None,
    ) -> Path:
        target_path = self.worktrees_root / task_id
        branch = branch_name
        if self.execution_broker.path_exists(target_path):
            # A daemon crash can leave the registered task worktree behind
            # with edits that never reached the branch ref. Reuse that exact
            # worktree instead of force-removing it and destroying recoverable
            # provider work. Unrelated or malformed leftovers are still pruned.
            attached_branch = self._registered_worktree_branch(target_path)
            if attached_branch == branch:
                self._validate_attached_worktree(target_path)
                return target_path
            raise WorktreeError(
                f"Existing worktree path {target_path} is not the retained {branch!r} "
                f"worktree (found {attached_branch or 'unregistered'}); refusing to remove it"
            )

        # Reattach retained task work at its current tip. Only a genuinely new
        # task branch starts from the requested base; resetting here discards
        # commits preserved across a crashed lease.
        if self.has_branch(branch):
            self._git("worktree", "add", str(target_path), branch)
        else:
            try:
                validate_object_id(base_sha or "")
            except ValueError:
                raise WorktreeError(
                    "a new worktree requires an exact lowercase SHA-1 base"
                )
            self._git("worktree", "add", "-b", branch, str(target_path), base_sha)
        return target_path

    def has_branch(self, branch: str) -> bool:
        """Return whether retained local work already owns this branch."""
        result = run_agent_git(
            self.execution_broker,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            cwd=self.repo_path,
            timeout=60,
        )
        return result.returncode == 0

    def _registered_worktree_branch(self, path: Path) -> str | None:
        """Resolve a retained path/ref only through the trusted repository registry."""
        output = self._git("worktree", "list", "--porcelain")
        wanted = self.execution_broker.resolve_path(path)
        for block in output.split("\n\n"):
            lines = block.splitlines()
            worktree_line = next(
                (line for line in lines if line.startswith("worktree ")), None
            )
            if worktree_line is None:
                continue
            registered = self.execution_broker.resolve_path(worktree_line.removeprefix("worktree "))
            if registered != wanted:
                continue
            branch_line = next(
                (line for line in lines if line.startswith("branch refs/heads/")),
                None,
            )
            if branch_line is None:
                return None
            return branch_line.removeprefix("branch refs/heads/")
        return None

    def _validate_attached_worktree(self, path: Path) -> None:
        """Reject a replaced repository or an interrupted model-owned Git operation."""
        top = self._git("rev-parse", "--show-toplevel", cwd=path)
        if self.execution_broker.resolve_path(top) != self.execution_broker.resolve_path(path):
            raise WorktreeError(f"Retained worktree has an unexpected root: {top}")

        expected_common = self._git("rev-parse", "--git-common-dir")
        actual_common = self._git("rev-parse", "--git-common-dir", cwd=path)
        expected_path = Path(expected_common)
        if not expected_path.is_absolute():
            expected_path = self.repo_path / expected_path
        actual_path = Path(actual_common)
        if not actual_path.is_absolute():
            actual_path = path / actual_path
        if self.execution_broker.resolve_path(expected_path) != self.execution_broker.resolve_path(actual_path):
            raise WorktreeError("Retained worktree no longer belongs to the managed repository")

        for marker, marker_path in git_operation_paths(lambda *args: self._git(*args, cwd=path)).items():
            if self.execution_broker.path_exists(marker_path):
                raise WorktreeError(
                    f"Retained worktree has an unfinished Git operation: {marker}"
                )

    def remove_worktree(self, task_id: str) -> None:
        target_path = self.worktrees_root / task_id
        if not self.execution_broker.path_exists(target_path):
            return
        attached_branch = self._registered_worktree_branch(target_path)
        if attached_branch is None:
            raise WorktreeError(
                f"Refusing to remove unregistered or detached path {target_path}"
            )
        self._validate_attached_worktree(target_path)
        self._git("worktree", "remove", "--force", str(target_path))
