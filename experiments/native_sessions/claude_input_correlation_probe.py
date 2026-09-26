"""Observe native input/result correlation without treating echoes as completion.

Run through glm_probe.py --login-shell --correlation-only. The fixture
records identifiers and event shapes, never authentication or transcript text.
"""
from __future__ import annotations

from pathlib import Path

from glm_probe import send


def correlation_probe(connection, world: Path) -> None:
    original = send(
        connection,
        f"Bounded transport fixture: run a Bash command to sleep 5 seconds, then write ORIGINAL "
        f"to {world}/correlation.txt. No other files or network. Reply with that marker.",
    )
    offered = {original: "original"}
    echoes = set()
    results = 0
    completed = set()
    late = None
    while True:
        event = connection.next()
        kind = event.get("type")
        if kind == "command_lifecycle":
            source = event.get("command_uuid")
            print("command lifecycle", offered.get(source, "native-internal"), event.get("state"), flush=True)
            if event.get("state") == "completed":
                completed.add(source)
            if completed.issuperset(offered) and late is not None:
                assert (world / "correlation.txt").read_text().strip() == "AFTER_RESULT"
                print("all offered commands reached native completion after active-fold and terminal-race inputs", flush=True)
                return
        if kind == "user" and event.get("uuid") in offered:
            echoes.add(event["uuid"])
            print("input echo", offered[event["uuid"]], "keys", sorted(event), flush=True)
        if kind == "assistant" and len(offered) == 1:
            if any(block.get("type") == "tool_use" and block.get("name") == "Bash"
                   for block in event.get("message", {}).get("content", [])):
                correction = send(
                    connection,
                    f"Correction: final contents of {world}/correlation.txt must be CORRECTED. "
                    "Apply the correction before finishing. No other changes.",
                )
                offered[correction] = "correction"
                print("offered correction during native Bash", flush=True)
        if kind == "result":
            results += 1
            print("result evidence", {
                "keys": sorted(event), "result_number": results,
                "input_reference": offered.get(event.get("user_message_uuid")),
                "terminal_reason": event.get("terminal_reason"),
                "origin": event.get("origin"),
                "echoed": [offered[source] for source in echoes],
            }, flush=True)
            if event.get("subtype") != "success" or event.get("is_error"):
                raise RuntimeError("native correlation fixture failed")
            marker = world / "correlation.txt"
            if marker.exists() and marker.read_text().strip() == "CORRECTED":
                print("correction applied in initial result", flush=True)
                if late is None:
                    late = send(connection, f"Now set {world}/correlation.txt to AFTER_RESULT. Reply with AFTER_RESULT.")
                    offered[late] = "after-result"
                    print("offered input racing initial completion", flush=True)
            if results >= 3:
                raise AssertionError("correction never applied")
