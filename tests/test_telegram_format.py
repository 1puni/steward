"""Safe Markdown rendering for Telegram's HTML parse mode."""

from __future__ import annotations

from html.parser import HTMLParser

from steward_harness.telegram.format import (
    chunk_telegram_html,
    format_markdown_chunks,
    sanitize_markdown_for_telegram,
)


class _StrictTelegramHTMLParser(HTMLParser):
    _allowed = {"a", "b", "blockquote", "code", "i", "pre", "s", "tg-spoiler"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        assert tag in self._allowed
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack.pop() == tag

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _visible_text(value: str) -> str:
    parser = _StrictTelegramHTMLParser()
    parser.feed(value)
    parser.close()
    assert parser.stack == []
    return "".join(parser.text)


def test_markdown_conversion_covers_provider_reply_constructs() -> None:
    source = (
        "# Result\n\n"
        "**bold**, *italic*, ~~gone~~, ||secret||, and `x < y`.\n"
        "[docs](https://example.com/a?x=1&y=2)\n\n"
        "> quoted **text**\n\n"
        "```python\nprint('<safe>')\n```\n"
    )

    formatted = sanitize_markdown_for_telegram(source)

    assert "<b>Result</b>" in formatted
    assert "<b>bold</b>" in formatted
    assert "<i>italic</i>" in formatted
    assert "<s>gone</s>" in formatted
    assert "<tg-spoiler>secret</tg-spoiler>" in formatted
    assert "<code>x &lt; y</code>" in formatted
    assert '<a href="https://example.com/a?x=1&amp;y=2">docs</a>' in formatted
    assert "<blockquote>quoted <b>text</b></blockquote>" in formatted
    assert '<pre><code class="language-python">print(\'&lt;safe&gt;\')</code></pre>' in formatted


def test_markdown_conversion_escapes_html_and_rejects_unsafe_links() -> None:
    formatted = sanitize_markdown_for_telegram(
        '<b>not trusted</b> [click](javascript:alert("x"))'
    )

    assert "&lt;b&gt;not trusted&lt;/b&gt;" in formatted
    assert "<a " not in formatted
    assert "javascript" in formatted


def test_markdown_conversion_preserves_paths_and_avoids_formatting_code_overlap() -> None:
    formatted = sanitize_markdown_for_telegram(
        r"**Run `tool --path C:\Users\me` now**; keep C:\Users\me and \*literal*."
    )

    assert formatted == (
        r"<b>Run </b><code>tool --path C:\Users\me</code><b> now</b>; "
        r"keep C:\Users\me and *literal*."
    )


def test_formatted_chunks_are_bounded_valid_html_and_preserve_visible_text() -> None:
    source = "**" + ("ab😀&<>" * 12) + "**"
    formatted = sanitize_markdown_for_telegram(source)

    chunks = chunk_telegram_html(formatted, max_units=17)

    assert len(chunks) > 1
    assert all(
        len(_visible_text(chunk).encode("utf-16-le")) // 2 <= 17 for chunk in chunks
    )
    assert "".join(_visible_text(chunk) for chunk in chunks) == "ab😀&<>" * 12
    assert all(chunk.startswith("<b>") and chunk.endswith("</b>") for chunk in chunks)


def test_format_markdown_chunks_keeps_fenced_code_valid_across_chunks() -> None:
    chunks = format_markdown_chunks("```text\n" + ("x&" * 20) + "\n```", max_units=9)

    assert len(chunks) > 1
    assert all(chunk.startswith("<pre><code") for chunk in chunks)
    assert all(chunk.endswith("</code></pre>") for chunk in chunks)
    assert "".join(_visible_text(chunk) for chunk in chunks) == "x&" * 20
