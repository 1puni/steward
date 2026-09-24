"""Trusted, stdlib-only entry point executed by systemd before dropping UID."""
from __future__ import annotations

import json
import os
import select
from pathlib import Path
import sys


def process_identity(pid: int) -> str:
    # comm may contain spaces and parentheses; starttime is field 22.
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        raise ValueError('controller has exited')
    return fields[19]


def launch(payload: Path) -> None:
    invocation = json.loads(payload.read_text())
    payload.unlink()
    payload.parent.rmdir()
    if process_identity(invocation['controller_pid']) != invocation['controller_start']:
        raise ValueError('invocation belongs to an expired controller generation')
    controller_fd = os.pidfd_open(invocation['controller_pid'])
    if (process_identity(invocation['controller_pid']) != invocation['controller_start']
            or select.select([controller_fd], [], [], 0)[0]):
        raise ValueError('invocation belongs to an expired controller generation')
    child = os.fork()
    if child == 0:
        os.close(controller_fd)
        os.setgroups([])
        os.setgid(invocation['gid'])
        os.setuid(invocation['uid'])
        os.umask(0o077)
        os.chdir(invocation['cwd'])
        os.execvpe(invocation['argv'][0], invocation['argv'], invocation['env'])
    # The root guardian owns lifetime only. The native child owns the pipes;
    # keeping another copy open here would prevent EOF during native shutdown.
    for fd in (0, 1, 2):
        os.close(fd)
    child_fd = os.pidfd_open(child)
    ready, _, _ = select.select([controller_fd, child_fd], [], [])
    if controller_fd in ready:
        # Exiting the main service process invokes systemd's whole-cgroup
        # cleanup, even when descendants have changed sessions/process groups.
        os._exit(1)
    _, status = os.waitpid(child, 0)
    code = os.waitstatus_to_exitcode(status)
    os._exit(code if code >= 0 else 128 - code)


if __name__ == '__main__':
    launch(Path(sys.argv[1]))
