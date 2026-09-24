"""Save uncommitted task work without rewriting native Git history."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from steward_harness.git import (
    IN_PROGRESS_GIT_MARKERS,
    STEWARD_ACTOR_EMAIL,
    STEWARD_ACTOR_NAME,
    run_agent_git,
    steward_commit_argv,
)
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.task_lock import DISPOSITION_TRAILER, REASON_TRAILER


class WorktreeCheckpointError(RuntimeError):
    """Raised when dirty provider work cannot be preserved as a commit."""


@dataclass(frozen=True, slots=True)
class TickClosure:
    """Typed model proposal closing one coding tick over the staged diff."""

    subject: str
    disposition: Literal["continue", "idle", "ask"]
    blocking_question: str | None = None
    # Everything the session said before its three closure lines. For a tick
    # that stages no diff this is the whole product of the task.
    findings: str = ""


def parse_tick_closure(proposal: str | None, fallback_title: str) -> TickClosure:
    """Validate the working session's final closure; never infer publication intent."""
    body = (proposal or "").strip().splitlines()
    lines = body[-3:]
    names = ("COMMIT", "DISPOSITION", "QUESTION")
    if len(lines) != 3 or any(
        not line.startswith(f"{name}:") for name, line in zip(names, lines)
    ):
        raise ValueError("Final response must end with COMMIT, DISPOSITION and QUESTION")
    fields = {
        name: _one_line(line.partition(":")[2])
        for name, line in zip(names, lines)
    }
    disposition = fields["DISPOSITION"]
    if disposition not in {"continue", "idle", "ask"}:
        raise ValueError("Task disposition must be continue, idle or ask")
    question = fields["QUESTION"]
    if (not question or len(question) > 1000
        or (disposition == "ask" and question == "NONE")
        or (disposition != "ask" and question != "NONE")):
        raise ValueError("Task closure requires a question for ask, otherwise NONE")
    return TickClosure(
        subject=commit_subject("\n".join(lines), fallback_title),
        disposition=disposition,  # type: ignore[arg-type]
        blocking_question=question if disposition == "ask" else None,
        findings="\n".join(body[:-3]).strip(),
    )


def commit_subject(proposal: str | None, fallback_title: str) -> str:
    """Accept one bounded COMMIT line, or derive a safe deterministic fallback."""
    if proposal:
        for line in proposal.splitlines():
            if not line.startswith("COMMIT:"):
                continue
            candidate = _one_line(line.removeprefix("COMMIT:"))
            if candidate and candidate.upper() != "NONE":
                return candidate[:120].rstrip()
            break
    title = _one_line(fallback_title) or "checkpoint provider work"
    return f"steward: {title}"[:120].rstrip()


def _one_line(value: str) -> str:
    without_controls = "".join(
        " " if ord(character) < 32 or ord(character) == 127 else character
        for character in value
    )
    return re.sub(r"\s+", " ", without_controls).strip()


class WorktreeCheckpointer:
    """Commit remaining edits on the live branch, preserving existing commits."""

    def __init__(
        self,
        root: str | Path,
        *,
        execution_broker: UntrustedExecutionBroker,
        actor_name: str = STEWARD_ACTOR_NAME,
        actor_email: str = STEWARD_ACTOR_EMAIL,
    ) -> None:
        self.root = execution_broker.resolve_path(root)
        self.actor_name = actor_name
        self.actor_email = actor_email
        self.execution_broker = execution_broker

    def _git(
        self, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = run_agent_git(
                self.execution_broker,
                *args,
                cwd=self.root,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorktreeCheckpointError(f"Git checkpoint command failed: {exc}") from exc
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise WorktreeCheckpointError(
                f"Git checkpoint command exited {result.returncode}: {detail}"
            )
        return result

    def stage(
        self,
        *,
        expected_branch: str,
    ) -> bool:
        """Stage remaining edits and report whether a save commit is needed."""
        self._reject_in_progress_git_operation()
        actual_branch = self._git("branch", "--show-current").stdout.strip()
        if actual_branch != expected_branch:
            raise WorktreeCheckpointError(
                "Provider changed task branch: "
                f"expected {expected_branch!r}, found {actual_branch or '<detached>'!r}"
            )

        self._git("add", "--all")
        changed = self._git("diff", "--cached", "--quiet", check=False)
        if changed.returncode == 0:
            return False
        if changed.returncode != 1:
            detail = changed.stderr.strip() or changed.stdout.strip()
            raise WorktreeCheckpointError(f"Could not inspect staged work: {detail}")
        return True

    def _reject_in_progress_git_operation(self) -> None:
        for marker in IN_PROGRESS_GIT_MARKERS:
            raw_path = self._git("rev-parse", "--git-path", marker).stdout.strip()
            marker_path = Path(raw_path)
            if not marker_path.is_absolute():
                marker_path = self.root / marker_path
            if self.execution_broker.path_exists(marker_path):
                raise WorktreeCheckpointError(
                    f"Provider left an unfinished Git operation: {marker}"
                )

    def commit(
        self,
        subject: str,
        *,
        disposition: str | None = None,
        reason: str | None = None,
        findings: str | None = None,
    ) -> None:
        """Save the staged tree — and the slice with it.

        This commit is the whole record of the execution slice it closes.
        `disposition` and `reason` are written as trailers and `findings` as
        the message body, so how the slice ended, why, and what it concluded
        are metadata about the commit rather than content in it, which the
        session cannot edit.
        """
        message = ("-m", subject)
        if findings and findings.strip():
            message += ("-m", findings.strip())
        if disposition is not None:
            trailers = f"{DISPOSITION_TRAILER}: {disposition}"
            if reason and reason.strip():
                trailers += f"\n{REASON_TRAILER}: {_one_line(reason)}"
            message += ("-m", trailers)
        self._git(
            *steward_commit_argv(
                "commit",
                "--quiet",
                "--no-verify",
                "--allow-empty",
                *message,
                name=self.actor_name,
                email=self.actor_email,
            )
        )
