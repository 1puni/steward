"""A settled target stops paying for the observation that told us it was settled."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace

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


class _Script:
    """A target whose next observation is chosen by the test, on a fake clock."""

    def __init__(self, targets, monkeypatch):
        self.targets, self.state, self.now = targets, "ready", 0.0
        monkeypatch.setattr(targets_module, "time", SimpleNamespace(monotonic=lambda: self.now))
        monkeypatch.setattr(targets, "call", self)
        # A satisfied observation would otherwise be reused, not re-observed.
        monkeypatch.setattr(targets_module, "SATISFIED_REOBSERVE_SECONDS", 0.0)

    def __call__(self, name, operation, repository, revision):
        if self.state == "failed":
            raise RuntimeError(f"target {name} observe failed (exit 1)")
        if operation == "apply":
            return None
        return Observation(revision=revision, ready=self.state == "ready",
                           busy=self.state == "busy", blocked=self.state == "blocked",
                           details=f"{self.state} at t={self.now}")

    def step(self, state, at=None):
        self.state = state
        if at is not None:
            self.now = at
        return self.targets.advance("production")

    def receipts(self):
        folder = self.targets.state.result_receipt_path("").parent
        return sorted((json.loads(p.read_text()) for p in folder.glob("*.json")),
                      key=lambda receipt: receipt["sequence"])


def test_progress_is_never_delivered_and_live_is_delivered_once(tmp_path, monkeypatch):
    with _harness(tmp_path) as (daemon, _clone, _calls):
        target = _Script(daemon._targets, monkeypatch)
        assert "not yet observed" in target.step("pending")
        assert "busy" in target.step("busy")
        target.step("pending")
        assert target.receipts() == []
        for _ in range(3):
            assert "satisfied" in target.step("ready")
        [live] = target.receipts()
        revision = live["observation"][0]
        assert live["observation"] == [revision, "satisfied"]
        # The driver's own words from the observation that reached it, once.
        assert "reply" not in live
        assert live["owner"] is None and live["task_id"] is None


def test_a_failure_that_recovers_before_it_persists_is_never_told(tmp_path, monkeypatch):
    with _harness(tmp_path) as (daemon, _clone, _calls):
        target = _Script(daemon._targets, monkeypatch)
        target.step("ready", at=0)
        # The controller's shutdown drain: observe exits 1 for a pass or two.
        target.step("failed", at=10)
        target.step("failed", at=20)
        target.step("ready", at=30)
        # Flapping under the window is not a stream of outages.
        for at in range(40, 1000, 20):
            target.step("failed" if at % 40 else "ready", at=at)
        assert [r["observation"][1] for r in target.receipts()] == ["satisfied"]


def test_a_persistent_failure_is_told_once_and_its_recovery_once(tmp_path, monkeypatch):
    with _harness(tmp_path) as (daemon, _clone, _calls):
        target = _Script(daemon._targets, monkeypatch)
        target.step("ready", at=0)
        target.step("failed", at=10)
        # Busy in between does not restart the clock on a failing target.
        target.step("busy", at=200)
        target.step("failed", at=299)
        assert len(target.receipts()) == 1
        target.step("failed", at=310)
        # Driver-reported block and an exception are the same outcome.
        target.step("blocked", at=320)
        target.step("failed", at=900)
        live, failed = target.receipts()
        assert failed["observation"] == [live["observation"][0], "failed"]
        assert "reply" not in failed
        assert "observe failed (exit 1)" in failed["result_text"]
        target.step("ready", at=910)
        target.step("ready", at=920)
        assert len(target.receipts()) == 3
        assert "reply" not in target.receipts()[-1]


def test_a_told_failure_survives_restart_without_being_told_again(tmp_path, monkeypatch):
    with _harness(tmp_path) as (daemon, _clone, _calls):
        monkeypatch.setattr(targets_module, "FAILURE_PERSISTS_SECONDS", 0.0)
        target = _Script(daemon._targets, monkeypatch)
        target.step("blocked")
        target.step("blocked")
        assert len(target.receipts()) == 1
        targets = daemon._targets
        restarted = Targets(targets.config, targets.state, targets.transports, targets.procedures)
        target = _Script(restarted, monkeypatch)
        target.step("blocked")
        assert len(target.receipts()) == 1
        target.step("ready")
        failed, recovered = target.receipts()
        assert [failed["observation"][1], recovered["observation"][1]] == ["failed", "satisfied"]
        assert failed["observation"][0] == recovered["observation"][0]
        assert all(r["owner"] is None and r["task_id"] is None for r in (failed, recovered))
        assert "reply" not in recovered


def test_receipts_from_before_outcomes_do_not_repeat_a_live_message(tmp_path, monkeypatch):
    with _harness(tmp_path) as (daemon, clone, _calls):
        revision = _git("rev-parse", "HEAD", cwd=clone)
        daemon._targets.state.save_result_receipt({
            "owner": "telegram:1", "task_id": None, "source_key": "target_result:old",
            "target": "production", "sequence": 3, "done": True, "result_text": "old",
            "observation": [revision, "satisfied", {"revision": revision, "ready": True,
                                                    "busy": False, "blocked": False}],
        })
        target = _Script(daemon._targets, monkeypatch)
        target.step("ready")
        assert len(target.receipts()) == 1


def test_an_owning_task_assesses_both_live_and_failure(tmp_path, monkeypatch):
    with _harness(tmp_path) as (daemon, clone, _calls):
        revision = _git("rev-parse", "HEAD", cwd=clone)
        targets = daemon._targets
        task = SimpleNamespace(task_id="task-1", landed=revision, landed_nothing=False, definition=SimpleNamespace(
            repository="app", owner="telegram:42"))
        monkeypatch.setattr(targets.state.tasks, "all", lambda: [task])
        target = _Script(targets, monkeypatch)
        target.step("pending", at=0)
        target.step("busy", at=1)
        assert target.receipts() == []
        target.step("ready", at=2)
        target.step("failed", at=3)
        target.step("failed", at=400)
        live, failed = target.receipts()
        assert (live["owner"], live["task_id"]) == ("telegram:42", "task-1")
        # Both outcomes belong to the owner to assess.
        assert "reply" not in live
        # A failure is the owner's to assess, so it carries no canned reply.
        assert (failed["owner"], failed["task_id"]) == ("telegram:42", "task-1")
        assert "reply" not in failed


def test_empty_site_target_does_not_claim_live_application(tmp_path, monkeypatch):
    with _harness(tmp_path) as (daemon, _clone, _calls):
        target = _Script(daemon._targets, monkeypatch)
        monkeypatch.setattr(daemon._targets, 'call', lambda name, operation, repository, revision:
                            Observation(revision=revision, ready=True, details='no sites declared'))
        target.step('ready')
        [receipt] = target.receipts()
        assert 'reply' not in receipt
        assert 'no sites declared' in receipt['result_text']
