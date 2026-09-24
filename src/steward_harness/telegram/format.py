"""Telegram formatting utilities and message chunking."""

from __future__ import annotations

import html
import re
import string
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from urllib.parse import urlsplit

_TELEGRAM_MESSAGE_UNITS = 4096

_INLINE_DELIMITERS = {
    "**": "b",
    "__": "b",
    "~~": "s",
    "||": "tg-spoiler",
    "*": "i",
    "_": "i",
}
_SAFE_LINK_SCHEMES = {"http", "https", "tg"}
_ESCAPABLE_MARKDOWN_CHARACTERS = frozenset(string.punctuation)
_CODE_ENTITY = re.compile(r"(<code>.*?</code>)", re.DOTALL)


def _find_unescaped(text: str, needle: str, start: int) -> int:
    while True:
        found = text.find(needle, start)
        if found < 0:
            return -1
        backslashes = 0
        cursor = found - 1
        while cursor >= 0 and text[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            return found
        start = found + len(needle)


def _safe_link_target(raw_target: str) -> str | None:
    """Return a Telegram-safe absolute link target, or no target."""
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    # A Markdown title follows whitespace. Telegram has no corresponding
    # entity attribute, so deliberately discard it.
    target = re.split(r"\s+[\"']", target, maxsplit=1)[0]
    try:
        scheme = urlsplit(target).scheme.lower()
    except ValueError:
        return None
    if scheme not in _SAFE_LINK_SCHEMES:
        return None
    return target


def _find_link_target_end(text: str, start: int) -> int:
    nested = 0
    cursor = start
    while cursor < len(text):
        if text[cursor] == "\\" and cursor + 1 < len(text):
            cursor += 2
            continue
        if text[cursor] == "(":
            nested += 1
        elif text[cursor] == ")":
            if nested == 0:
                return cursor
            nested -= 1
        cursor += 1
    return -1


def _wrap_outside_code(opening: str, closing: str, formatted: str) -> str:
    """Wrap an entity without illegally overlapping Telegram code entities."""
    parts = _CODE_ENTITY.split(formatted)
    return "".join(
        part if part.startswith("<code>") else f"{opening}{part}{closing}"
        for part in parts
        if part
    )


def _format_inline(text: str) -> str:
    """Convert the Telegram-supported, useful subset of inline Markdown."""
    output: list[str] = []
    cursor = 0

    while cursor < len(text):
        if (
            text[cursor] == "\\"
            and cursor + 1 < len(text)
            and text[cursor + 1] in _ESCAPABLE_MARKDOWN_CHARACTERS
        ):
            output.append(html.escape(text[cursor + 1], quote=False))
            cursor += 2
            continue

        if text[cursor] == "`":
            run = len(text[cursor:]) - len(text[cursor:].lstrip("`"))
            marker = "`" * run
            end = _find_unescaped(text, marker, cursor + run)
            if end >= 0:
                code = text[cursor + run : end].replace("\n", " ")
                output.append(f"<code>{html.escape(code, quote=False)}</code>")
                cursor = end + run
                continue

        link_start = cursor
        is_image = text.startswith("![", cursor)
        if is_image:
            link_start += 1
        if text.startswith("[", link_start):
            label_end = _find_unescaped(text, "]", link_start + 1)
            if label_end >= 0 and text.startswith("(", label_end + 1):
                target_end = _find_link_target_end(text, label_end + 2)
                if target_end >= 0:
                    label = text[link_start + 1 : label_end]
                    target = _safe_link_target(text[label_end + 2 : target_end])
                    if target is not None:
                        formatted_label = _format_inline(label) or html.escape(target)
                        output.append(
                            _wrap_outside_code(
                                f'<a href="{html.escape(target, quote=True)}">',
                                "</a>",
                                formatted_label,
                            )
                        )
                        cursor = target_end + 1
                        continue

        if text[cursor] == "<":
            autolink_end = text.find(">", cursor + 1)
            if autolink_end >= 0:
                raw_target = text[cursor + 1 : autolink_end]
                target = _safe_link_target(raw_target)
                if target is not None:
                    escaped_target = html.escape(target, quote=True)
                    output.append(f'<a href="{escaped_target}">{escaped_target}</a>')
                    cursor = autolink_end + 1
                    continue

        matched = False
        for delimiter, tag in _INLINE_DELIMITERS.items():
            if not text.startswith(delimiter, cursor):
                continue
            if delimiter == "_" and cursor > 0 and text[cursor - 1].isalnum():
                continue
            end = _find_unescaped(text, delimiter, cursor + len(delimiter))
            if end < 0:
                continue
            inner = text[cursor + len(delimiter) : end]
            if not inner or inner[0].isspace() or inner[-1].isspace():
                continue
            if delimiter == "_" and end + 1 < len(text) and text[end + 1].isalnum():
                continue
            output.append(
                _wrap_outside_code(f"<{tag}>", f"</{tag}>", _format_inline(inner))
            )
            cursor = end + len(delimiter)
            matched = True
            break
        if matched:
            continue

        output.append(html.escape(text[cursor], quote=False))
        cursor += 1

    return "".join(output)


def sanitize_markdown_for_telegram(text: str) -> str:
    """Convert Markdown to the safe HTML subset accepted by Telegram.

    User-supplied HTML is always escaped. The conversion intentionally covers
    the constructs provider replies commonly use: emphasis, strike-through,
    spoilers, links, headings, block quotes, inline code, and fenced code.
    Markdown list markers remain readable as ordinary text.
    """
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0

    while cursor < len(lines):
        line = lines[cursor]
        body = line.rstrip("\r\n")
        ending = line[len(body) :]

        fence = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$", body)
        if fence:
            marker = fence.group(1)
            language = fence.group(2).strip().split(maxsplit=1)[0:1]
            code_lines: list[str] = []
            cursor += 1
            while cursor < len(lines):
                candidate = lines[cursor]
                candidate_body = candidate.rstrip("\r\n")
                if re.match(
                    rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}\s*$",
                    candidate_body,
                ):
                    ending = candidate[len(candidate_body) :]
                    cursor += 1
                    break
                code_lines.append(candidate)
                cursor += 1
            class_name = ""
            if language and re.fullmatch(r"[A-Za-z0-9_+.-]{1,64}", language[0]):
                class_name = f' class="language-{html.escape(language[0], quote=True)}"'
            code = html.escape("".join(code_lines).rstrip("\r\n"), quote=False)
            output.append(f"<pre><code{class_name}>{code}</code></pre>{ending}")
            continue

        quote = re.match(r"^ {0,3}> ?(.*)$", body)
        if quote:
            quoted: list[str] = []
            while cursor < len(lines):
                candidate = lines[cursor]
                candidate_body = candidate.rstrip("\r\n")
                match = re.match(r"^ {0,3}> ?(.*)$", candidate_body)
                if not match:
                    break
                candidate_ending = candidate[len(candidate_body) :]
                quoted.append(_format_inline(match.group(1)) + candidate_ending)
                cursor += 1
            output.append(f"<blockquote>{''.join(quoted).rstrip()}</blockquote>")
            if cursor < len(lines):
                output.append("\n")
            continue

        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", body)
        if heading:
            output.append(
                _wrap_outside_code("<b>", "</b>", _format_inline(heading.group(1)))
                + ending
            )
        else:
            output.append(_format_inline(body) + ending)
        cursor += 1

    return "".join(output)


