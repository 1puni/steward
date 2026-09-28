"""What an automatic run asks to send: nothing, unless it says NOTIFY.

A scheduled run speaks to its owner only by opting in. Told to stay silent,
models reply "SILENT", "(empty)", "<br>" or a lone word joiner, and no filter
of such replies can win: each night invents another. So silence is the
default and sending is the act. A `NOTIFY:` line starts the message, and
everything from it to the end is what the owner receives.

The marker is forgiving to write: any case, and Markdown emphasis, a bullet or
a quote before it are all accepted. Its failure is the safe one. A reply
without it is recorded, where the turn or the task's evidence commit already
keeps it, and sent to no one. Nothing blocks on it.
"""
from __future__ import annotations

import re
import unicodedata

MARKER = "NOTIFY:"

_MARKER_LINE = re.compile(
    r"^\s*(?:[-*+>]\s+)?(?:\*\*|__|\*|_)?notify(?:\*\*|__|\*|_)?\s*:(?:\*\*|__|\*|_)?[ \t]*",
    re.IGNORECASE,
)

#: A marker followed only by these asks for nothing, as `QUESTION: NONE` does.
_NOTHING = {"none", "nothing", "silent"}

DIRECTIVE = f"""\
Nothing you write here is sent to anyone unless you ask. To message this
run's owner, start a line with `{MARKER}` and put the message after it:
everything from that line to the end of your reply is sent. Without the line,
your reply is recorded and nobody is notified. Most runs should notify no one."""


def notification(text: str | None) -> str:
    """The message a reply asks to send, or empty when it asks for none."""
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        match = _MARKER_LINE.match(line)
        if match is None:
            continue
        rest = [line[match.end():], *(_MARKER_LINE.sub("", later, count=1) for later in lines[index + 1:])]
        message = "\n".join(rest).strip()
        if message.casefold().strip(" .") in _NOTHING:
            return ""
        # Something a person can read, not an invisible character.
        if not any(unicodedata.category(c)[0] not in "CZ" for c in message):
            return ""
        return message
    return ""
