#!/usr/bin/env python3
"""Exec the configured release as the real systemd service MainPID.

Only the provider and remote APIs are scripted fixtures. systemd owns service
restart, invocation containment and detached deployment workers.
"""
import os
from pathlib import Path
import sys

current = Path('/opt/steward-current')
root = current.resolve() if current.exists() else Path('/opt/steward-bootstrap')
os.chdir(root)
os.environ.update(PYTHONPATH=str(root / 'src'), PYTHONDONTWRITEBYTECODE='1')
Path('/run/acceptance-steward.pid').write_text(str(os.getpid()))
os.execv(sys.executable, [sys.executable, '-m', 'steward_harness.cli', 'run',
                         '--config', '/etc/steward/steward.yaml'])
