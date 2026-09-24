"""The untrusted broker strips authority and drops the configured OS identity."""

from __future__ import annotations

import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

from steward_harness.config.schema import (
    CommandSpec,
    StewardConfig,
    UntrustedExecutionConfig,
)
from steward_harness.git import agent_git, run_agent_git
from steward_harness.landing.gates import GateRunner
from steward_harness.landing.worktree import WorktreeError
from steward_harness.runtime.execution import UntrustedExecutionBroker


def _current_user_broker(tmp_path: Path) -> UntrustedExecutionBroker:
    """A broker whose untrusted identity is whoever is running the suite.

    Root cannot be that identity: the configuration refuses `user: root`,
    which is the invariant these tests exist to protect, so asking root to
    drop to root is not a case the boundary is allowed to have. Skipping is
    honest about the consequence — a suite run as root does not exercise the
    boundary at all. The harness's own gate runs as the untrusted user, which
    is where these do run.
    """
    if os.getuid() == 0:
        pytest.skip("the untrusted identity cannot be root; run this suite unprivileged")
    user = pwd.getpwuid(os.getuid()).pw_name
    return UntrustedExecutionBroker(
        UntrustedExecutionConfig(user=user, home=str(tmp_path))
    )


def test_command_child_cannot_receive_provider_or_controller_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _current_user_broker(tmp_path)
    observed_environment: dict[str, str] = {}

    def run(*_args, **kwargs):
        observed_environment.update(kwargs["env"])
        return subprocess.CompletedProcess([], 0, "", "")

    class LaunchedCommand:
        returncode = 0

        def communicate(self, input_text, *, timeout):
            return "", ""

    def launch(*_args, **kwargs):
        observed_environment.update(kwargs["env"])
        return LaunchedCommand()

    # Configured Linux identities use the owned launch path. This unit test
    # checks filtering before launch; real UID isolation has its own acceptance.
    monkeypatch.setattr(broker, "_launch", launch)
    monkeypatch.setattr(subprocess, "run", run)
    result = broker.run(
        ["candidate-command"],
        cwd=tmp_path,
        timeout=10,
        env={
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HTTPS_PROXY": "http://proxy.invalid",
            "GITHUB_TOKEN": "landing-sentinel",
            "TELEGRAM_BOT_TOKEN": "telegram-sentinel",
            "SSH_AUTH_SOCK": "/run/controller-agent.sock",
            "OPENAI_API_KEY": "provider-credential",
        },
        extra_env={"STEWARD_TELEGRAM_CHAT_ID": "7"},
    )

    assert result.returncode == 0
    assert {"PATH", "LANG", "HTTPS_PROXY", "STEWARD_TELEGRAM_CHAT_ID"} <= set(
        observed_environment
    )
    assert {
        "GITHUB_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "SSH_AUTH_SOCK",
        "OPENAI_API_KEY",
    }.isdisjoint(observed_environment)


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "ZAI_AUTH_TOKEN"])
def test_command_metadata_cannot_smuggle_provider_credentials(
    tmp_path: Path, name: str
) -> None:
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())

    with pytest.raises(ValueError, match=name):
        broker.run(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            timeout=10,
            extra_env={name: "smuggled"},
        )


def test_local_provider_receives_actual_account_names(tmp_path: Path) -> None:
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    process = broker.popen(
        [sys.executable, "-c", "import os; print(os.environ['USER']); print(os.environ['LOGNAME'])"],
        cwd=tmp_path,
        env={"USER": "wrong-account", "LOGNAME": "wrong-account"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate(timeout=10)
    expected = pwd.getpwuid(os.geteuid()).pw_name
    assert process.returncode == 0, stderr
    assert stdout.decode().splitlines() == [expected, expected]


def test_command_child_is_credentialless_without_identity_drop(tmp_path: Path) -> None:
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    result = broker.run(
        [
            sys.executable,
            "-c",
            "import os; print(os.getenv('LANG')); print(os.getenv('OPENAI_API_KEY')); "
            "print(os.getenv('GITHUB_TOKEN')); print(os.getenv('ZAI_AUTH_TOKEN'))",
        ],
        cwd=tmp_path,
        timeout=10,
        env={
            "LANG": "C.UTF-8",
            "OPENAI_API_KEY": "provider-credential",
            "GITHUB_TOKEN": "controller-credential",
            "ZAI_AUTH_TOKEN": "glm-credential",
        },
    )

    assert result.stdout.splitlines() == ["C.UTF-8", "None", "None", "None"]


def test_provider_child_receives_explicit_provider_environment(tmp_path: Path) -> None:
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    process = broker.popen(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['OPENAI_API_KEY']); "
            "print(os.getenv('GITHUB_TOKEN')); print(os.getenv('TELEGRAM_BOT_TOKEN'))",
        ],
        cwd=tmp_path,
        env={
            "OPENAI_API_KEY": "provider-credential",
            "GITHUB_TOKEN": "controller-credential",
            "TELEGRAM_BOT_TOKEN": "telegram-credential",
        },
        stdout=subprocess.PIPE,
    )

    stdout, _stderr = process.communicate(timeout=10)
    assert process.returncode == 0
    assert stdout.splitlines() == [b"provider-credential", b"None", b"None"]


