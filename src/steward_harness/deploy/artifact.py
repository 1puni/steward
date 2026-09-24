"""Credentialless construction; controller-owned import of a runnable source tree."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tarfile
import tempfile
from pathlib import Path

from steward_harness.config.schema import CommandSpec
from steward_harness.runtime.execution import UntrustedExecutionBroker

# The child alone opens the mutable build workspace. The controller receives an
# archive through an already-open descriptor, never by following agent paths.
_BUILD = r"""
import json, pathlib, subprocess, sys, tarfile, tempfile
command = json.loads(sys.argv[1])
with tempfile.TemporaryDirectory(prefix="steward-artifact-") as name:
    root = pathlib.Path(name).resolve()
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as archive:
        archive.extractall(root, filter="data")
    cwd = (root / command["cwd"]).resolve()
    if not cwd.is_relative_to(root):
        raise ValueError("artifact build cwd escapes source tree")
    result = subprocess.run(command["argv"], cwd=cwd, stdout=sys.stderr)
    if result.returncode:
        raise SystemExit(result.returncode)
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        archive.add(root, arcname=".")
"""


def construct_artifact(
    source: Path, destination: Path, command: CommandSpec,
    broker: UntrustedExecutionBroker,
) -> None:
    """Import a private snapshot, rejecting links and any changed committed file.

    Portable dependencies must be regular files. Absolute-path virtualenvs and
    external interpreter links are not self-contained release artifacts.
    """
    with (
        tempfile.TemporaryFile() as incoming,
        tempfile.TemporaryFile() as outgoing,
        tempfile.TemporaryFile() as errors,
    ):
        with tarfile.open(fileobj=incoming, mode="w") as archive:
            archive.add(source, arcname=".")
        incoming.seek(0)
        process = broker.popen_command(
            [broker.python_executable, "-c", _BUILD, json.dumps(command.model_dump(mode="json"))],
            cwd="/", stdin=incoming, stdout=outgoing, stderr=errors,
        )
        try:
            returncode = process.wait(timeout=command.timeout_seconds)
        except subprocess.TimeoutExpired:
            raise ValueError("artifact build timed out") from None
        finally:
            # The broker owns cleanup in the execution namespace.
            try:
                broker.signal_process(process, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if returncode:
            errors.seek(0, os.SEEK_END)
            errors.seek(max(0, errors.tell() - 4000))
            detail = errors.read().decode(errors="replace")
            raise ValueError(f"artifact build exited {returncode}: {detail}")
        outgoing.seek(0)
        with tarfile.open(fileobj=outgoing, mode="r:") as archive:
            def regular_only(member: tarfile.TarInfo, target: str) -> tarfile.TarInfo:
                if not (member.isfile() or member.isdir()):
                    raise ValueError(
                        f"artifact contains a link or special file: {member.name}"
                    )
                return tarfile.data_filter(member, target)
            archive.extractall(destination, filter=regular_only)
    for path in destination.rglob("*"):
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
    # Source identity belongs to the controller's exact Git tree. A build may
    # add dependencies, but cannot silently replace or remove committed code.
    for original in source.rglob("*"):
        imported = destination / original.relative_to(source)
        if original.is_symlink():
            raise ValueError("artifact source contains a symlink")
        if original.is_dir():
            if not imported.is_dir():
                raise ValueError("artifact build removed a committed directory")
        else:
            if imported.is_file():
                with original.open("rb") as before, imported.open("rb") as after:
                    unchanged = (
                        hashlib.file_digest(before, "sha256").digest()
                        == hashlib.file_digest(after, "sha256").digest()
                        and bool(original.stat().st_mode & 0o111)
                        == bool(imported.stat().st_mode & 0o111)
                    )
            else:
                unchanged = False
            if not unchanged:
                raise ValueError(
                    f"artifact build changed committed source: {original.relative_to(source)}"
                )
