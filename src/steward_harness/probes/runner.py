"""Deterministic health probes: a command, or a produced-files freshness check."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from steward_harness.config.schema import (
    CommandProbeSpec,
    FilesystemProbeSpec,
    ProbeSpec,
)
from steward_harness.runtime.execution import UntrustedExecutionBroker


@dataclass(frozen=True, slots=True)
class ProbeObservation:
    healthy: bool
    probe_type: str
    details: str
    observed_at: str


class ProbeRunner:
    """Executes declared probes and returns normalized observations."""

    @classmethod
    def execute(
        cls,
        probe: ProbeSpec,
        base_dir: Path | None = None,
        *,
        broker: UntrustedExecutionBroker,
    ) -> ProbeObservation:
        now_iso = datetime.now(timezone.utc).isoformat()
        work_dir = base_dir or Path(".")

        try:
            if isinstance(probe, CommandProbeSpec):
                return cls._run_command(probe, work_dir, now_iso, broker=broker)
            if isinstance(probe, FilesystemProbeSpec):
                return cls._run_filesystem(probe, work_dir, now_iso, broker=broker)
        except (OSError, subprocess.SubprocessError) as error:
            return ProbeObservation(
                healthy=False,
                probe_type=probe.type,
                details=f"Probe failed to run: {error}",
                observed_at=now_iso,
            )
        raise TypeError(f"Unsupported probe specification: {type(probe).__name__}")

    @staticmethod
    def _run_command(
        probe: CommandProbeSpec,
        base_dir: Path,
        now_iso: str,
        *,
        broker: UntrustedExecutionBroker,
    ) -> ProbeObservation:
        work_dir = (base_dir / probe.command.cwd).resolve()
        res = broker.run(
            probe.command.argv,
            cwd=work_dir,
            timeout=probe.command.timeout_seconds,
        )
        healthy = res.returncode == 0
        details = f"Command exited {res.returncode}"
        if not healthy and res.stderr:
            details += f": {res.stderr[:200]}"
        return ProbeObservation(
            healthy=healthy,
            probe_type="command",
            details=details,
            observed_at=now_iso,
        )

    @classmethod
    def _run_filesystem(
        cls,
        probe: FilesystemProbeSpec,
        work_dir: Path,
        now_iso: str,
        *,
        broker: UntrustedExecutionBroker,
    ) -> ProbeObservation:
        script = """
import pathlib
import sys
import time

root = pathlib.Path(sys.argv[1])
if not root.is_dir():
    print(f"Directory not found: {root}")
    raise SystemExit(1)
glob = root.rglob if sys.argv[3] == "1" else root.glob
files = [path for path in glob(sys.argv[2]) if path.is_file()]
minimum_files = int(sys.argv[4])
if len(files) < minimum_files:
    print(f"Found {len(files)} files (minimum {minimum_files})")
    raise SystemExit(1)
total = sum(path.stat().st_size for path in files)
minimum_bytes = int(sys.argv[5])
if total < minimum_bytes:
    print(f"Total size {total} bytes (minimum {minimum_bytes})")
    raise SystemExit(1)
if sys.argv[6]:
    maximum_age = int(sys.argv[6])
    age = time.time() - max(path.stat().st_mtime for path in files)
    if age > maximum_age:
        print(f"Newest file is {int(age)}s old (max allowed {maximum_age}s)")
        raise SystemExit(1)
print(f"Matched {len(files)} files ({total} bytes)")
"""
        result = broker.run(
            (
                sys.executable,
                "-c",
                script,
                probe.path,
                probe.pattern,
                "1" if probe.recursive else "0",
                str(probe.minimum_files),
                str(probe.minimum_bytes),
                str(probe.max_age_seconds or ""),
            ),
            cwd=work_dir,
            timeout=30,
        )

        return cls._command_observation("filesystem", result, now_iso)

    @staticmethod
    def _command_observation(
        probe_type: str,
        result: Any,
        now_iso: str,
    ) -> ProbeObservation:
        output = (result.stdout or result.stderr or "probe returned no details").strip()
        return ProbeObservation(
            healthy=result.returncode == 0,
            probe_type=probe_type,
            details=output[:1000],
            observed_at=now_iso,
        )
