"""Telegram command vocabulary and parser.

The harness owns command syntax and dispatch.  Product repositories may add
allowlisted adapter commands by supplying argument modes; their names never
need to be hard-coded into this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class CommandArgumentMode(StrEnum):
    """How much text following a command name belongs to its argument."""

    NONE = "none"
    OPTIONAL_TOKEN = "optional_token"
    OPTIONAL_REMAINDER = "optional_remainder"
    REQUIRED_TOKEN = "required_token"
    REQUIRED_REMAINDER = "required_remainder"


_BUILTIN_COMMANDS: Mapping[str, tuple[CommandArgumentMode, str]] = {
    "status": (
        CommandArgumentMode.NONE,
        "Inspect repositories, pipelines, conversation sessions, and scheduler state; changes nothing.",
    ),
    "tasks": (
        CommandArgumentMode.NONE,
        "Inspect proposed, queued, active, blocked, and completed tasks; changes nothing.",
    ),
    "model": (
        CommandArgumentMode.OPTIONAL_TOKEN,
        "Show or set this topic's quality (fast|balanced|deep) without forgetting its conversation.",
    ),
    "model_family": (
        CommandArgumentMode.OPTIONAL_TOKEN,
        "Show or switch this topic's configured provider; switching starts fresh.",
    ),
    "git": (
        CommandArgumentMode.OPTIONAL_REMAINDER,
        "Operate repositories: reconcile a conflict, deploy a release, or move a queued task to another repo.",
    ),
    "task": (
        CommandArgumentMode.OPTIONAL_REMAINDER,
        "Inspect or control one task: show, confirm, reject, answer, retry, note, cancel, priority, model, or model_family.",
    ),
    "rhythm": (
        CommandArgumentMode.OPTIONAL_REMAINDER,
        # This string is published through setMyCommands, so it is the operator's
        # menu. Advertise only what `_rhythm` accepts: schedules are edited in
        # controller configuration, not through a command.
        "Inspect scheduled cognition: list, or run <name>.",
    ),
    "clear": (
        CommandArgumentMode.NONE,
        "Forget this topic's conversation; the next message starts fresh. Other topics and tasks are unchanged.",
    ),
    "cancel": (
        CommandArgumentMode.NONE,
        "Cancel the active conversation turn in this topic.",
    ),
    "pause": (
        CommandArgumentMode.NONE,
        "Pause new scheduled work; in-flight work finishes and conversations remain intact.",
    ),
    "resume": (
        CommandArgumentMode.NONE,
        "Resume new scheduled work after /pause; conversations are unchanged.",
    ),
}

BUILTIN_COMMAND_MODES: Mapping[str, CommandArgumentMode] = {
    name: mode for name, (mode, _description) in _BUILTIN_COMMANDS.items()
}
BUILTIN_COMMAND_DESCRIPTIONS: Mapping[str, str] = {
    name: description for name, (_mode, description) in _BUILTIN_COMMANDS.items()
}

# These handlers only inspect/change controller state or interrupt an active
# execution. Git, rhythm host-wake updates, and product adapters may block on
# external work and remain on the ordinary executor path.
CONTROL_COMMANDS = frozenset({
    "status", "tasks", "task", "pause", "resume", "model", "model_family", "clear", "cancel",
})


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: str
    arg: str | None


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    command: ParsedCommand | None
    error: str | None = None


def parse_inbound_text(
    text: str,
    command_modes: Mapping[str, CommandArgumentMode] = BUILTIN_COMMAND_MODES,
) -> ParsedMessage:
    """Parse raw inbound Telegram text into a command or steering message."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return ParsedMessage(command=None)

    parts = stripped[1:].split(maxsplit=1)
    if not parts:
        return ParsedMessage(command=None, error="Use Telegram's command menu.")
    raw_name = parts[0].lower()
    if "@" in raw_name:
        raw_name = raw_name.split("@", 1)[0]
    canonical_name = raw_name.replace("-", "_")
    rest = parts[1].strip() if len(parts) > 1 else ""

    mode = command_modes.get(canonical_name)
    if mode is None:
        return ParsedMessage(
            command=ParsedCommand(canonical_name, rest or None),
            error=f"Unknown command /{canonical_name}. Use Telegram's command menu.",
        )
    if mode is CommandArgumentMode.NONE:
        return ParsedMessage(
            command=ParsedCommand(canonical_name, None),
        )

    if mode in {CommandArgumentMode.OPTIONAL_TOKEN, CommandArgumentMode.REQUIRED_TOKEN}:
        token = rest.split()[0] if rest else None
        error = None
        if mode is CommandArgumentMode.REQUIRED_TOKEN and token is None:
            error = f"/{canonical_name} requires an argument."
        return ParsedMessage(
            command=ParsedCommand(canonical_name, token),
            error=error,
        )

    if mode in {
        CommandArgumentMode.OPTIONAL_REMAINDER,
        CommandArgumentMode.REQUIRED_REMAINDER,
    }:
        error = None
        if mode is CommandArgumentMode.REQUIRED_REMAINDER and not rest:
            error = f"/{canonical_name} requires an argument."
        return ParsedMessage(
            command=ParsedCommand(canonical_name, rest or None),
            error=error,
        )

    raise AssertionError(f"unhandled command argument mode: {mode}")
