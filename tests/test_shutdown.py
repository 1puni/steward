"""The daemon lease outlives every writer during orderly shutdown.

The invariant is unchanged: a replacement daemon must not acquire the lease
while a writer from this one is still writing. What changed is how much code
holds it. There is one pass and it *is* a writer, so it has returned before
`stop()` runs; only the two things genuinely concurrent with it — the shared
`Dispatch` and Telegram's executor — have to be waited for.

Gone with the supervisor: six parameterised lanes, each proving that a fault
in one loop reached a registry of peers and requested a stop on all of them.
There is no registry and no request. A dispatched worker's exception is
re-raised by `Dispatch.reap()` in the pass, which is the same exception
leaving `run_forever`; a fault in one of Telegram's own threads exits the
process, which is not expressible in-process and is asserted at the hook.
"""

import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

from state_fixtures import admit_task, advance
from steward_harness.config.schema import StewardConfig, TelegramConfig, UntrustedExecutionConfig
from steward_harness import daemon as daemon_module
from steward_harness.daemon import StewardDaemon
from steward_harness.kernel import Dispatch, StewardKernel
from steward_harness.state import StateDatabase, TaskSpec
from steward_harness.telegram.api import TelegramAPI
from steward_harness.telegram.service import TelegramService
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.lease import Busy
from test_daemon import _config


def test_shutdown_discards_queued_work_but_waits_for_active_writer():
    dispatch = Dispatch(1)
    started, release, queued_ran = threading.Event(), threading.Event(), threading.Event()
    def write():
        started.set()
        assert release.wait(5)
    dispatch.submit("active", write)
    assert started.wait(2)
    dispatch.submit("queued", queued_ran.set)
    pending = dispatch._inflight["queued"]
    stopped = threading.Thread(target=dispatch.stop)
    stopped.start()
    try:
        # Cancellation is observable on the future while shutdown still drains.
        import time
        until = time.monotonic() + 2
        while not pending.cancelled() and time.monotonic() < until:
            time.sleep(0.01)
        assert pending.cancelled()
        assert stopped.is_alive()
    finally:
        release.set()
        stopped.join(3)
    assert not stopped.is_alive() and not queued_ran.is_set()


def test_a_dispatched_faults_exception_leaves_the_pass_and_the_daemon(tmp_path, monkeypatch):
    """No latch, no callback: reaping the slot re-raises where the pass runs."""
    daemon = StewardDaemon(_config(tmp_path), tmp_path / "steward.yaml", adapters={})
    state = StateDatabase(tmp_path / "fault.db")
    task = admit_task(
        state,
        TaskSpec("app", "Faulting", "Exercise fault propagation"),
        provider="codex",
        profile="balanced",
    ).task_id
    failure = ValueError("broken worker invariant")
    failed = threading.Event()

    def work(task_id, **kwargs):
        assert task_id == task
        failed.set()
        raise failure

    kernel = StewardKernel(
        state,
        SimpleNamespace(repositories={}),
        SimpleNamespace(prepare=work),
    )
    monkeypatch.setattr(daemon, "_start_owned", lambda: lambda: advance(kernel))

    with pytest.raises(ValueError) as caught:
        daemon.run_forever(poll_seconds=0.01)
    assert caught.value is failure
    assert failed.is_set()
    # The lease went with it; a replacement can take over immediately.
    with daemon._daemon_lease():
        pass


def test_a_thread_that_dies_outside_the_pass_exits_the_process(monkeypatch):
    """The chain that carried a lane's fault to its peers is one hook now."""
    exits = []
    monkeypatch.setattr(daemon_module.os, "_exit", exits.append)

    try:
        raise ValueError("broken Telegram polling invariant")
    except ValueError as error:
        daemon_module.exit_on_thread_fault(
            SimpleNamespace(
                exc_type=ValueError,
                exc_value=error,
                exc_traceback=error.__traceback__,
                thread=threading.current_thread(),
            )
        )

    assert exits == [1], "an unexpected thread death must exit non-zero"

    # A thread exiting through SystemExit asked to stop; that is not a fault.
    daemon_module.exit_on_thread_fault(
        SimpleNamespace(
            exc_type=SystemExit, exc_value=SystemExit(), exc_traceback=None,
            thread=threading.current_thread(),
        )
    )
    assert exits == [1]