class _TelegramHTMLTokens(HTMLParser):
    """Tokenize only HTML generated by ``sanitize_markdown_for_telegram``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        self.tokens.append(("start", tag, self.get_starttag_text()))

    def handle_endtag(self, tag: str) -> None:
        self.tokens.append(("end", tag, f"</{tag}>"))

    def handle_data(self, data: str) -> None:
        self.tokens.append(("text", "", data))


def chunk_telegram_html(
    formatted: str, max_units: int = _TELEGRAM_MESSAGE_UNITS
) -> list[str]:
    """Chunk generated Telegram HTML while closing and reopening active tags."""
    if not formatted:
        return []
    if max_units <= 0:
        raise ValueError("max_units must be positive")

    parser = _TelegramHTMLTokens()
    parser.feed(formatted)
    parser.close()

    chunks: list[str] = []
    active: list[tuple[str, str]] = []
    current: list[str] = []
    current_units = 0

    def finish_chunk() -> None:
        nonlocal current, current_units
        if current_units == 0:
            return
        chunks.append("".join(current + [f"</{tag}>" for tag, _raw in reversed(active)]))
        current = [raw for _tag, raw in active]
        current_units = 0

    for kind, tag, value in parser.tokens:
        if kind == "start":
            active.append((tag, value))
            current.append(value)
            continue
        if kind == "end":
            if active and active[-1][0] == tag:
                active.pop()
            current.append(value)
            continue

        for character in value:
            units = len(character.encode("utf-16-le")) // 2
            if current_units and current_units + units > max_units:
                finish_chunk()
            if units > max_units:
                raise ValueError("max_units cannot accommodate one Unicode character")
            current.append(html.escape(character, quote=False))
            current_units += units

    finish_chunk()
    return chunks


def format_markdown_chunks(
    text: str, max_units: int = _TELEGRAM_MESSAGE_UNITS
) -> list[str]:
    """Convert one Markdown reply into valid, bounded Telegram HTML chunks."""
    return chunk_telegram_html(sanitize_markdown_for_telegram(text), max_units=max_units)


class ArtifactKind(StrEnum):
    IMAGE = "image"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class OutboundArtifact:
    kind: ArtifactKind
    path: str


class TelegramActionKind(StrEnum):
    PIN_REPLY = "pin_reply"
    PIN_MESSAGE = "pin_message"


@dataclass(frozen=True, slots=True)
class TelegramAction:
    kind: TelegramActionKind
    message_id: int | None = None


def extract_telegram_action_markers(text: str) -> tuple[str, list[TelegramAction]]:
    """Extract fixed-chat administrative requests from one provider reply."""
    pattern = re.compile(
        r"\[\[telegram_(pin_reply|pin_message)(?::([1-9][0-9]*))?\]\]"
    )
    actions: list[TelegramAction] = []

    def _replace(match: re.Match[str]) -> str:
        raw_kind, raw_message_id = match.groups()
        if raw_kind == "pin_reply" and raw_message_id is None:
            action = TelegramAction(TelegramActionKind.PIN_REPLY)
        elif raw_kind == "pin_message" and raw_message_id is not None:
            action = TelegramAction(
                TelegramActionKind.PIN_MESSAGE, int(raw_message_id)
            )
        else:
            return match.group(0)
        if action not in actions:
            actions.append(action)
        return ""

    clean_text = pattern.sub(_replace, text).strip()
    return clean_text, actions


def extract_artifact_markers(text: str) -> tuple[str, list[OutboundArtifact]]:
    """Extract harness-owned outbound attachment markers.

    ``send_document`` and its shorter ``send_file`` alias always use
    Telegram's document transport, preserving the filename and avoiding photo
    recompression. ``send_image`` retains the established photo behaviour.
    """
    pattern = re.compile(r"\[\[send_(image|document|file):([^\]]+)\]\]")
    artifacts: list[OutboundArtifact] = []

    def _replace(match: re.Match[str]) -> str:
        raw_kind, raw_path = match.groups()
        kind = ArtifactKind.IMAGE if raw_kind == "image" else ArtifactKind.DOCUMENT
        artifacts.append(OutboundArtifact(kind=kind, path=raw_path.strip()))
        return ""

    clean_text = pattern.sub(_replace, text).strip()
    return clean_text, artifacts
