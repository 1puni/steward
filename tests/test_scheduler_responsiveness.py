"""Slow execution must block only the durable owner that it actually needs.

What is asserted here is the property, through a real daemon. Three tests
that constructed a `ConcurrentLoop` and checked its worker pool overlapped
two callables went with the pool: they asserted the mechanism, and the
mechanism is now `controller.workers` on one shared executor.
"""

import json
import threading
import time

import pytest

from state_fixtures import admit_task
from steward_harness.config.schema import StewardConfig, UntrustedExecutionConfig
from steward_harness.daemon import StewardDaemon
from steward_harness.desk import DeskEvents
from steward_harness.incidents.kernel import IncidentProbeLoop
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import StateDatabase, TaskSpec
from steward_harness.repository_reconciler import RepositoryReconciler
from steward_harness.task_runner import TaskRunner
from steward_harness.lease import Busy
from test_daemon import _cancel_conversation_task
from world_fixtures import ReadingAdapter


@pytest.mark.parametrize("blocked", ["scheduler", "desk", "task-results", "repository-observation", "incident-probes"])
def test_slow_owner_does_not_block_the_other_drains(tmp_path, monkeypatch, blocked):
    work = tmp_path / "work"
    work.mkdir()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    config = StewardConfig.model_validate({
        "identity": {"name": "Steward", "slug": "test"},
        "provider": {
            "state_db": str(tmp_path / "state.db"), "workdir": str(work),
            "fallback_families": [],
        },
        "controller": {"poll_seconds": 1},
        "repositories": {"app": {
            "path": str(work), "remote_url": str(tmp_path / "remote.git"),
        }},
        "pipelines": {"health": {
            "repository": "app", "probe": {"type": "command", "command": {"argv": ["true"]}},
        }},
        "desk": {"inbox_dir": str(inbox), "events_file": str(tmp_path / "events.jsonl")},
    })
    active, release = threading.Event(), threading.Event()
    progressed = {name: threading.Event() for name in (
        "scheduler", "desk", "task-results", "repository-observation", "incident-probes",
    )}
    failures = []

    def advance(name):
        if name == blocked:
            active.set()
            assert release.wait(10), "test did not release the blocked owner"
        progressed[name].set()

    def respond(request):
        if "Harness task result" in request.prompt:
            advance("task-results")
            return ""
        advance("desk")
        return "Desk completed."

    daemon = StewardDaemon(
        config, tmp_path / "steward.yaml",
        adapters={"codex": ReadingAdapter("session", respond)},
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
    )
    state = StateDatabase(config.provider.state_db)
    # Local fake providers/Git; production split-identity enforcement has its own tests.
    monkeypatch.setattr(daemon, "_require_boundary", lambda: None)
    monkeypatch.setattr(TaskRunner, "prepare", lambda *args, **kwargs: advance("scheduler"))
    monkeypatch.setattr(RepositoryReconciler, "publish_repository", lambda self, name: advance("repository-observation"))
    monkeypatch.setattr(IncidentProbeLoop, "observe", lambda self, name: advance("incident-probes"))
    admit_task(
        state,
        TaskSpec("app", "Scheduler work", "Exercise independent execution"),
        provider="codex",
        profile="balanced",
    )
    delivered = threading.Event()
    append = DeskEvents.append

    def record(events, kind, text, msg_id=""):
        append(events, kind, text, msg_id)
        if "Task cancelled" in text:
            delivered.set()

    monkeypatch.setattr(DeskEvents, "append", record)

    def queue_desk():
        # Publish a complete job atomically while the inbox reader is active.
        pending = inbox / "1-message.tmp"
        pending.write_text(json.dumps({
            "id": "message", "kind": "message", "text": "Please inspect", "topic": 2,
        }))
        pending.replace(inbox / "1-message.json")

    if blocked == "desk":
        queue_desk()
    if blocked == "task-results":
        _cancel_conversation_task(state, "desk", "7")

    def run():
        try:
            daemon.run_forever(poll_seconds=0.01)
        except BaseException as error:
            failures.append(error)

    owner = threading.Thread(target=run)
    owner.start()
    try:
        assert active.wait(5)
        if blocked != "desk":
            queue_desk()
        if blocked != "task-results":
            _cancel_conversation_task(state, "desk", "7")
        for name, event in progressed.items():
            if name != blocked:
                assert event.wait(5), f"{name} waited behind {blocked}"
        if blocked != "task-results":
            assert delivered.wait(5), "the result did not reach its transport"
        assert not progressed[blocked].is_set()
        with pytest.raises(Busy):
            with daemon._daemon_lease():
                pytest.fail("a second daemon acquired a live owner's lease")
    finally:
        daemon._stop.set()
        release.set()
        owner.join(5)
    assert not owner.is_alive()
    assert failures == []
    assert delivered.is_set()