def test_dispatch_holds_the_lease_until_its_worker_finishes(tmp_path, monkeypatch):
    """`stop()` waits for the executor, and the lease is released after it."""
    daemon = StewardDaemon(_config(tmp_path), tmp_path / "steward.yaml", adapters={})
    state = StateDatabase(tmp_path / "kernel.db")
    admit_task(
        state,
        TaskSpec("app", "Write", "Exercise writer ownership"),
        provider="codex",
        profile="balanced",
    )
    active, release, errors = threading.Event(), threading.Event(), []
    failure = ValueError("startup failed after dispatching a writer")
    marker = tmp_path / "accepted.txt"

    interrupted = threading.Event()

    def write(*args, **kwargs):
        active.set()
        assert release.wait(15), "test did not release the writer"
        marker.write_text("accepted before lease release")

    # Shutdown asks running task turns to end; asking is not their ending, so
    # the lease still waits for a writer that has not yet returned.
    kernel = StewardKernel(
        state,
        SimpleNamespace(repositories={}),
        SimpleNamespace(prepare=write, interrupt_running=interrupted.set),
    )

    def start_owned():
        advance(kernel)
        daemon._kernel = kernel
        assert active.wait(2)
        raise failure

    monkeypatch.setattr(daemon, "_start_owned", start_owned)

    def run():
        try:
            daemon.run_forever(poll_seconds=0.01)
        except BaseException as error:
            errors.append(error)

    owner = threading.Thread(target=run)
    owner.start()
    try:
        assert active.wait(2)
        # Exceed the old five-second join: expiry is not proof of termination.
        assert not owner.join(5.2) and owner.is_alive()
        assert interrupted.is_set() and not marker.exists()
        with pytest.raises(Busy):
            with daemon._daemon_lease():
                pytest.fail("replacement acquired ownership while a writer was alive")
    finally:
        release.set()
        owner.join(5)
    assert not owner.is_alive()
    assert errors == [failure]
    assert marker.read_text() == "accepted before lease release"
    with daemon._daemon_lease():
        pass


def test_a_faulting_worker_does_not_release_the_lease_over_a_live_peer(tmp_path, monkeypatch):
    """The one property the four-test rewrite dropped, restored.

    Found by the window-4 convergence audit: every other test here dispatches
    a single worker, so nothing exercised a fault arriving while a *peer* is
    still mid-write. The composition is what matters and it is ours, not the
    standard library's — `reap()` re-raises in the pass, `_run_owned`'s
    `finally` calls `stop()`, and only then does `shutdown(wait=True)` hold
    the lease over the survivor.
    """
    daemon = StewardDaemon(_config(tmp_path), tmp_path / "steward.yaml", adapters={})
    state = StateDatabase(tmp_path / "peer.db")
    slow, faulting = (
        admit_task(
            state,
            TaskSpec("app", name, "Exercise concurrent shutdown"),
            provider="codex",
            profile="balanced",
        ).task_id
        for name in ("slow", "fault")
    )
    active, release, failed = threading.Event(), threading.Event(), threading.Event()
    failure = ValueError("broken worker invariant")
    errors = []
    marker = tmp_path / "peer-accepted.txt"

    def work(task_id, **kwargs):
        if task_id == slow:
            active.set()
            assert release.wait(15), "test did not release the peer writer"
            marker.write_text("peer committed before ownership release")
            return
        assert active.wait(3), "the peer never started, so nothing was drained"
        failed.set()
        raise failure

    kernel = StewardKernel(
        state,
        SimpleNamespace(repositories={}),
        SimpleNamespace(prepare=work, interrupt_running=lambda: None),
        dispatch=Dispatch(2),
    )
    daemon._kernel = kernel
    monkeypatch.setattr(daemon, "_start_owned", lambda: lambda: advance(kernel))

    def run():
        try:
            daemon.run_forever(poll_seconds=0.01)
        except BaseException as error:
            errors.append(error)

    owner = threading.Thread(target=run)
    owner.start()
    try:
        assert failed.wait(3)
        # Teardown has begun: the pass reaped the fault, re-raised, and
        # `_run_owned`'s `finally` reached `stop()`. Asserting before this
        # point would prove nothing — the lease is held by a pass that has
        # not noticed yet, whether or not it would ever wait for the peer.
        assert daemon._stop.wait(3), "the fault never reached the daemon"
        assert not marker.exists()
        with pytest.raises(Busy):
            with daemon._daemon_lease():
                pytest.fail("the lease escaped an unfinished peer writer")
        assert owner.is_alive()
    finally:
        release.set()
        owner.join(5)
    assert not owner.is_alive()
    assert errors == [failure]
    assert marker.read_text() == "peer committed before ownership release"
    assert faulting is not slow
    with daemon._daemon_lease():
        pass


