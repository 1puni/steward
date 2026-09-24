"""Telegram inbound text parsing: whitelisted verbs and argument shapes."""

from __future__ import annotations

from steward_harness.telegram.commands import (
    BUILTIN_COMMAND_DESCRIPTIONS,
    BUILTIN_COMMAND_MODES,
    CommandArgumentMode,
    parse_inbound_text,
)


def test_no_arg_command_ignores_trailing_text() -> None:
    parsed = parse_inbound_text("/status anything here")
    assert parsed.command is not None
    assert parsed.command.name == "status"
    assert parsed.command.arg is None


def test_clear_is_canonical_and_reset_is_retired() -> None:
    clear = parse_inbound_text("/clear")
    reset = parse_inbound_text("/reset old words are ignored")
    assert clear.command is not None
    assert clear.command.name == "clear"
    assert clear.command.arg is None
    assert reset.error is not None


def test_public_command_menu_and_parser_share_one_canonical_vocabulary() -> None:
    expected = {
        "cancel", "clear", "git", "model", "model_family", "pause",
        "resume", "rhythm", "status", "task", "tasks",
    }
    assert set(BUILTIN_COMMAND_DESCRIPTIONS) == expected
    assert set(BUILTIN_COMMAND_MODES) == expected
    assert len(set(BUILTIN_COMMAND_DESCRIPTIONS.values())) == len(expected)
    for name in expected:
        parsed = parse_inbound_text(f"/{name}")
        assert parsed.command is not None
        assert parsed.command.name == name


def test_optional_token_command_takes_first_word_only() -> None:
    parsed = parse_inbound_text("/model deep extra words dropped")
    assert parsed.command is not None
    assert parsed.command.name == "model"
    assert parsed.command.arg == "deep"


def test_optional_token_command_with_no_arg() -> None:
    parsed = parse_inbound_text("/model_family")
    assert parsed.command is not None
    assert parsed.command.arg is None


def test_git_is_a_remainder_command() -> None:
    parsed = parse_inbound_text("/git reconcile app :: watch the auth refactor")
    assert parsed.command is not None
    assert parsed.command.name == "git"
    assert parsed.command.arg == "reconcile app :: watch the auth refactor"


def test_rhythm_is_a_remainder_command() -> None:
    parsed = parse_inbound_text("/rhythm set sleep wall_clock 03:30 Europe/Stockholm")
    assert parsed.command is not None
    assert parsed.command.name == "rhythm"
    assert parsed.command.arg == "set sleep wall_clock 03:30 Europe/Stockholm"


def test_unknown_verbs_are_commands_with_an_honest_error() -> None:
    """A typo or retired command must never silently become a model prompt."""
    for text in ("/reconcile app", "/land app", "/deploy app",
                 "/retarget task_1 app", "/goal ship it", "/stop"):
        parsed = parse_inbound_text(text)
        assert parsed.command is not None
        assert parsed.error is not None


def test_adapter_command_uses_declared_argument_shape() -> None:
    parsed = parse_inbound_text(
        "/build arm64 signed",
        {"build": CommandArgumentMode.OPTIONAL_REMAINDER},
    )
    assert parsed.command is not None
    assert parsed.command.name == "build"
    assert parsed.command.arg == "arm64 signed"
    assert parsed.error is None


def test_required_adapter_argument_reports_usage_error() -> None:
    parsed = parse_inbound_text(
        "/buddy",
        {"buddy": CommandArgumentMode.REQUIRED_TOKEN},
    )
    assert parsed.command is not None
    assert parsed.error == "/buddy requires an argument."


def test_command_name_is_case_and_hyphen_normalized() -> None:
    parsed = parse_inbound_text("/Model-Family fast")
    assert parsed.command is not None
    assert parsed.command.name == "model_family"


def test_command_with_bot_username_suffix_is_stripped() -> None:
    parsed = parse_inbound_text("/status@my_steward_bot")
    assert parsed.command is not None
    assert parsed.command.name == "status"


def test_plain_text_is_not_a_command() -> None:
    parsed = parse_inbound_text("just talking, not a slash command")
    assert parsed.command is None