def test_piped_local_command_receives_no_provider_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "provider-credential")
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    process = broker.popen_command(
        [sys.executable, "-c", "import os; print(os.getenv('OPENAI_API_KEY'))"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
    )

    stdout, _stderr = process.communicate(timeout=10)

    assert process.returncode == 0
    assert stdout == b"None\n"


def test_agent_git_uses_the_credentialless_command_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, "git version test\n", "")

    monkeypatch.setattr(broker, "run", run)

    result = run_agent_git(broker, "--version", cwd=tmp_path)

    assert result.stdout == "git version test\n"
    assert calls[0][0] == "git"
    assert "--no-replace-objects" in calls[0]


def test_agent_git_failure_reaches_the_operator_redacted_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agent_git's message becomes a blocked-task reason, so it is operator text."""
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    stderr = (
        "fatal: unable to access "
        "'https://x-access-token:ghp_realsecret@github.com/owner/repo.git/'\n"
        + "noise " * 2000
    )

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 128, "", stderr)

    monkeypatch.setattr(broker, "run", run)

    with pytest.raises(WorktreeError) as raised:
        agent_git(broker, "fetch", cwd=tmp_path, error=WorktreeError)

    message = str(raised.value)
    assert "ghp_realsecret" not in message
    assert "[URL redacted]" in message
    assert len(message) < 1200


def test_identity_kwargs_drop_supplementary_groups(tmp_path: Path) -> None:
    broker = _current_user_broker(tmp_path)
    kwargs = broker._identity_kwargs()

    assert kwargs["user"] == os.getuid()
    assert kwargs["extra_groups"] == ()
    assert kwargs["umask"] == 0o077


def test_steward_broker_collects_one_controller_filesystem_contract(
    tmp_path: Path,
) -> None:
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "t", "slug": "t"},
            "provider": {
                "state_db": str(tmp_path / "state" / "state.db"),
                "workdir": str(tmp_path / "work"),
            },
            "repositories": {
                "app": {
                    "path": str(tmp_path / "repo"),
                    "remote_url": "https://example.invalid/repository.git",
                }
            },
            "telegram": {
                "chat_id": 1,
                "allowed_users": [1],
                "token_path": str(tmp_path / "secrets" / "telegram-token"),
            },
        }
    )

    broker = UntrustedExecutionBroker.for_steward(
        config, tmp_path / "config" / "steward.yaml"
    )

    assert broker._private_files["state database"] == (
        tmp_path / "state" / "state.db"
    )
    assert broker._private_files["state database WAL"] == (
        tmp_path / "state" / "state.db-wal"
    )
    assert broker._private_files["state database shared memory"] == (
        tmp_path / "state" / "state.db-shm"
    )
    assert broker._private_files["Telegram token"] == (
        tmp_path / "secrets" / "telegram-token"
    )
    assert broker._protected_paths["state directory"] == tmp_path / "state"
    assert broker._protected_paths["configuration"] == (
        tmp_path / "config" / "steward.yaml"
    )
    assert broker._protected_paths["repository 'app' controller Git store"] == (
        tmp_path / "state" / "git" / "app.git"
    )
    assert broker._writable_paths == {
        "provider work directory": tmp_path / "work",
        "repository 'app'": tmp_path / "repo",
    }
    assert broker.writable_roots == (tmp_path / "work", tmp_path / "repo")
    assert not set(broker.writable_roots) & set(broker._protected_paths.values())


