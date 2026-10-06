"""Slack presentation for the shared builtins; no board authentication needed."""
import re

from steward_harness.telegram.commands import (
    BUILTIN_COMMAND_DESCRIPTIONS, BUILTIN_COMMAND_MODES, CommandArgumentMode,
    parse_inbound_text,
)

MODES = {**BUILTIN_COMMAND_MODES, "help": CommandArgumentMode.NONE}


def parse_command(text):
    return parse_inbound_text(text, MODES)


def command_text(text):
    names = "|".join(MODES)
    return re.sub(r"(?<![\w/])/(" + names + r")\b", r"!\1", text)


def task_card(task):
    return (f"{task.title}\n{task.status.value} · {task.repository} · {task.task_id.short}"
            f" · priority {task.priority}\nOwner: {task.owner or 'unowned'}")


def slack_command(commands, name, arg, route):
    if name == "help":
        return ("Slack commands (reply in a thread to keep its conversation):\n"
                + "\n".join(f"!{name}: {command_text(description).replace('topic', 'thread')}"
                            for name, description in BUILTIN_COMMAND_DESCRIPTIONS.items())
                + "\n!task show <task_id> displays the brief and recent checkpoints here."
                + "\nObservers: !help, !status, !tasks and !task show <task_id>."
                + " Other commands and conversations require an explicit operator grant.")
    if name == "tasks":
        return "Tasks (up to 20; use !task show <task_id>):\n\n" + (
            "\n\n".join(task_card(task) for task in commands.state.tasks.all()[:20]) or "No tasks yet.")
    if name == "task":
        return command_text(commands._task(arg, card=task_card))
    if name in {"model", "model_family", "clear", "cancel"}:
        return command_text(commands._conversation(name, arg, route, transport="slack"))
    if name not in BUILTIN_COMMAND_MODES:
        return "Unknown command. Use !help."
    return command_text(commands(name, arg, 0, 0, 0))


def observer_command(name, arg):
    return name in {"help", "status", "tasks"} or (
        name == "task" and len((arg or "").split()) == 2 and arg.split()[0] == "show")
