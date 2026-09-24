"""Probe effects and the boundary that permits incident repair admission."""

from __future__ import annotations

import sys
from subprocess import CompletedProcess
from pathlib import Path

import pytest

from steward_harness.config.schema import (
    CommandProbeSpec,
    CommandSpec,
    FilesystemProbeSpec,
    StewardConfig,
    UntrustedExecutionConfig,
)
from steward_harness.incidents.kernel import IncidentProbeLoop
from steward_harness.probes import ProbeRunner
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import StateDatabase


def _broker() -> UntrustedExecutionBroker:
    return UntrustedExecutionBroker(UntrustedExecutionConfig())


def test_filesystem_probe_healthy_on_matching_files(tmp_path: Path) -> None:
    (tmp_path / "data.log").write_text("x" * 10)
    probe = FilesystemProbeSpec(
        type="filesystem", path=str(tmp_path), pattern="*.log"
    )
    observation = ProbeRunner.execute(probe, broker=_broker())
    assert observation.healthy
    assert observation.probe_type == "filesystem"
    assert "1 files" in observation.details


def test_filesystem_probe_reports_missing_files(tmp_path: Path) -> None:
    probe = FilesystemProbeSpec(type="filesystem", path=str(tmp_path), pattern="*.log")
    observation = ProbeRunner.execute(probe, broker=_broker())
    assert not observation.healthy
    assert "0 files" in observation.details


def test_filesystem_probe_enforces_max_age(tmp_path: Path) -> None:
    stale = tmp_path / "old.log"
    stale.write_text("data")
    import os

    ancient = stale.stat().st_mtime - 7200
    os.utime(stale, (ancient, ancient))
    probe = FilesystemProbeSpec(
        type="filesystem",
        path=str(tmp_path),
        pattern="*.log",
        max_age_seconds=600,
    )
    observation = ProbeRunner.execute(probe, broker=_broker())
    assert not observation.healthy
    assert "old" in observation.details


def test_command_probe_healthy_on_zero_exit(tmp_path: Path) -> None:
    probe = CommandProbeSpec(
        type="command",
        command=CommandSpec(argv=(sys.executable, "-c", "print('ok')")),
    )
    observation = ProbeRunner.execute(probe, base_dir=tmp_path, broker=_broker())
    assert observation.healthy
    assert observation.probe_type == "command"


def test_command_probe_failing_exit(tmp_path: Path) -> None:
    probe = CommandProbeSpec(
        type="command",
        command=CommandSpec(argv=(sys.executable, "-c", "raise SystemExit(3)")),
    )
    observation = ProbeRunner.execute(probe, base_dir=tmp_path, broker=_broker())
    assert not observation.healthy
    assert "3" in observation.details


def test_observation_carries_timestamp(tmp_path: Path) -> None:
    (tmp_path / "a.log").write_text("x")
    probe = FilesystemProbeSpec(type="filesystem", path=str(tmp_path), pattern="*.log")
    observation = ProbeRunner.execute(probe, broker=_broker())
    assert observation.observed_at
    from datetime import datetime

    parsed = datetime.fromisoformat(observation.observed_at)
    assert parsed.tzinfo is not None


def test_every_probe_kind_crosses_the_untrusted_execution_boundary(
    tmp_path: Path,
) -> None:
    class RecordingBroker:
        def __init__(self) -> None:
            self.commands: list[tuple[str, ...]] = []

        def run(self, command, **_kwargs):
            normalized = tuple(command)
            self.commands.append(normalized)
            return CompletedProcess(normalized, 0, "probe healthy", "")

    broker = RecordingBroker()
    probes = (
        FilesystemProbeSpec(type="filesystem", path=str(tmp_path)),
    )

    for probe in probes:
        assert ProbeRunner.execute(  # type: ignore[arg-type]
            probe, base_dir=tmp_path, broker=broker
        ).healthy

    assert len(broker.commands) == 1
    assert broker.commands[0][0] == sys.executable


@pytest.mark.parametrize("kind", ["command", "filesystem"])
@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_probe_fault_cannot_admit_product_repair(tmp_path, kind, error_type):
    probe = {
        "command": {"type": "command", "command": {"argv": ["false"]}},
        "filesystem": {"type": "filesystem", "path": str(tmp_path)},
    }[kind]
    config = StewardConfig.model_validate({
        "identity": {"name": "Steward", "slug": "probe"},
        "provider": {"state_db": str(tmp_path / "state.db"), "workdir": str(tmp_path)},
        "repositories": {"app": {
            "path": str(tmp_path / "repository"),
            "remote_url": str(tmp_path / "remote.git"),
        }},
        "pipelines": {"health": {"repository": "app", "probe": probe}},
        "incident_policy": {"confirm_after_failures": 1, "transient_retries": 0},
    })
    state = StateDatabase(tmp_path / "state.db")
    failure = error_type("probe launch boundary failed")

    class FailingBroker:
        def run(self, *args, **kwargs):
            raise failure

    loop = IncidentProbeLoop(state, config, FailingBroker())
    if error_type is OSError:
        loop.observe("health")
        incident = state.get_incident("health")
        assert incident is not None and incident.repair_task_id is not None
        assert state.tasks.get(incident.repair_task_id).repository == "app"
        assert "probe launch boundary failed" in incident.last_details
    else:
        with pytest.raises(ValueError) as caught:
            loop.observe("health")
        assert caught.value is failure
        assert state.get_incident("health") is None
        assert state.tasks.all() == []
