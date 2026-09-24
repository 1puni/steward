"""CLI configuration checks."""

from __future__ import annotations

import os
import signal
from argparse import Namespace

import pytest

from steward_harness import cli
from steward_harness.config.schema import StewardConfig
from steward_harness.runtime.contracts import Availability
from steward_harness.runtime.execution import BoundaryStatus


@pytest.fixture(autouse=True)
def _restore_sigterm():
    """`run` installs a real handler; leaving it behind would outlive the test."""
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


class AvailabilityOnlyRuntime:
    def __init__(self, available: bool) -> None:
        self._available = available

    def available(self) -> Availability:
        return Availability(self._available, None if self._available else "offline")


def test_check_accepts_available_fallback_and_reports_local_prerequisites(
    monkeypatch, capsys
) -> None:
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "t", "slug": "t"},
            "repositories": {
                "r": {
                    "path": "/tmp/r",
                    "remote_url": "https://example.invalid/repository.git",
                }
            },
            "pipelines": {
                "p": {
                    "repository": "r",
                    "probe": {
                        "type": "command",
                        "command": {"argv": ["true"]},
                    },
                }
            },
        }
    )
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(
        cli.UntrustedExecutionBroker,
        "status",
        lambda _broker: BoundaryStatus(True, "test boundary"),
    )
    monkeypatch.setattr(
        cli,
        "build_runtimes",
        lambda _provider, _broker: {
            "codex": AvailabilityOnlyRuntime(False),
            "claude": AvailabilityOnlyRuntime(True),
            "glm": AvailabilityOnlyRuntime(True),
        },
    )

    assert cli._cmd_check(Namespace(config="unused")) == 0
    output = capsys.readouterr()
    assert "provider order: codex > claude > glm (first locally available: claude)" in output.out
    assert "pipeline p: probe=command repair=autonomous" in output.out
    assert output.err == ""


def test_check_rejects_deploy_capable_instance_without_identity_boundary(
    monkeypatch, capsys
) -> None:
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "t", "slug": "t"},
            "repositories": {
                "app": {
                    "path": "/srv/app",
                    "remote_url": "https://example.invalid/repository.git",
                }
            },
        }
    )
    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    assert cli._cmd_check(Namespace(config="unused")) == 1
    output = capsys.readouterr()
    assert "untrusted execution: unavailable" in output.out
    assert "requires an enforceable untrusted execution identity" in output.err


def test_check_reports_a_configured_provider_without_an_adapter(
    monkeypatch, capsys
) -> None:
    config = StewardConfig.model_validate(
        {
            "identity": {"name": "t", "slug": "t"},
            "provider": {
                "default_family": "future-provider",
                "fallback_families": [],
                "models": {"future-provider": {"balanced": "model"}},
            },
        }
    )
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(
        cli.UntrustedExecutionBroker,
        "status",
        lambda _broker: BoundaryStatus(True, "test boundary"),
    )
    monkeypatch.setattr(cli, "build_runtimes", lambda _provider, _broker: {})

    assert cli._cmd_check(Namespace(config="unused")) == 1
    output = capsys.readouterr()
    assert "provider future-provider local prerequisites: unavailable (no adapter registered)" in output.out
    assert "no provider in the configured order is locally available" in output.err


def test_run_constructs_the_state_native_daemon_with_the_validated_broker(
    monkeypatch,
) -> None:
    config = StewardConfig.model_validate(
        {"identity": {"name": "t", "slug": "t"}}
    )

    class Broker:
        def status(self):
            return BoundaryStatus(False, "not required")

    broker = Broker()
    calls: list[tuple[object, ...]] = []

    class BrokerFactory:
        @staticmethod
        def for_steward(_config, config_path):
            calls.append(("broker", _config, config_path))
            return broker

    class Daemon:
        def __init__(self, daemon_config, config_path, *, broker):
            calls.append(("daemon", daemon_config, config_path, broker))

        def run_forever(self):
            calls.append(("run",))

    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "UntrustedExecutionBroker", BrokerFactory)
    monkeypatch.setattr(cli, "StewardDaemon", Daemon)

    assert cli._cmd_run(Namespace(config="config.yaml")) == 0
    assert calls == [
        ("broker", config, "config.yaml"),
        ("daemon", config, "config.yaml", broker),
        ("run",),
    ]


