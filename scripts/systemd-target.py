#!/usr/bin/env python3
"""Run the systemd target driver and its worker from one immutable release."""
from pathlib import Path
import sys

entry = Path(__file__).resolve()
release = entry.parent.parent
sys.path[:0] = [str(release / "src"), str(release / "vendor")]

from steward_harness.deploy.cli import main

if __name__ == "__main__":
    raise SystemExit(main(worker_entry=str(entry)))
