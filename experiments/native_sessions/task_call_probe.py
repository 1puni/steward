#!/usr/bin/env python3
"""Probe installed Codex MCP discovery/calls without inference or a provider login.

Disposable controller and native home; no running steward, publication or deployment.
The protocol fixture suite separately verifies terminal-event ordering for all families.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

from steward_harness.runtime.task_call_mcp import codex_arguments
from steward_harness.state import ConversationId, StateDatabase
from steward_harness.task_calls import TaskCalls, TaskCallServer


def main():
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("native MCP probe timed out")))
    signal.alarm(45)
    with tempfile.TemporaryDirectory() as root:
        path = Path(root)
        (path / "home").mkdir()
        state = StateDatabase(path / "state.db")
        state.tasks.repositories = {"app"}
        owner = state.open_conversation(ConversationId("telegram:probe"), provider="codex", profile="balanced")
        turn, _ = state.start_turn(owner.conversation_id, "probe", "operator", "Verify MCP")
        server = TaskCallServer(TaskCalls(state, turn.turn_id))
        process = subprocess.Popen(
            ["codex", "app-server", "--stdio", "-c", "features.apps=false", *codex_arguments(server.path)],
            env=dict(os.environ, CODEX_HOME=str(path / "home")), start_new_session=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        serial = 0

        def rpc(method, params):
            nonlocal serial
            serial += 1
            process.stdin.write(json.dumps(dict(id=serial, method=method, params=params)) + "\n")
            process.stdin.flush()
            for line in process.stdout:
                item = json.loads(line)
                if item.get("id") == serial:
                    if "error" in item:
                        raise RuntimeError(item["error"])
                    return item["result"]
            raise RuntimeError("native process ended without its response")

        try:
            server.bind(process.pid, None)
            rpc("initialize", {"clientInfo": {"name": "steward-task-probe", "version": "1"},
                               "capabilities": {"experimentalApi": True}})
            thread = rpc("thread/start", {"cwd": root, "approvalPolicy": "never", "sandbox": "read-only"})["thread"]["id"]
            inventory = rpc("mcpServerStatus/list", {"threadId": thread})
            assert any(item["name"] == "steward_tasks" and item["tools"] for item in inventory["data"])

            def call(arguments):
                result = rpc("mcpServer/tool/call", {"threadId": thread, "server": "steward_tasks",
                                                   "tool": "task", "arguments": arguments})
                assert not result.get("isError"), result
                return json.loads(result["content"][0]["text"])

            requests = [dict(operation="submit", key=key, repository="app", title=key, brief="Probe only")
                        for key in ("first", "second")]
            receipts = [call(request) for request in requests]
            assert all(receipt["accepted"] for receipt in receipts)
            assert receipts[0]["task_id"] != receipts[1]["task_id"]
            assert call(requests[0])["replayed"]
            assert len(call(dict(operation="list"))["tasks"]) == 2
            assert call(dict(operation="cancel", key="stop", task_id=receipts[0]["task_id"], text="Probe done"))["accepted"]
            assert state.get_turn(turn.turn_id).state == "running"
            print(json.dumps(dict(native=subprocess.check_output(["codex", "--version"], text=True).strip(),
                                  receipts=receipts, parent_state="running", inference=False)))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                server.close()
                signal.alarm(0)


if __name__ == "__main__":
    main()
