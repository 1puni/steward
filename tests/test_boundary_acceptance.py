"""The startup gate itself, proven against a real second OS identity.

`UntrustedExecutionBroker.status()` is what stands between a model-controlled
child and the controller's own authority. It proves four things: that the
configured identity resolves non-root, that it differs from the controller's,
that it actually takes effect in a spawned child, and that the child cannot
reach the controller's private files. Four production callers depend on it —
`steward check`, `steward run`, and the deploy CLI — and until this file
nothing executed past its first two branches, because every other test either
hands those callers a fabricated `BoundaryStatus` or monkeypatches
`_require_boundary` away.

That is the evidence gap of `docs/half-migrated-state.md` §3.6, stated more
precisely than §3.6 states it. Read against the code, three of that section's
four claims have drifted: `tests/test_execution_boundary.py` does configure a
real user, five tests do run a real child program through the real
`ProcessController`, and pushes do go through real Git plumbing to real bare
repositories. What is true, and worse, is narrower: **the function that decides
whether privilege can be dropped has never run.**

It needs Linux and root, because a UID transition is the one thing a macOS dev
loop cannot simulate. `scripts/linux-boundary-acceptance.sh` supplies both.
Everywhere else this file skips, and a skipped run is not evidence — a green
suite on a laptop says nothing about this boundary and should not be read as
though it did.
"""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from steward_harness.config.schema import StewardConfig
from steward_harness.runtime.execution import (
    ExecutionBoundaryUnavailable,
    UntrustedExecutionBroker,
)


pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0,
    reason="requires Linux root: the point is a real UID transition",
)

# The controller's own tree must sit somewhere the untrusted identity cannot
# replace. That rules out `/tmp`: `_unstable_namespace` walks every ancestor of
# a controller-private path and a sticky world-writable ancestor fails it —
# correctly, since anyone could rename a component out from under the check.
_CONTROLLER_ROOT = Path("/opt")

# Whichever exists. The script creates `steward`; a bare distribution image
# still has `nobody`, and `daemon` is the account the deployment acceptance
# already relies on.
_AGENT_ACCOUNTS = ("steward", "nobody", "daemon")


def _agent_account() -> pwd.struct_passwd:
    for name in _AGENT_ACCOUNTS:
        try:
            account = pwd.getpwnam(name)
        except KeyError:
            continue
        if account.pw_uid != 0:
            return account
    pytest.skip(f"no unprivileged account among {_AGENT_ACCOUNTS}")


