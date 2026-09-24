"""State-native probe observation and incident task admission."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from steward_harness.config.schema import RepairMode, StewardConfig
from steward_harness.probes import ProbeRunner
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import StateDatabase


class IncidentProbeLoop:
    """Run due declared probes and admit repair work into the task path."""

    def __init__(
        self,
        state: StateDatabase,
        config: StewardConfig,
        broker: UntrustedExecutionBroker,
        *,
        notify: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state = state
        self.config = config
        self.broker = broker
        self.notify = notify or (lambda _message: None)
        self.clock = clock or (lambda: datetime.now(UTC))

    def observe(self, name: str) -> None:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("incident probe clock must be timezone-aware")
        pipeline = self.config.pipelines[name]
        if self.state.paused() or not self.state.incident_probe_due(
            name, pipeline.observation_interval_seconds, now=now
        ):
            return None
        repository = self.config.repositories[pipeline.repository]
        observation = ProbeRunner.execute(
            pipeline.probe, Path(repository.path), broker=self.broker,
        )
        if self.state.paused():
            return None
        notice = self.state.observe_incident(
            pipeline=name,
            repository=pipeline.repository,
            healthy=observation.healthy,
            details=f"{observation.probe_type}: {observation.details}",
            observed_at=observation.observed_at,
            rule=self.config.incident_policy,
            automatic_repair=pipeline.repair_mode is RepairMode.AUTONOMOUS,
            provider=self.config.provider.default_family,
            profile=self.config.provider.default_profile,
        )
        if notice is not None:
            self.notify(notice)