def test_telegram_drains_its_executor_before_the_lease_is_released(tmp_path, monkeypatch):
    """Telegram is the one writer still concurrent with the pass."""
    token = tmp_path / "token"
    token.write_text("test-token")
    config = StewardConfig.model_validate({
        **_config(tmp_path).model_dump(),
        "telegram": {"token_path": str(token), "chat_id": 1, "allowed_users": [2]},
    })
    daemon = StewardDaemon(
        config, tmp_path / "steward.yaml", adapters={},
        broker=UntrustedExecutionBroker(UntrustedExecutionConfig()),
    )
    monkeypatch.setattr(daemon, "_require_boundary", lambda: None)
    active, release, errors = threading.Event(), threading.Event(), []
    failure = ValueError("startup failed after starting the ingress")
    marker = tmp_path / "telegram-accepted.txt"

    def write(*args, **kwargs):
        active.set()
        assert release.wait(15), "test did not release the writer"
        marker.write_text("accepted before lease release")
        return "Accepted"

    def network(method, endpoint, **kwargs):
        if endpoint == "getMe":
            return {"id": 9}
        if endpoint == "getChat":
            return {"id": 1, "type": "private"}
        if endpoint == "getUpdates":
            release.wait(0.02)
            return []
        if endpoint == "sendMessage":
            return {"message_id": 1}
        assert endpoint in {"setMyCommands", "sendChatAction"}
        return True

    def start_owned():
        service = TelegramService(
            TelegramConfig(token_path=str(token), chat_id=1, allowed_users=(2,)),
            write,
            command_handler=lambda *args: "",
            state_db=StateDatabase(tmp_path / "telegram.db"),
            execution_broker=daemon.broker,
        )
        monkeypatch.setattr(service.api, "_request", network)
        # Queued in memory, as the poll loop would have; an executor thread
        # must still claim it once the service starts.
        service._ingest_update({
            "update_id": 1,
            "message": {"chat": {"id": 1}, "from": {"id": 2}, "text": "Work"},
        })
        daemon._telegram = service
        service.start()
        assert active.wait(3)
        raise failure

    monkeypatch.setattr(daemon, "_start_owned", start_owned)

    def run():
        try:
            daemon.run_forever(poll_seconds=0.01)
        except BaseException as error:
            errors.append(error)

    owner = threading.Thread(target=run)
    owner.start()
    try:
        assert active.wait(3)
        assert not owner.join(2) and owner.is_alive()
        assert not marker.exists()
        with pytest.raises(Busy):
            with daemon._daemon_lease():
                pytest.fail("replacement acquired ownership while a writer was alive")
    finally:
        release.set()
        owner.join(5)
    assert not owner.is_alive()
    assert errors == [failure]
    assert marker.read_text() == "accepted before lease release"
    with daemon._daemon_lease():
        pass
