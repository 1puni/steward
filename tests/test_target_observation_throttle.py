"""A settled target stops paying for the observation that told us it was settled."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys

from steward_harness import targets as targets_module
from steward_harness.config.schema import StewardConfig, UntrustedExecutionConfig
from steward_harness.daemon import StewardDaemon
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import StateDatabase
from steward_harness.targets import Observation, Targets
from test_task_no_changes import InvestigationAdapter
from test_task_runner_kernel import _git, _repository


@contextmanager
def _harness(tmp_path):
    """A daemon with one target whose driver is always ready and counts its calls."""
    remote, clone = _repository(tmp_path)
    calls = tmp_path / "driver-calls"
    driver = tmp_path / "installed-driver"
    driver.write_text(f"#!{sys.executable}\n" + f'''import json,sys
from pathlib import Path
request=json.load(sys.stdin)
with Path({str(calls)!r}).open("a") as handle:
    handle.write(sys.argv[1] + "\\n")
if sys.argv[1]=="observe":
    print(json.dumps(dict(revision=request["revision"], ready=True)))
''')
    driver.chmod(0o755)
    config = StewardConfig.model_validate(dict(
        identity=dict(name="test", slug="test"),
        provider=dict(state_db=str(tmp_path / "state.db"), workdir=str(tmp_path / "work"),
                      default_family="claude", fallback_families=[]),
        repositories={"app": dict(path=str(clone), remote_url=str(remote))},
        targets={"production": dict(ref="repositories/app/main", driver=str(driver))},
    ))
    Path(config.provider.workdir).mkdir()
    StateDatabase(config.provider.state_db)
    daemon = StewardDaemon(config, tmp_path / "config.yaml", adapters={"claude": InvestigationAdapter()},
                           broker=UntrustedExecutionBroker(UntrustedExecutionConfig()))
    with daemon._daemon_lease():
        # Builds the lanes, including `_targets`. Nothing is stepped: these
        # tests drive the target lane by hand.
        daemon._start_owned()
        try:
            yield daemon, clone, calls
        finally:
            daemon.stop()


def _observations(calls: Path) -> int:
    return calls.read_text().count("observe") if calls.exists() else 0


def test_satisfied_target_is_not_re_observed_every_pass(tmp_path):
    with _harness(tmp_path) as (daemon, _clone, calls):
        assert "satisfied" in daemon._targets.advance("production")
        assert _observations(calls) == 1

        for _ in range(5):
            message = daemon._targets.advance("production")
        assert _observations(calls) == 1, "a settled target kept spawning its driver"
        assert "not re-observed" in message

        # The receipt of an observation that did not happen would report a
        # transition the target never made.
        receipts = daemon._targets.state.result_receipt_path("").parent
        assert len(list(receipts.glob("*.json"))) == 1


def test_satisfied_target_is_not_scheduled_until_ref_or_observation_changes(tmp_path, monkeypatch):
    with _harness(tmp_path) as (daemon, clone, calls):
        targets = daemon._targets
        assert targets.due("production")
        targets.advance("production")
        assert not targets.due("production")
        # The pass consults local truth; it never fetches while deriving work.
        transport = targets.transports["app"]
        fetch = transport.fetch
        monkeypatch.setattr(transport, "fetch", lambda: (_ for _ in ()).throw(
            AssertionError("network work on the pass thread")))
        assert not targets.due("production")
        (clone / "README.md").write_text("moved\n")
        _git("commit", "-qam", "move main", cwd=clone)
        _git("push", "-q", "origin", "main", cwd=clone)
        fetch()
        assert targets.due("production")
        monkeypatch.setattr(transport, "fetch", fetch)
        targets.advance("production")
        assert not targets.due("production")
        monkeypatch.setattr(targets_module, "SATISFIED_REOBSERVE_SECONDS", 0.0)
        assert targets.due("production")


def test_reobservation_resumes_on_a_moved_ref_a_demand_and_the_interval(tmp_path, monkeypatch):
    with _harness(tmp_path) as (daemon, clone, calls):
        daemon._targets.advance("production")
        assert _observations(calls) == 1

        # An operator asking directly is owed live truth, not the cached answer.
        daemon._targets.advance("production", force=True)
        assert _observations(calls) == 2

        # A new commit is observed on the very next pass: the throttle covers
        # re-confirmation, never deployment latency.
        (clone / "README.md").write_text("moved\n")
        _git("commit", "-qam", "move main", cwd=clone)
        _git("push", "-q", "origin", "main", cwd=clone)
        daemon._targets.advance("production")
        assert _observations(calls) == 3

        # And an unchanged target is re-observed once the interval lapses.
        daemon._targets.advance("production")
        assert _observations(calls) == 3
        monkeypatch.setattr(targets_module, "SATISFIED_REOBSERVE_SECONDS", 0.0)
        assert "satisfied" in daemon._targets.advance("production")
        assert _observations(calls) == 4


def test_unowned_target_retains_failure_and_same_revision_recovery_across_restart(tmp_path, monkeypatch):
    with _harness(tmp_path) as (daemon, _clone, _calls):
        targets = daemon._targets
        blocked = True
        def observe(name, operation, repository, revision):
            assert operation == "observe"
            return Observation(revision=revision, ready=not blocked, blocked=blocked,
                               details="repair needed" if blocked else "repaired")
        monkeypatch.setattr(targets, "call", observe)
        targets.advance("production")
        targets.advance("production")
        # Repeated failure is one retained transition, even without a task.
        receipts = targets.state.result_receipt_path("").parent
        assert len(list(receipts.glob("*.json"))) == 1
        targets = Targets(targets.config, targets.state, targets.transports, targets.procedures)
        monkeypatch.setattr(targets, "call", observe)
        targets.advance("production")
        assert len(list(receipts.glob("*.json"))) == 1
        blocked = False
        targets.advance("production")
        retained = sorted((json.loads(p.read_text()) for p in receipts.glob("*.json")),
                          key=lambda receipt: receipt["sequence"])
        assert [r["observation"][1] for r in retained] == ["blocked", "satisfied"]
        assert all(r["task_id"] is None and r["owner"] is None and r["reply"] for r in retained)
        assert retained[0]["observation"][0] == retained[1]["observation"][0]
