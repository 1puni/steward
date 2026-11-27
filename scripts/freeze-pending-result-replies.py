#!/usr/bin/env python3
"""Freeze pending result messages in a quiescent state directory before upgrade."""

import argparse
import json
from pathlib import Path

from steward_harness.receipts import write_receipt
from steward_harness.state import TaskId
from steward_harness.task_store import GitTaskStore


def freeze(database: Path) -> int:
    if not database.is_file():
        raise FileNotFoundError(database)
    store = GitTaskStore(database.with_name(database.name + ".tasks.git"), create=False)
    count = 0
    for path in database.with_name(database.name + ".task-results").glob("*.json"):
        receipt = json.loads(path.read_text())
        if receipt.get("done") or "reply" in receipt:
            continue
        quiet = (receipt.get("task_id") and receipt["source_key"].endswith(":done")
                 and store.get(TaskId(receipt["task_id"])).quiet)
        receipt["reply"] = "" if quiet else receipt["result_text"]
        write_receipt(path, receipt)
        count += 1
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    print(f"Frozen replies: {freeze(parser.parse_args().database)}")
