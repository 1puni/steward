"""Declarative product command adapters stay behind the harness boundary."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from steward_harness.config.schema import (
    IdentityConfig,
    StewardConfig,
    UntrustedExecutionConfig,
)
from steward_harness.daemon import KernelCommands
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import StateDatabase


def _config(
    tmp_path: Path,
    *,
    allowed_topics: list[str] | None = None,
) -> StewardConfig:
    token = tmp_path / "token"
    token.write_text("tok")
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "import os, sys\n"
        "arg = sys.argv[1] if len(sys.argv) > 1 else 'none'\n"
        "print(f\"build {arg} topic={os.environ['STEWARD_TELEGRAM_TOPIC_ID']}\")\n"
    )
    return StewardConfig(
        identity=IdentityConfig(name="test", slug="test"),
        provider={"state_db": str(tmp_path / "state.db")},
        telegram={
            "chat_id": 1,
            "token_path": str(token),
            "allowed_users": [2],
            "topics": {"steward": 0, "qa": 7},
            "adapter_commands": {
                "build": {
                    "description": "Build a signed app artifact",
                    "argument_mode": "optional_remainder",
                    "allowed_topics": allowed_topics or [],
                    "command": {
                        "argv": [sys.executable, str(adapter)],
                        "cwd": str(tmp_path),
                        "timeout_seconds": 5,
                    },
                }
            },
        },
    )


def _commands(config: StewardConfig) -> KernelCommands:
    unused = cast(Any, object())
    return KernelCommands(
        config,
        StateDatabase(config.provider.state_db),
        unused,
        unused,
        {},
        UntrustedExecutionBroker(UntrustedExecutionConfig()),
    )


def test_adapter_command_is_parsed_and_executed(tmp_path: Path) -> None:
    reply = _commands(_config(tmp_path))(
        "build", "arm64 signed", 1, 8, 2
    )

    assert reply == "build arm64 signed topic=8"


def test_adapter_command_honors_topic_scope_without_starting_process(tmp_path: Path) -> None:
    reply = _commands(_config(tmp_path, allowed_topics=["steward"]))(
        "build", None, 1, 7, 2
    )

    assert reply == "/build is not allowed in this topic."


@pytest.mark.parametrize("name", ["status", "rhythm", "Bad-Name", "x" * 33])
def test_adapter_command_cannot_shadow_or_use_invalid_name(name: str) -> None:
    with pytest.raises(ValidationError):
        StewardConfig.model_validate(
            {
                "identity": {"name": "test", "slug": "test"},
                "telegram": {
                    "chat_id": 1,
                    "allowed_users": [1],
                    "adapter_commands": {
                        name: {
                            "description": "bad",
                            "command": {"argv": ["/bin/true"]},
                        }
                    }
                },
            }
        )


def test_adapter_command_rejects_unknown_topic_scope() -> None:
    with pytest.raises(ValidationError, match="unknown topics"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "test", "slug": "test"},
                "telegram": {
                    "chat_id": 1,
                    "allowed_users": [1],
                    "topics": {"steward": 0},
                    "adapter_commands": {
                        "build": {
                            "description": "Build",
                            "allowed_topics": ["ghost"],
                            "command": {"argv": ["/bin/true"]},
                        }
                    },
                },
            }
        )


def test_delivery_outbox_requires_quarantine_and_bounded_roots() -> None:
    with pytest.raises(ValidationError, match="requires delivery_quarantine_dir"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "test", "slug": "test"},
                "telegram": {
                    "chat_id": 1,
                    "allowed_users": [1],
                    "delivery_outbox_dir": "/var/lib/steward/outbox",
                    "delivery_roots": ["/var/lib/steward/artifacts"],
                },
            }
        )

    with pytest.raises(ValidationError, match="bounded absolute paths"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "test", "slug": "test"},
                "telegram": {
                    "chat_id": 1,
                    "allowed_users": [1],
                    "delivery_outbox_dir": "/var/lib/steward/outbox",
                    "delivery_quarantine_dir": "/var/lib/steward/quarantine",
                    "delivery_roots": ["/"],
                },
            }
        )


def _controller_config(tmp_path: Path, executable: Path) -> StewardConfig:
    token = tmp_path / "token"
    token.write_text("tok")
    return StewardConfig(
        identity=IdentityConfig(name="test", slug="test"),
        provider={"state_db": str(tmp_path / "state.db")},
        telegram={
            "chat_id": 1,
            "token_path": str(token),
            "allowed_users": [2],
            "topics": {"steward": 0},
            "adapter_commands": {
                "pair": {
                    "description": "Mint a one-time phone pairing link",
                    "authority": "controller",
                    "command": {"argv": [str(executable)], "timeout_seconds": 5},
                }
            },
        },
    )


class _NoBroker:
    """A controller command must never reach the agent's broker."""

    def run(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("controller command went through the agent broker")


def _controller_commands(config: StewardConfig) -> KernelCommands:
    unused = cast(Any, object())
    return KernelCommands(
        config, StateDatabase(config.provider.state_db), unused, unused, {},
        cast(Any, _NoBroker()),
    )


def _executable(tmp_path: Path, body: str) -> Path:
    directory = tmp_path / "libexec"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    script = directory / "pair"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return script


def test_controller_command_runs_as_controller_with_only_its_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTROLLER_SECRET", "must-not-leak")
    script = _executable(
        tmp_path,
        'echo "user=$STEWARD_TELEGRAM_USER_ID secret=${CONTROLLER_SECRET:-none}"',
    )

    reply = _controller_commands(_controller_config(tmp_path, script))("pair", None, 1, 0, 2)

    assert reply == "user=2 secret=none"


def test_controller_command_refuses_an_executable_others_can_replace(tmp_path: Path) -> None:
    script = _executable(tmp_path, "echo minted")
    script.chmod(0o777)

    reply = _controller_commands(_controller_config(tmp_path, script))("pair", None, 1, 0, 2)

    assert reply.startswith("⚠️ /pair refused:")
    assert "writable by others" in reply


def test_controller_command_refuses_a_writable_directory(tmp_path: Path) -> None:
    script = _executable(tmp_path, "echo minted")
    script.parent.chmod(0o777)

    reply = _controller_commands(_controller_config(tmp_path, script))("pair", None, 1, 0, 2)

    assert "writable by others" in reply


def test_controller_command_requires_an_absolute_executable() -> None:
    with pytest.raises(ValidationError, match="bounded absolute"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "test", "slug": "test"},
                "telegram": {
                    "chat_id": 1,
                    "allowed_users": [1],
                    "adapter_commands": {
                        "pair": {
                            "description": "Pair",
                            "authority": "controller",
                            "command": {"argv": ["pair"]},
                        }
                    },
                },
            }
        )


def test_controller_command_executable_is_a_protected_path(tmp_path: Path) -> None:
    script = _executable(tmp_path, "echo minted")

    broker = UntrustedExecutionBroker.for_steward(_controller_config(tmp_path, script))

    protected = broker._protected_paths
    assert protected["controller command 'pair'"] == script
    assert protected["controller command 'pair' directory"] == script.parent