@contextmanager
def _steward_tree(
    *, state_directory_mode: int, database_mode: int
) -> Iterator[tuple[pwd.struct_passwd, UntrustedExecutionBroker, Path, Path]]:
    """A real split-ownership steward tree, built with real ownership.

    Controller authority and model-writable roots are separate subtrees, which
    is the split `config/schema.py` already enforces at validation time; here it
    is enforced a second time by the filesystem, which is the only place it is
    ever actually true.
    """
    account = _agent_account()
    base = Path(tempfile.mkdtemp(prefix="steward-boundary-", dir=_CONTROLLER_ROOT))
    try:
        os.chmod(base, 0o755)
        controller = base / "controller"
        controller.mkdir(mode=0o700)
        state_db = controller / "state.db"
        state_db.write_text("synthetic controller-only state")
        os.chmod(state_db, database_mode)

        agent_root = base / "agent"
        agent_root.mkdir(mode=0o755)
        agent: dict[str, Path] = {}
        for name in ("home", "work", "tmp", "repo"):
            path = agent_root / name
            path.mkdir(mode=0o700)
            os.chown(path, account.pw_uid, account.pw_gid)
            agent[name] = path

        payload = {
            "identity": {"name": "boundary-acceptance", "slug": "boundary"},
            "execution": {
                "user": account.pw_name,
                "group": grp.getgrgid(account.pw_gid).gr_name,
                "home": str(agent["home"]),
                "tmpdir": str(agent["tmp"]),
            },
            "provider": {
                "workdir": str(agent["work"]),
                "state_db": str(state_db),
            },
            "repositories": {
                "app": {
                    "path": str(agent["repo"]),
                    "remote_url": "https://example.invalid/app.git",
                }
            },
        }
        config_path = controller / "steward.yaml"
        config_path.write_text(yaml.safe_dump(payload))
        config = StewardConfig.model_validate(payload)
        # Set last: everything above had to be created through this directory.
        os.chmod(controller, state_directory_mode)

        broker = UntrustedExecutionBroker.for_steward(config, config_path)
        assert broker.enabled
        yield account, broker, state_db, agent["work"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_the_boundary_is_enforced_against_a_real_second_identity() -> None:
    """A correct tree: `status()` returns enforced, and the drop is real.

    The second half matters as much as the first. `status()` reporting success
    is the harness's own opinion; a child reporting the agent's uid and failing
    to open the state database is the kernel's.
    """
    with _steward_tree(state_directory_mode=0o700, database_mode=0o600) as (
        account,
        broker,
        state_db,
        workdir,
    ):
        status = broker.status()
        assert status.enforced, status.reason
        assert (status.uid, status.gid) == (account.pw_uid, account.pw_gid)
        assert status.reason == (
            f"commands run as {account.pw_name} "
            f"({account.pw_uid}:{account.pw_gid})"
        )

        observed = broker.run(
            [sys.executable, "-c", "import os; print(os.getuid())"],
            cwd=workdir,
            timeout=30,
        )
        assert observed.returncode == 0, observed.stderr
        assert observed.stdout.strip() == str(account.pw_uid)

        denied = broker.run(
            [sys.executable, "-c", f"open({str(state_db)!r}).read()"],
            cwd=workdir,
            timeout=30,
        )
        assert denied.returncode != 0
        assert "PermissionError" in denied.stderr


def test_a_provider_the_agent_cannot_execute_is_not_reported_available() -> None:
    """`os.access` asks for whoever is asking, and the controller is root.

    A `0700` root-owned CLI is executable by the controller and unusable by the
    agent, so `steward check` said a provider was ready that could not launch a
    single turn. That failure only exists where the controller is root and the
    identity is real — which is why it survived to be found by reading the code
    rather than by any test.
    """
    with _steward_tree(state_directory_mode=0o700, database_mode=0o600) as (
        _account,
        broker,
        _state_db,
        workdir,
    ):
        cli = workdir.parent / "provider-cli"
        cli.write_text("#!/bin/sh\nexit 0\n")

        os.chmod(cli, 0o700)
        os.chown(cli, 0, 0)
        assert os.access(cli, os.X_OK), "the controller can run it, which is the trap"
        assert not broker.can_execute(cli)

        os.chmod(cli, 0o755)
        assert broker.can_execute(cli)


def test_a_readable_state_database_refuses_the_boundary_and_stops_the_harness() -> None:
    """The check that has never run, failing for the reason it exists for.

    A traversable state directory and a world-readable database is the ordinary
    operator mistake — nothing crashes, nothing looks wrong, and the model can
    read every conversation the steward has ever had. `status()` names it
    exactly, and `_require_boundary` turns that refusal into a stopped harness
    rather than a warning, which is the property the four production callers
    are actually buying.
    """
    with _steward_tree(state_directory_mode=0o755, database_mode=0o644) as (
        _account,
        broker,
        state_db,
        workdir,
    ):
        status = broker.status()
        assert not status.enforced
        assert status.reason == (
            "untrusted identity can read controller-private state database: "
            f"{state_db}"
        )

        with pytest.raises(ExecutionBoundaryUnavailable) as refusal:
            broker.run([sys.executable, "-c", "pass"], cwd=workdir, timeout=30)
        assert str(state_db) in str(refusal.value)
