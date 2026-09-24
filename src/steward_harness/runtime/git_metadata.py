"""Finite, non-spawning Git path queries without a transient service per read.

The timeout supervisor owns the process group even if the controller dies. Only audited
built-in path queries qualify: there is no helper, hook, filter or transport to
escape that group. All other Git stays in the systemd ownership lane.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import time
from typing import Any, Mapping

from steward_harness.git import hardened_git_argv, is_git_path_query


def _protected_executable(executable: str) -> None:
    inspected: set[Path] = set()

    def inspect(path: Path) -> None:
        if path in inspected:
            return
        if len(inspected) >= 128:
            raise ValueError('direct Git executable has excessive indirection')
        inspected.add(path)
        if path.parent != path:
            inspect(path.parent)
        metadata = path.lstat()
        if metadata.st_uid != 0:
            raise ValueError(f'direct Git executable is not protected: {path}')
        if stat.S_ISLNK(metadata.st_mode):
            target = Path(os.readlink(path))
            inspect(target if target.is_absolute() else path.parent / target)
        elif metadata.st_mode & 0o022:
            raise ValueError(f'direct Git executable is not protected: {path}')

    inspect(Path(executable))
    mode = Path(executable).resolve(strict=True).stat().st_mode
    if not stat.S_ISREG(mode) or mode & 0o6000:
        raise ValueError(f'direct Git executable changes privilege: {executable}')


def run_metadata(args: tuple[str, ...], *, cwd: str | Path, timeout: float,
                 home: Path, identity: Mapping[str, Any]) -> subprocess.CompletedProcess[str]:
    if not Path(cwd).is_absolute():
        raise ValueError('direct Git requires an absolute working directory')
    if not is_git_path_query(args) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('not a finite controller Git path query')
    # Fixed binaries and a fixed environment; neither repository config nor an
    # inherited PATH/LD_PRELOAD can select the supervisor or Git executable.
    executables = ['/usr/bin/timeout', '/usr/bin/git']
    if identity:
        if (set(identity) != {'user', 'group', 'extra_groups', 'umask'}
                or os.geteuid() != 0 or type(identity['user']) is not int
                or type(identity['group']) is not int
                or not 0 < identity['user'] < 2**32 - 1
                or not 0 < identity['group'] < 2**32 - 1
                or identity['extra_groups'] != () or identity['umask'] != 0o077):
            raise ValueError('direct Git requires an explicit non-root identity')
        executables.extend(('/usr/bin/setpriv', '/usr/bin/env'))
    elif os.geteuid() == 0:
        raise ValueError('root direct Git requires an explicit non-root identity')
    for executable in executables:
        _protected_executable(executable)
    environment = {
        'PATH': '/usr/bin:/bin', 'HOME': str(home), 'LANG': 'C',
        'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1',
    }
    command = ['/usr/bin/git', *hardened_git_argv(*args)[1:]]
    duration = max(.001, min(timeout, 60))
    if identity:
        # Keep the watchdog controller-owned: agent processes must not disable
        # the orphan deadline with SIGSTOP/SIGKILL. setpriv drops credentials
        # before env changes into the untrusted repository and execs Git.
        command = ['/usr/bin/setpriv', f"--reuid={identity['user']}",
                   f"--regid={identity['group']}", '--clear-groups', '--no-new-privs',
                   '--', '/usr/bin/env', f'--chdir={cwd}', *command]
    # KILL is intentional: an included config FIFO cannot defer the deadline.
    # Without --foreground, timeout signals its whole group (including itself).
    started = time.monotonic()
    process = subprocess.Popen(
        ['/usr/bin/timeout', '--signal=KILL', f'{duration:.3f}s', *command],
        cwd='/' if identity else cwd, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, umask=0o077,
    )
    try:
        stdout, stderr = process.communicate(timeout=duration + 5)
    except BaseException as original:
        # If communicate already reaped the supervisor (e.g. while handling
        # KeyboardInterrupt), its PID may be reusable. Never signal that ID.
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.communicate(timeout=5)
        except Exception as cleanup_error:
            original.add_note(f'Git cleanup failed: {cleanup_error}')
            if process.returncode is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception as reap_error:
                    original.add_note(f'Git supervisor reap failed: {reap_error}')
        finally:
            process.stdout.close()
            process.stderr.close()
        raise
    # Text mode would translate legal carriage returns in filesystem names.
    stdout, stderr = os.fsdecode(stdout), os.fsdecode(stderr)
    if process.returncode == -signal.SIGKILL and time.monotonic() - started < duration - .01:
        raise RuntimeError('direct Git supervisor was killed before its deadline')
    if process.returncode in (124, -signal.SIGKILL):
        raise subprocess.TimeoutExpired(command, duration, output=stdout, stderr=stderr)
    if process.returncode in (125, 126, 127):
        raise RuntimeError(f'direct Git supervisor failed (exit {process.returncode})')
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