def test_run_asks_the_daemon_to_drain_on_sigterm(monkeypatch) -> None:
    """SIGTERM is how a service manager stops this, and it used to just kill it."""
    config = StewardConfig.model_validate({"identity": {"name": "t", "slug": "t"}})
    stopped: list[str] = []

    class Broker:
        def status(self):
            return BoundaryStatus(False, "not required")

    class BrokerFactory:
        @staticmethod
        def for_steward(_config, _config_path):
            return Broker()

    class Daemon:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def request_stop(self) -> None:
            stopped.append("requested")

        def run_forever(self) -> None:
            os.kill(os.getpid(), signal.SIGTERM)
            # The handler runs between bytecodes, so the loop this stands for
            # observes the request rather than the process dying under it.
            assert stopped == ["requested"]

    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "UntrustedExecutionBroker", BrokerFactory)
    monkeypatch.setattr(cli, "StewardDaemon", Daemon)

    assert cli._cmd_run(Namespace(config="config.yaml")) == 0
    assert stopped == ["requested"]


@pytest.fixture
def task_add(tmp_path):
    import yaml

    config = tmp_path / "steward.yaml"
    config.write_text(yaml.safe_dump({
        "identity": {"name": "test", "slug": "test"},
        "provider": {"state_db": str(tmp_path / "state.db")},
        "repositories": {
            name: {"path": str(tmp_path / name),
                   "remote_url": f"https://example.invalid/{name}.git"}
            for name in ("app", "incident")
        },
    }))
    brief = tmp_path / "brief.md"
    brief.write_text("Fix the settings page.\n\nKeep café labels.\n", encoding="utf-8")
    return ["task", "add", "--config", str(config), "--repository", "app",
            "--title", "Settings", "--owner", "telegram:123", "--brief-file", str(brief)]


def test_task_add_persists_operator_request(task_add, tmp_path, capsys):
    from steward_harness.state import TaskId, TaskOriginKind
    from steward_harness.task_store import GitTaskStore

    assert cli.main([*task_add, "--priority", "12"]) == 0
    first = TaskId(capsys.readouterr().out.strip())
    store = GitTaskStore(tmp_path / "state.db.tasks.git", create=False)
    _, definition, body = store.read(first)
    assert (definition.repository, definition.title, definition.owner, definition.priority) == (
        "app", "Settings", "telegram:123", 12)
    assert definition.origin == TaskOriginKind.CONVERSATION
    assert definition.source.startswith("operator-cli:")
    assert definition.hold is None
    assert body == "Fix the settings page.\n\nKeep café labels."
    assert not (tmp_path / "state.db").exists()
    assert cli.main(task_add) == 0
    second = TaskId(capsys.readouterr().out.strip())
    assert first != second
    assert store.read(second)[1].priority == 0
    assert len(store.refs()) == 2


@pytest.mark.parametrize(("flag", "value", "message"), [
    ("--repository", "missing", "unknown repository"),
    ("--title", "  ", "title must be nonblank"),
    ("--title", "x" * 257, "title must be nonblank"),
    ("--owner", "task:abc", "reply owner"),
    ("--owner", "telegram: ", "reply owner"),
    ("--owner", "invalid", "invalid ConversationId"),
    ("--priority", "101", "priority"),
    ("--brief-file", "/nonexistent/request.md", "No such file"),
])
def test_task_add_rejects_invalid_input(task_add, tmp_path, capsys, flag, value, message):
    assert cli.main([*task_add, flag, value]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert message in output.err
    assert not (tmp_path / "state.db.tasks.git").exists()


@pytest.mark.parametrize("brief", [" \n", "x" * 8001], ids=["blank", "too-long"])
def test_task_add_rejects_invalid_brief(task_add, tmp_path, capsys, brief):
    (tmp_path / "brief.md").write_text(brief)
    assert cli.main(task_add) == 1
    assert "brief must contain between 1 and 8000 characters" in capsys.readouterr().err
    assert not (tmp_path / "state.db.tasks.git").exists()


def test_task_add_respects_store_lease(task_add, tmp_path, monkeypatch, capsys):
    from steward_harness.task_store import GitTaskStore
    from steward_harness.lease import Lease

    store = GitTaskStore(tmp_path / "state.db.tasks.git")
    # A second handle cannot enter the shared writer lock from another thread.
    store.lease = Lease(tmp_path, lock_name=".state.db.tasks.git.lock", timeout_seconds=0)
    monkeypatch.setattr(cli, "GitTaskStore", lambda _path: store)
    holder = Lease(tmp_path, lock_name=".state.db.tasks.git.lock")
    results = []
    import threading

    with holder:
        thread = threading.Thread(target=lambda: results.append(cli.main(task_add)))
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert results == [1]
    assert "busy" in capsys.readouterr().err
    assert store.refs() == {}