def test_explicit_world_lock_is_shared_without_sharing_its_namespace(
    tmp_path: Path,
) -> None:
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "t", "slug": "t"},
            "provider": {
                "state_db": str(tmp_path / "state" / "state.db"),
                "workdir": str(tmp_path / "work"),
            },
            "world": {
                "root": str(tmp_path / "world"),
                "lock_dir": str(tmp_path / "controller-lock"),
            },
        }
    )

    broker = UntrustedExecutionBroker.for_steward(config)

    assert broker._protected_paths["world lock directory"] == (
        tmp_path / "controller-lock"
    )
    assert "world lock" not in broker._protected_paths
    assert broker._shared_lock_files == {
        "world lock": tmp_path / "controller-lock" / "world.lock"
    }


def test_authority_bearing_steward_broker_fails_before_child_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "t", "slug": "t"},
            "provider": {
                "state_db": str(tmp_path / "state.db"),
                "workdir": str(tmp_path),
            },
            "repositories": {
                "app": {
                    "path": str(tmp_path / "repo"),
                    "remote_url": "https://example.invalid/repository.git",
                }
            },
        }
    )
    broker = UntrustedExecutionBroker.for_steward(config)

    def unexpected_launch(*_args, **_kwargs):
        pytest.fail("child process launched without an execution identity")

    monkeypatch.setattr(subprocess, "run", unexpected_launch)

    with pytest.raises(
        RuntimeError, match="no untrusted execution user is configured"
    ):
        broker.run(["true"], cwd=tmp_path, timeout=1)


def test_controller_path_check_rejects_readable_private_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / "state.db"
    private.write_text("secret")
    broker = UntrustedExecutionBroker(
        UntrustedExecutionConfig(), private_files={"state": private}
    )
    monkeypatch.setattr(broker, "_unstable_namespace", lambda *_args, **_kwargs: None)

    failure = broker._path_failure(
        os.getuid() + 100_000,
        os.getgid(),
        probe_environment={"PATH": "/usr/bin:/bin"},
        identity_kwargs={},
    )

    assert failure == f"untrusted identity can read controller-private state: {private}"


def test_controller_path_check_rejects_a_namespace_the_untrusted_identity_controls(
    tmp_path: Path,
) -> None:
    """A directory on the way to controller state that the identity controls.

    Which of the two namespace rejections fires depends on the runner, not on
    the harness: `/tmp` is above every `tmp_path`, and root *owns* it while an
    unprivileged user merely *can write* it. Both are the same guarantee — the
    untrusted identity can replace something on the path to controller state —
    so the assertion is on that, not on which sentence describes it.
    """
    private = tmp_path / "state.db"
    broker = UntrustedExecutionBroker(
        UntrustedExecutionConfig(), private_files={"state": private}
    )

    failure = broker._path_failure(
        os.getuid(),
        os.getgid(),
        probe_environment={"PATH": "/usr/bin:/bin"},
        identity_kwargs={},
    )

    assert failure is not None
    assert failure.startswith("untrusted identity ")
    assert "controller-owned state" in failure


