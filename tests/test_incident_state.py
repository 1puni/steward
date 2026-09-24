"""First-principles incident policy over the replacement state kernel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from state_fixtures import FakeRemote, close_task_slice
from steward_harness.config.schema import IncidentPolicy
from steward_harness.state import (
    CheckpointDisposition,
    IncidentId,
    IncidentStatus,
    StateDatabase,
    TaskStatus,
)


SHA = "a" * 40


def _block_repair(state, task_id, reason):
    """One repair slice that ended blocked: its commit, then the decision."""
    close_task_slice(state, task_id, CheckpointDisposition.BLOCKED, detail=reason)



def test_landed_repair_budget_counts_merged_branches_inside_the_window(
    tmp_path: Path,
) -> None:
    """The 24h landed budget, now that a landing is ancestry rather than a row.

    It used to be counted from `promotion_publications.observed_at`. There is
    no publication row, so this walks the pipeline's repair branches and counts
    the ones the default branch contains whose last slice closed inside the
    window — commit time is landing time, because the push happens in the same
    breath as the gates.
    """
    clock = [datetime.now(UTC)]
    rule = IncidentPolicy(
        confirm_after_failures=1, transient_retries=0,
        repair_failure_cooldown_seconds=0, recovery_cooldown_seconds=0,
        landed_changes_per_24h=1,
    )
    state = StateDatabase(tmp_path / "state.db")
    _observe(state, at=clock[0], rule=rule)
    first = state.get_incident("orders").repair_task_id
    assert first is not None

    # One repair lands: its slice closes idle and the default branch takes it.
    close_task_slice(state, first, CheckpointDisposition.IDLE)
    FakeRemote().land(state, first, "repo")
    assert state.tasks.get(first).status is TaskStatus.DONE

    # The probe stays sick. Inside the window the budget of one is spent, so
    # no second repair is admitted.
    assert _observe(state, healthy=True, at=clock[0], rule=rule) is not None
    clock[0] += timedelta(hours=1)
    assert _observe(state, at=clock[0], rule=rule) is None
    assert state.get_incident("orders").repair_task_id is None

    # And the count is what does it: the same state with a budget of two
    # admits the next repair.
    generous = rule.model_copy(update={"landed_changes_per_24h": 2})
    assert _observe(state, at=clock[0], rule=generous) is not None
    second = state.get_incident("orders").repair_task_id
    assert second is not None and second != first


def _observe(
    state: StateDatabase,
    *,
    healthy: bool = False,
    at: datetime | None = None,
    rule: IncidentPolicy | None = None,
    automatic: bool = True,
):
    moment = at or datetime.now(UTC)
    return state.observe_incident(
        pipeline="orders",
        repository="repo",
        healthy=healthy,
        details="HTTP 200" if healthy else "HTTP 502",
        observed_at=moment.isoformat(),
        rule=rule
        or IncidentPolicy(
            confirm_after_failures=1,
            transient_retries=0,
            repair_failure_cooldown_seconds=0,
            recovery_cooldown_seconds=0,
            escalate_after_failed_repairs=2,
        ),
        automatic_repair=automatic,
        provider="any-provider",
        profile="balanced",
    )


def test_one_incident_task_is_retried_then_escalated_from_task_evidence(
    tmp_path: Path,
) -> None:
    state = StateDatabase(tmp_path / "state.db")

    admitted = _observe(state)

    current = state.get_incident("orders")
    assert current is not None and current.repair_task_id is not None
    repair_id = current.repair_task_id
    assert admitted == f"[orders] repair_admitted ({repair_id}): repair task admitted"
    repair = state.tasks.get(repair_id)
    assert repair.origin_ref == str(IncidentId("orders", 1))
    assert repair.status is TaskStatus.QUEUED

    _block_repair(state, repair_id, "first repair failed")
    with pytest.raises(PermissionError, match="only be retried by incidents"):
        state.tasks.retry(repair_id)

    retried = _observe(state)

    assert retried == (
        f"[orders] repair_retried ({repair_id}): retrying the retained incident task"
    )
    assert state.get_incident("orders").repair_task_id == repair_id
    assert state.tasks.get(repair_id).status is TaskStatus.QUEUED

    _block_repair(state, repair_id, "second repair failed")

    escalated = _observe(state)

    current = state.get_incident("orders")
    assert current is not None
    assert current.status is IncidentStatus.ESCALATED
    assert current.repair_task_id is None
    escalation_id = current.escalation_task_id
    assert escalation_id is not None
    assert state.tasks.get(escalation_id).status is TaskStatus.PROPOSED
    assert escalated == (
        f"[orders] escalated ({escalation_id}): 2 consecutive repair attempts failed"
    )

    assert state.tasks.confirm(escalation_id).status is TaskStatus.QUEUED


def test_operator_may_reject_escalation_without_granting_execution(
    tmp_path: Path,
) -> None:
    state = StateDatabase(tmp_path / "state.db")
    _observe(state)
    incident = state.get_incident("orders")
    assert incident is not None and incident.repair_task_id is not None
    _block_repair(state, incident.repair_task_id, "first repair failed")
    _observe(state)
    _block_repair(state, incident.repair_task_id, "second repair failed")
    _observe(state)
    escalated = state.get_incident("orders")
    assert escalated is not None and escalated.escalation_task_id is not None

    rejected = state.tasks.reject(escalated.escalation_task_id, "not authorized today")

    assert rejected.status is TaskStatus.CANCELLED
    assert rejected.reason == "not authorized today"
    assert state.tasks.queued() == ()


def test_probe_recovery_closes_cycle_and_later_failure_gets_new_identity(
    tmp_path: Path,
) -> None:
    state = StateDatabase(tmp_path / "state.db")
    _observe(state)
    first = state.get_incident("orders")
    assert first is not None and first.repair_task_id is not None

    recovered = _observe(state, healthy=True)

    assert recovered == "[orders] resolved: probe recovered"
    current = state.get_incident("orders")
    assert current is not None
    assert current.incident_id == IncidentId("orders", 2)
    assert current.status is IncidentStatus.HEALTHY
    assert state.tasks.get(first.repair_task_id).status is TaskStatus.CANCELLED

    _observe(state, at=datetime.now(UTC) + timedelta(seconds=1))

    reopened = state.get_incident("orders")
    assert reopened is not None and reopened.repair_task_id is not None
    assert reopened.repair_task_id != first.repair_task_id
    assert state.tasks.get(reopened.repair_task_id).origin_ref == str(IncidentId("orders", 2))


def test_monitor_only_confirms_truth_without_admitting_authority(
    tmp_path: Path,
) -> None:
    state = StateDatabase(tmp_path / "state.db")

    notice = _observe(state, automatic=False)

    assert notice is None
    current = state.get_incident("orders")
    assert current is not None and current.status is IncidentStatus.CONFIRMED
    assert current.repair_task_id is None
    assert state.tasks.all() == []


def test_probe_due_uses_one_durable_observation_timestamp(tmp_path: Path) -> None:
    state = StateDatabase(tmp_path / "state.db")
    observed = datetime.now(UTC)
    _observe(state, healthy=True, at=observed)

    assert not state.incident_probe_due(
        "orders", 300, now=observed + timedelta(seconds=299)
    )
    assert state.incident_probe_due(
        "orders", 300, now=observed + timedelta(seconds=300)
    )

