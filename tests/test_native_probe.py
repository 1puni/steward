"""Correlation checks for the live probe, without provider quota."""

import importlib.util
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "codex_probe",
    Path(__file__).parents[1] / "experiments/native_sessions/codex_probe.py",
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_probe_close_stops_its_quiet_child_through_the_request_predicate(tmp_path):
    connection = probe.Connection(
        {}, tmp_path, command=[sys.executable, "-c", "import time; time.sleep(60)"],
    )
    connection.close()
    assert not connection.thread.is_alive()
    assert isinstance(connection.failure, probe.RuntimeExecutionError)
    assert "cancelled" in str(connection.failure)


def terminal(thread, turn, status="completed"):
    return {
        "method": "turn/completed",
        "params": {"threadId": thread, "turn": {"id": turn, "status": status}},
    }


class Events:
    def __init__(self, buffered, pending):
        self.events = list(buffered)
        self.pending = iter(pending)

    def next(self):
        event = next(self.pending)
        self.events.append(event)
        return event


@pytest.mark.parametrize("buffered", [True, False])
def test_child_and_previous_turn_completion_cannot_complete_parent(buffered):
    unrelated = [terminal("child", "parent-turn"), terminal("parent", "old-turn")]
    parent = terminal("parent", "parent-turn")
    stream = Events(
        unrelated if buffered else [], [parent] if buffered else [*unrelated, parent]
    )
    assert (
        probe.completed(stream, 0, "parent", "parent-turn") == parent["params"]["turn"]
    )
    assert stream.events[-1] is parent


def test_failed_child_is_not_parent_failure_but_failed_parent_is():
    stream = Events(
        [terminal("child", "child-turn", "failed")],
        [terminal("parent", "parent-turn", "failed")],
    )
    with pytest.raises(RuntimeError, match="turn failed"):
        probe.completed(stream, 0, "parent", "parent-turn")
    assert len(stream.events) == 2
