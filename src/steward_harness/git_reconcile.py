"""One bounded Git reconciliation path for repository and world candidates."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from steward_harness.git import (
    ISOLATED_GIT_ENV,
    redact_command_output,
    run_agent_git,
    steward_commit_argv,
)
from steward_harness.runtime.contracts import RuntimeExecutionError, RuntimeUnavailable
from steward_harness.runtime.execution import UntrustedExecutionBroker


@dataclass(frozen=True, slots=True)
class ResolverTurn:
    """Context handed to one conflict-resolution provider turn."""

    worktree: Path
    conflicted_files: tuple[str, ...]
    base_ref: str
    branch: str
    stop_index: int


ResolveTurn = Callable[[ResolverTurn], None]


def _git_failure(stage: str, result: subprocess.CompletedProcess[str]) -> str:
    return f"Git {stage} failed (exit {result.returncode}): {redact_command_output(result.stderr or result.stdout)}"


def _reconcile_env() -> dict[str, str]:
    return {"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true", **ISOLATED_GIT_ENV}


def _reconcile_git(
    broker: UntrustedExecutionBroker, worktree: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return run_agent_git(
        broker, *steward_commit_argv(*args), cwd=worktree, timeout=120,
        extra_env=_reconcile_env(),
    )


def reconcile_git(
    worktree: Path, base_ref: str, branch: str, *,
    broker: UntrustedExecutionBroker,
    resolve_turn: ResolveTurn | None,
    max_stops: int = 20,
) -> str | None:
    """Rebase onto ``base_ref``, resolving each conflict with ``resolve_turn``.

    Without a resolver the first conflict aborts the rebase and names the
    conflicted files, so their owner can reconcile and submit again.
    """
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return _reconcile_git(broker, worktree, *args)

    def git_checked(*args: str) -> str:
        result = git(*args)
        if result.returncode:
            raise RuntimeExecutionError(_git_failure(args[0], result))
        return result.stdout.strip()

    # Cancellation used to be re-asked here between every git
    # invocation. It is SIGTERM to the child now; a withdrawn task is
    # read from its marker at the decision points that write it down.
    ancestry = git(
        "merge-base", "--is-ancestor", base_ref, "HEAD"
    )
    if ancestry.returncode == 0:
        return None
    if ancestry.returncode != 1:
        return _git_failure("ancestry", ancestry)
    # A commit whose change the base already holds still records its turn:
    # keep it rather than let the rebase drop it as empty or upstream.
    rebase = git(
        "rebase", "--merge", "--empty=keep", "--reapply-cherry-picks", base_ref
    )
    stops = 0
    stage = "rebase"
    try:
        while rebase.returncode != 0:
            files = tuple(git_checked(
                "diff", "--name-only", "--diff-filter=U"
            ).splitlines())
            error = None
            if not files:
                error = _git_failure(stage, rebase)
            elif resolve_turn is None:
                conflicted = redact_command_output(", ".join(files))
                error = (f"Rebase conflict in {conflicted}; "
                         f"reconcile the retained branch with {base_ref} and submit again")
            elif stops >= max_stops:
                error = f"Rebase conflict: exhausted {max_stops} resolution stops"
            if error is not None:
                return error

            stops += 1
            try:
                resolve_turn(ResolverTurn(
                    worktree, files, base_ref, branch, stops
                ))
            except (RuntimeExecutionError, RuntimeUnavailable) as exc:
                return f"Rebase resolver {type(exc).__name__}: {redact_command_output(str(exc))}"
            # Git validates added conflict markers and supports resolutions
            # that delete files. No parallel filesystem conflict scanner.
            checked = git("diff", "--check")
            if checked.returncode:
                return _git_failure("resolver output", checked)
            git_checked("add", "--", *files)
            rebase = git(
                "rebase", "--continue"
            )
            stage = "rebase continuation"
        if resolve_turn is not None and git_checked("status", "--porcelain"):
            # Native resolver records and memories are part of its candidate.
            # Save them before gates/acceptance, after the rebase has finished.
            git_checked("add", "--all")
            git_checked("commit", "-qm", "steward: preserve reconciliation work")
        return None
    finally:
        if rebase.returncode != 0:
            git("rebase", "--abort")
