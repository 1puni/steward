"""Subprocess gate runner for declared repository validation checks."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from steward_harness.config.schema import CommandSpec
from steward_harness.git import run_agent_git
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.contracts import RuntimeExecutionError
from steward_harness.runtime.process import ProcessController, ProcessTimeout


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


class GateRunner:
    """Runs test, lint, and build gates against a repository worktree."""

    INTEGRITY_CHECK = ("steward", "verify-clean-git-tree")

    @classmethod
    def _snapshot(
        cls, cwd: Path, broker: UntrustedExecutionBroker
    ) -> tuple[str, str] | GateResult:
        """Read the exact committed candidate without invoking repository hooks."""
        started = time.monotonic()
        try:
            head_result = run_agent_git(
                broker,
                "rev-parse",
                "HEAD",
                cwd=cwd,
                timeout=30,
            )
            if head_result.returncode != 0:
                raise subprocess.CalledProcessError(
                    head_result.returncode, head_result.args
                )
            head = head_result.stdout.strip()
            status_result = run_agent_git(
                broker,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                cwd=cwd,
                timeout=30,
            )
            if status_result.returncode != 0:
                raise subprocess.CalledProcessError(
                    status_result.returncode, status_result.args
                )
            status = status_result.stdout
            return head, status
        except (OSError, subprocess.SubprocessError) as exc:
            return GateResult(
                passed=False,
                command=cls.INTEGRITY_CHECK,
                exit_code=125,
                stdout="",
                stderr=f"Could not identify the exact gate input: {exc}",
                duration_seconds=round(time.monotonic() - started, 3),
            )

    @classmethod
    def _integrity_failure(cls, detail: str) -> GateResult:
        return GateResult(
            passed=False,
            command=cls.INTEGRITY_CHECK,
            exit_code=125,
            stdout="",
            stderr=detail,
            duration_seconds=0.0,
        )

    @staticmethod
    def run_command(
        spec: CommandSpec,
        cwd: Path,
        *,
        broker: UntrustedExecutionBroker,
    ) -> GateResult:
        started = time.monotonic()
        try:
            work_dir = broker.candidate_directory(cwd, spec.cwd)
        except ValueError as exc:
            return GateResult(
                passed=False,
                command=spec.argv,
                exit_code=1,
                stdout="",
                stderr=str(exc),
                duration_seconds=0.0,
            )

        try:
            res = ProcessController(broker).run(
                spec.argv, cwd=work_dir, env={},
                timeout_seconds=spec.timeout_seconds,
                command_only=True,
            )
            duration = time.monotonic() - started
            return GateResult(
                passed=(res.returncode == 0),
                command=spec.argv,
                exit_code=res.returncode,
                stdout=res.stdout,
                stderr=res.stderr,
                duration_seconds=round(duration, 3),
            )
        except ProcessTimeout:
            duration = time.monotonic() - started
            return GateResult(
                passed=False,
                command=spec.argv,
                exit_code=124,
                stdout="",
                stderr=f"Command timed out after {spec.timeout_seconds}s",
                duration_seconds=round(duration, 3),
            )
        except (OSError, RuntimeExecutionError) as exc:
            duration = time.monotonic() - started
            return GateResult(
                passed=False,
                command=spec.argv,
                exit_code=1,
                stdout="",
                stderr=f"Command execution failed: {exc}",
                duration_seconds=round(duration, 3),
            )

    @classmethod
    def run_all(
        cls,
        gates: Sequence[CommandSpec],
        cwd: Path,
        *,
        broker: UntrustedExecutionBroker,
    ) -> tuple[bool, list[GateResult]]:
        """Run gates only for one clean commit and prove they did not change it."""
        before = cls._snapshot(cwd, broker)
        if isinstance(before, GateResult):
            return False, [before]
        before_head, before_status = before
        if before_status:
            return False, [
                cls._integrity_failure("Gate input worktree is not clean")
            ]

        results: list[GateResult] = []
        for gate in gates:
            res = cls.run_command(gate, cwd, broker=broker)
            results.append(res)
            if not res.passed:
                break

        after = cls._snapshot(cwd, broker)
        if isinstance(after, GateResult):
            results.append(after)
            return False, results
        after_head, after_status = after
        if after_head != before_head or after_status:
            results.append(
                cls._integrity_failure(
                    "Gate mutated its input; HEAD and worktree must remain unchanged"
                )
            )
            return False, results
        return all(result.passed for result in results), results
