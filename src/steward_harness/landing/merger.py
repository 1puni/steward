"""Bounded, test-gated promotion of one candidate branch."""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from typing import ClassVar
from pathlib import Path

from steward_harness.config.schema import RepositoryConfig
from steward_harness.git import (
    agent_git,
    redact_command_output,
    run_agent_git,
    validate_object_id,
    steward_commit_argv,
)
from steward_harness.git_reconcile import (
    reconcile_git,
)
from steward_harness.git_transport import ControllerGitTransport, GitTransportError
from steward_harness.landing.gates import GateResult, GateRunner
from steward_harness.landing.worktree import WorktreeManager
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeUnavailable


@dataclass(frozen=True, slots=True)
class Tested:
    """One integrated, gated candidate and the exact base it was tested on."""

    __test__: ClassVar[bool] = False

    base_sha: str
    tested_sha: str


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    """Evidence returned to the candidate owner, never an embedded repair turn."""

    reason: str
    candidate: str | None = None
    actionable: bool = True


def publish(transport: ControllerGitTransport, revision: Tested) -> bool:
    """Push the gated revision with an exact-base lease; True once the remote holds it.

    Observation, not the push's response, settles the outcome: a push that
    landed but whose response was lost, or a remote that already took this
    revision, both read as landed. A moved or refused remote reads as not.
    """
    transport.push_candidate(revision.tested_sha, revision.base_sha)
    return transport._contains(revision.tested_sha, transport.fetch())


class PromotionEngine:
    """Validate and retain a candidate without publishing or calling cognition."""

    def __init__(
        self,
        repo_config: RepositoryConfig,
        worktrees_root: str | Path,
        *,
        transport: ControllerGitTransport,
        execution_broker: UntrustedExecutionBroker,
    ) -> None:
        self.config = repo_config
        self.transport = transport
        self.repo_path = Path(repo_config.path).resolve()
        self.worktree_mgr = WorktreeManager(
            self.repo_path,
            worktrees_root,
            execution_broker=execution_broker,
        )
        self.execution_broker = execution_broker

    def _agent_git(self, *args: str, cwd: Path | None = None) -> str:
        return agent_git(
            self.execution_broker,
            *args,
            cwd=cwd or self.repo_path,
            timeout=120,
        )

    def prepare(
        self,
        branch_name: str,
        authorized_work_sha: str,
        expected_base_sha: str,
    ) -> Tested | ValidationFailure:
        """Return a retained, gated SHA or actionable failure evidence."""
        for role, sha in (
            ("authorized work", authorized_work_sha),
            ("expected base", expected_base_sha),
        ):
            try:
                validate_object_id(sha)
            except ValueError:
                raise ValueError(f"{role} must be a full lowercase SHA-1")
        slug = branch_name.replace("/", "-")
        scratch = uuid.uuid4().hex[:12]
        worktree_id = f"landing-{slug}-{scratch}"
        landing_branch = f"landing/{slug}-{scratch}"

        try:
            branch = run_agent_git(
                self.execution_broker,
                "show-ref",
                "--verify",
                "--hash",
                f"refs/heads/{branch_name}",
                cwd=self.repo_path,
                timeout=120,
            )
            if branch.returncode != 0:
                return ValidationFailure(f"work branch not found: {branch_name}")
            if branch.stdout.strip() != authorized_work_sha:
                return ValidationFailure(f"work branch changed after {authorized_work_sha} was authorized")
            if int(self._agent_git(
                "rev-list", "--count", f"{expected_base_sha}..{authorized_work_sha}"
            )) == 0:
                return ValidationFailure(f"work branch has no commits to land: {branch_name}")

            worktree = self.worktree_mgr.create_worktree(
                worktree_id,
                branch_name=landing_branch,
                base_sha=authorized_work_sha,
            )
            try:
                # Collapse the final task tree before replaying it. Native merge
                # and exploration histories remain on the authorized task ref.
                fork = self._agent_git("merge-base", expected_base_sha, authorized_work_sha)
                stamp = self._agent_git("show", "-s", "--format=%cI", authorized_work_sha)
                message = (f"steward: accept {branch_name}\n\n"
                           f"Steward-Work: {authorized_work_sha}\n"
                           f"Steward-Base: {expected_base_sha}\n")
                def collapse(parent):
                    tree = self._agent_git("rev-parse", "HEAD^{tree}", cwd=worktree)
                    sha = agent_git(self.execution_broker,
                        *steward_commit_argv("commit-tree", tree, "-p", parent),
                        cwd=worktree, input_text=message,
                        env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
                    self._agent_git("reset", "--hard", sha, cwd=worktree)
                    return sha
                collapse(fork)
                error = reconcile_git(
                    worktree, expected_base_sha, branch_name,
                    broker=self.execution_broker, resolve_turn=None,
                )
                if error is not None:
                    return ValidationFailure(error)

                # Canonical identity after rebase: one parent, stable metadata.
                tested_sha = collapse(expected_base_sha)
                self.transport.import_candidate(
                    worktree, tested_sha, expected_base_sha, self.execution_broker,
                )
                passed, gate_results = self._run_gates(worktree)
                if not passed:
                    failed = next(result for result in gate_results if not result.passed)
                    return ValidationFailure(
                        f"Gate failed: {' '.join(failed.command)} "
                        f"(exit {failed.exit_code})\n"
                        f"stdout: {redact_command_output(failed.stdout, tail=True)}\n"
                        f"stderr: {redact_command_output(failed.stderr, tail=True)}",
                        candidate=tested_sha
                    )

                # Every gate exited 0, and the runner's integrity check proved
                # none moved HEAD, so the imported candidate is what was tested.
                return Tested(expected_base_sha, tested_sha)
            finally:
                self.worktree_mgr.remove_worktree(worktree_id)
                run_agent_git(
                    self.execution_broker,
                    "branch", "-D", landing_branch,
                    cwd=self.repo_path,
                    timeout=120,
                )

        except GitTransportError:
            raise
        except (
            RuntimeExecutionError,
            RuntimeUnavailable,
            OSError,
            subprocess.SubprocessError,
        ) as exc:
            return ValidationFailure(f"Promotion failure: {redact_command_output(str(exc))}", actionable=False)

    def _run_gates(self, worktree: Path) -> tuple[bool, list[GateResult]]:
        passed, results = GateRunner.run_all(
            self.config.gates, worktree, broker=self.execution_broker,
        )
        return passed, results
