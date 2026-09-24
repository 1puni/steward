"""Capture current world files and each turn's exchange in Git."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from steward_harness.git import ISOLATED_GIT_ENV, agent_git, run_agent_git, steward_commit_argv
from steward_harness.turn_id import validate_turn_id

if TYPE_CHECKING:
    from steward_harness.runtime.execution import UntrustedExecutionBroker


#: Written on the commit that closes each world turn. Git owns the rest of the
#: exchange: the message body is what prompted the turn and what it replied.
TURN_TRAILER, BASE_TRAILER, SOURCE_TRAILER = "Steward-Turn", "Steward-Base", "Steward-Source"


class GitWorld:
    """Manages the Git repository that acts as durable cognitive state for the steward."""

    def __init__(
        self,
        root: str | Path,
        *,
        execution_broker: UntrustedExecutionBroker,
    ) -> None:
        self.root = execution_broker.resolve_path(root)
        self.execution_broker = execution_broker
        if not execution_broker.is_directory(self.root):
            raise ValueError(f"World root must be an existing directory: {self.root}")
        probe = run_agent_git(
            self.execution_broker,
            "rev-parse",
            "--show-toplevel",
            cwd=self.root,
            timeout=30,
            extra_env=ISOLATED_GIT_ENV,
        )
        top = execution_broker.resolve_path(probe.stdout.strip()) if probe.returncode == 0 else None
        if top != self.root:
            raise ValueError(f"World root must be a Git repository root: {self.root}")

    def _git(self, *args: str, input_text: str | None = None) -> str:
        return agent_git(
            self.execution_broker,
            *args,
            cwd=self.root,
            timeout=30,
            env=ISOLATED_GIT_ENV,
            input_text=input_text,
        )

    def input_cursor(self) -> str:
        """Return the exact committed Git-world input boundary."""
        return self._git("rev-parse", "HEAD")

    def finish(self, event_id: str, user_text: str, reply_text: str, *,
               base: str, source: str) -> None:
        """Commit this attempt's files as the turn, its exchange as the message.

        A retried unprepared attempt amends its own earlier commit rather than
        adding a second one for the same turn.
        """
        validate_turn_id(event_id)
        filters = run_agent_git(
            self.execution_broker,
            "config",
            "--get-regexp",
            r"^filter\..*\.(clean|process)$",
            cwd=self.root,
            timeout=30,
            extra_env=ISOLATED_GIT_ENV,
        )
        if filters.returncode not in {0, 1}:
            raise subprocess.CalledProcessError(
                filters.returncode,
                filters.args,
                output=filters.stdout,
                stderr=filters.stderr,
            )
        if filters.stdout.strip():
            raise ValueError("Git-world checkpoints do not permit content filters")
        self._git("add", "--all")
        amend = self.trailers("HEAD").get(TURN_TRAILER) == event_id
        message = (f"steward: turn {event_id}\n\nInput:\n{user_text.strip()}\n\n"
                   f"Reply:\n{reply_text.strip() or '(none)'}\n\n"
                   f"{TURN_TRAILER}: {event_id}\n{BASE_TRAILER}: {base}\n{SOURCE_TRAILER}: {source}\n")
        self._git(*steward_commit_argv(
            "commit", "--allow-empty", "--quiet", *(("--amend",) if amend else ()), "-F", "-",
        ), input_text=message)

    def trailers(self, revision: str) -> dict[str, str]:
        """The steward trailers on one world commit; empty when it has none."""
        fields = self._git("log", "-1", "--format=" + "%x00".join(
            f"%(trailers:key={key},valueonly)" for key in (TURN_TRAILER, BASE_TRAILER, SOURCE_TRAILER)
        ), revision, "--").split("\0")
        return {key: value.strip() for key, value in
                zip((TURN_TRAILER, BASE_TRAILER, SOURCE_TRAILER), fields) if value.strip()}

    def turn_sources(self, revisions) -> dict[str, tuple[str, str]]:
        """(source, base) of each named commit that closed a world turn."""
        listed = agent_git(
            self.execution_broker, "log", "--no-walk=unsorted", "--ignore-missing", "--stdin",
            f"--format=%H%x00%(trailers:key={SOURCE_TRAILER},valueonly)%x00%(trailers:key={BASE_TRAILER},valueonly)%x01",
            cwd=self.root, timeout=30, env=ISOLATED_GIT_ENV,
            input_text="".join(f"{revision}\n" for revision in revisions),
        )
        found = {}
        for record in listed.split("\x01"):
            if record.strip():
                sha, source, base = record.strip("\n").split("\0")
                if source.strip() and base.strip():
                    found[sha] = (source.strip(), base.strip())
        return found