def test_controller_path_check_allows_only_a_stable_writable_shared_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_directory = tmp_path / "locks"
    lock_directory.mkdir(mode=0o750)
    lock_directory.chmod(0o750)  # The fixture requires this mode even under umask 0077.
    lock = lock_directory / "world.lock"
    lock.write_bytes(b"")
    lock.chmod(0o660)
    broker = UntrustedExecutionBroker(
        UntrustedExecutionConfig(), shared_lock_files={"world lock": lock}
    )
    monkeypatch.setattr(broker, "_unstable_namespace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(broker, "_identity_can", lambda mode, *_args: mode == "-w")

    assert (
        broker._path_failure(
            os.getuid() + 100_000,
            os.getgid(),
            probe_environment={"PATH": "/usr/bin:/bin"},
            identity_kwargs={},
        )
        is None
    )

    lock_directory.chmod(0o770)
    assert broker._path_failure(
        os.getuid() + 100_000,
        os.getgid(),
        probe_environment={"PATH": "/usr/bin:/bin"},
        identity_kwargs={},
    ) == f"controller-shared world lock directory must have mode 0750: {lock_directory}"
    lock_directory.chmod(0o750)

    alias = tmp_path / "lock-alias"
    alias.hardlink_to(lock)
    assert broker._path_failure(
        os.getuid() + 100_000,
        os.getgid(),
        probe_environment={"PATH": "/usr/bin:/bin"},
        identity_kwargs={},
    ) == f"controller-shared world lock must have one link: {lock}"
    alias.unlink()

    monkeypatch.setattr(broker, "_identity_can", lambda *_args: False)
    assert broker._path_failure(
        os.getuid() + 100_000,
        os.getgid(),
        probe_environment={"PATH": "/usr/bin:/bin"},
        identity_kwargs={},
    ) == f"untrusted identity cannot write controller-shared world lock: {lock}"

    lock.unlink()
    lock.symlink_to(tmp_path / "target")
    assert broker._path_failure(
        os.getuid() + 100_000,
        os.getgid(),
        probe_environment={"PATH": "/usr/bin:/bin"},
        identity_kwargs={},
    ) == f"controller-shared world lock must not be a symlink: {lock}"


def test_read_access_is_bounded_and_never_grants_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _current_user_broker(tmp_path)
    broker._readable_roots = {"media": tmp_path / "media"}
    root = tmp_path / "media"
    root.mkdir()
    image = root / "image"
    image.write_bytes(b"image")
    ownership: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(
        os,
        "chown",
        lambda path, uid, gid: ownership.append((Path(path), uid, gid)),
    )
    monkeypatch.setattr(
        broker,
        "_identity_can",
        lambda mode, *_args: mode in {"-r", "-x"},
    )

    broker.grant_read_access(root)
    broker.grant_read_access(image)

    assert ownership[0][0] == root
    assert ownership[1][0] == image
    assert root.stat().st_mode & 0o777 == 0o750
    assert image.stat().st_mode & 0o777 == 0o640
    with pytest.raises(ValueError, match="outside configured roots"):
        broker.grant_read_access(tmp_path / "other")


def test_repository_gate_uses_credentialless_broker(tmp_path: Path, monkeypatch) -> None:
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GH_TOKEN", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.setenv(key, "controller-secret")
    monkeypatch.setenv("GATE_FIXTURE", "allowed-command-environment")
    config = UntrustedExecutionConfig(inherited_environment=("PATH", "GATE_FIXTURE"))
    result = GateRunner.run_command(
        CommandSpec(argv=(sys.executable, "-c", "import os; print(dict(os.environ))")),
        tmp_path,
        broker=UntrustedExecutionBroker(config),
    )
    assert result.passed
    assert "controller-secret" not in result.stdout
    assert "allowed-command-environment" in result.stdout


@pytest.mark.skipif(os.geteuid() != 0, reason="real UID transition requires root")
def test_real_child_process_runs_as_non_root_identity(tmp_path: Path) -> None:
    candidates = [account for account in pwd.getpwall() if account.pw_uid not in {0}]
    if not candidates:
        pytest.skip("host has no non-root account")
    account = candidates[0]
    os.chown(tmp_path, account.pw_uid, account.pw_gid)
    broker = UntrustedExecutionBroker(
        UntrustedExecutionConfig(user=account.pw_name, home=str(tmp_path))
    )

    result = broker.run(
        [sys.executable, "-c", "import os; print(os.geteuid()); print(os.getgroups())"],
        cwd=tmp_path,
        timeout=10,
    )

    lines = result.stdout.splitlines()
    assert result.returncode == 0
    assert lines[0] == str(account.pw_uid)
    assert lines[1] == "[]"


def test_git_transport_services_run_as_the_agent_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fetch --upload-pack` and `push --receive-pack` cross as the agent identity."""
    import shlex

    broker = _current_user_broker(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "controller-secret")
    command = shlex.split(broker.git_service("upload-pack").removeprefix("umask 077; "))
    assert command[:2] == ["env", "-i"]
    setpriv = command.index("setpriv")
    environment = dict(item.split("=", 1) for item in command[2:setpriv])
    assert "ANTHROPIC_API_KEY" not in environment
    assert environment["HOME"] == str(tmp_path)
    assert command[setpriv + 1:setpriv + 5] == [
        f"--reuid={os.getuid()}", f"--regid={os.getgid()}", "--clear-groups", "--"]
    assert command[setpriv + 5] == "git" and command[-1] == "upload-pack"
    assert "core.hooksPath=/dev/null" in command