def test_real_observations_admit_work_while_a_task_owns_execution(tmp_path, monkeypatch):
    from unowned_repo_fixtures import _git, _repository

    remote, repository, base, work = _repository(tmp_path)
    config = StewardConfig.model_validate({
        "identity": {"name": "Steward", "slug": "test"},
        "provider": {
            "state_db": str(tmp_path / "state.db"), "workdir": str(repository),
            "fallback_families": [],
        },
        "controller": {"poll_seconds": 1},
        "repositories": {"app": {
            "path": str(repository), "remote_url": str(remote),
        }},
        "pipelines": {"health": {
            "repository": "app", "probe": {"type": "command", "command": {"argv": ["false"]}},
        }},
        "incident_policy": {"confirm_after_failures": 1, "transient_retries": 0},
    })
    daemon = StewardDaemon(
        config, tmp_path / "steward.yaml", adapters={},
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
    )
    state = StateDatabase(config.provider.state_db)
    task = admit_task(
        state,
        TaskSpec("app", "Already executing", "Retain execution ownership"),
        provider="codex",
        profile="balanced",
    ).task_id
    state.set_paused(True)
    active, release = threading.Event(), threading.Event()
    observed, probed = threading.Event(), threading.Event()
    failures = []
    observation = StateDatabase.observe_incident
    observations = []

    def observe_repository(self, repository_name):
        # Ambient observation is the publish path now. Only the gating and
        # pushing is stubbed out here; the remote observation it performs is
        # real, because the incident probes read the ref it establishes.
        self.transports[repository_name].sync_remote_to_agent(
            self.repositories[repository_name].path, self.broker
        )
        observations.append(repository_name)
        observed.set()
        return None

    def probe(*args, **kwargs):
        result = observation(*args, **kwargs)
        probed.set()
        return result

    def execute(runner, task_id, **kwargs):
        if task_id == task:
            active.set()
            assert release.wait(10)
        return task_id

    monkeypatch.setattr(RepositoryReconciler, "publish_one", observe_repository)
    monkeypatch.setattr(StateDatabase, "observe_incident", probe)
    # One dispatch path now: a task interrupted mid-slice never left the queue,
    # so resuming it is the same call that starts one.
    monkeypatch.setattr(TaskRunner, "prepare", execute)
    # Observation *is* `_advance_repository` now — it takes the repository
    # lock and calls `publish_one`, which is the stub above. There is no
    # second, unlocked loop doing the same call beside it any more.
    monkeypatch.setattr(daemon, "_require_boundary", lambda: None)

    def run():
        try:
            daemon.run_forever(poll_seconds=0.01)
        except BaseException as error:
            failures.append(error)

    owner = threading.Thread(target=run)
    owner.start()
    try:
        assert observed.wait(5), "repository observation waited for the pause"
        assert not probed.is_set(), "pause admitted a probe"
        # Pause now covers resumption too: `running` is a lock, so a task whose
        # slice was cut short is an ordinary queued task, and pause stops it.
        assert not active.is_set(), "pause admitted task execution"
        state.set_paused(False)
        assert active.wait(5)
        assert probed.wait(5), "probe waited for task execution"
        admissions = len(observations)
        deadline = time.monotonic() + 5
        while len(observations) == admissions:
            assert time.monotonic() < deadline, (
                "repository observation waited for task execution"
            )
            threading.Event().wait(0.01)
        assert task in state.tasks.queued()
        incident = state.get_incident("health")
        assert incident.repair_task_id is not None
        assert incident.repair_task_id != task
        assert _git("rev-parse", "main", cwd=remote) == base
    finally:
        daemon._stop.set()
        release.set()
        owner.join(5)
    assert not owner.is_alive()
    assert failures == []
    # It never left the queue, which is the whole of what used to need a
    # separate recovery list.
    assert task in state.tasks.queued()


def test_an_operator_desk_message_is_answered_without_waiting_for_a_pass(tmp_path, monkeypatch):
    """A phone caller waits in silence: the answer may not queue behind the poll."""
    work = tmp_path / "work"
    work.mkdir()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    events = tmp_path / "events.jsonl"
    config = StewardConfig.model_validate({
        "identity": {"name": "Steward", "slug": "test"},
        "provider": {
            "state_db": str(tmp_path / "state.db"), "workdir": str(work),
            "fallback_families": [],
        },
        "controller": {"poll_seconds": 60},
        "repositories": {"app": {
            "path": str(work), "remote_url": str(tmp_path / "remote.git"),
        }},
        "desk": {"inbox_dir": str(inbox), "events_file": str(events)},
    })
    prompts = []

    def answer(request):
        prompts.append(request.prompt)
        return "Hello from the desk."

    daemon = StewardDaemon(
        config, tmp_path / "steward.yaml",
        adapters={"codex": ReadingAdapter("session", answer)},
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
    )
    monkeypatch.setattr(daemon, "_require_boundary", lambda: None)
    passes = []
    original = StewardDaemon._pass

    def counting(self, *args, **kwargs):
        step = original(self, *args, **kwargs)
        def counted():
            step()
            passes.append(1)  # counted once finished, so a message queued after cannot ride it
        return counted

    monkeypatch.setattr(StewardDaemon, "_pass", counting)
    failures = []

    def run():
        try:
            daemon.run_forever()
        except BaseException as error:
            failures.append(error)

    owner = threading.Thread(target=run)
    owner.start()
    try:
        deadline = time.monotonic() + 5
        while not passes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert passes, "the daemon never made its first pass"
        pending = inbox / "call.tmp"
        pending.write_text(json.dumps({
            "id": "call", "kind": "message", "text": "Are you there?", "topic": 1, "profile": "fast",
            "context": "Live phone call: answer in one short spoken sentence.",
        }))
        pending.replace(inbox / "call.json")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "Hello from the desk." not in (
            events.read_text() if events.exists() else ""
        ):
            time.sleep(0.02)
        assert "Hello from the desk." in events.read_text()
        assert len(passes) == 1, "the answer should not have needed a second pass"
        state = StateDatabase(config.provider.state_db)
        thread = state.get_or_create_conversation("desk", "1", provider="codex", profile="balanced")
        assert thread.profile == "fast"
        assert "Live phone call: answer in one short spoken sentence.\n\nAre you there?" in prompts[0]
        assert not (inbox / "call.json").exists()
    finally:
        daemon._stop.set()
        owner.join(10)
    assert not owner.is_alive()
    assert failures == []
