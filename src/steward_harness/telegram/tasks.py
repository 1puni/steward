"""Task presentation: titles for reading, derived references for commands."""

import re

from steward_harness.state import TaskId
from steward_harness.telegram.format import TASK_REFERENCE


STATUS_ICONS = {
    "proposed": "💡", "queued": "⏳", "running": "⚙️", "waiting": "💬",
    "blocked": "⚠️", "done": "✅", "cancelled": "🛑",
}


def app_link(url: str | None, reference: str | None = None) -> str | None:
    if url is None:
        return None
    return url + "?startapp=" + ("task_" + reference.lstrip("#") if reference else "")


def markdown_text(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~\-])", r"\\\1", " ".join(text.split()))


def discussion_link(owner: str | None, chat_id: int | None) -> str | None:
    """The admitted forum topic already names the task's discussion."""
    chat = str(chat_id)
    kind, _, topic = (owner or "").partition(":")
    if kind != "telegram" or not topic.isdecimal() or not chat.startswith("-100"):
        return None
    return f"https://t.me/c/{chat[4:]}/{int(topic) or 1}"


def task_card(task, app_url: str | None = None, chat_id: int | None = None) -> str:
    title = markdown_text(task.title)
    link = app_link(app_url, task.task_id.short) or discussion_link(task.definition.owner, chat_id)
    heading = f"[{title}]({link})" if link else f"**{title}**"
    return (
        f"{STATUS_ICONS[task.status.value]} {heading}\n"
        f"{task.status.value.capitalize()} · {markdown_text(task.repository)} · `{task.task_id.short}`"
        + (f" · priority {task.priority}" if task.priority else "")
    )


def reference_entities(
    store, text: str, app_url: str | None, chat_id: int | None = None,
) -> dict[str, tuple[str, str | None]]:
    """Only accepted task addresses receive controller-derived destinations."""
    words = set(TASK_REFERENCE.findall(text))
    # Ordinary words and short historical slugs should not turn prose into links.
    words = {word for word in words if len(word) >= 16 and "-" in word}
    if not words:
        return {}
    references = {}
    for ref, sha in store.refs().items():
        raw = ref.rsplit("/", 1)[-1]
        if raw in words:
            short = TaskId(raw).short
            link = app_link(app_url, short)
            if not link and str(chat_id).startswith("-100"):
                _, definition, _ = store.read(TaskId(raw), sha)
                link = discussion_link(definition.owner, chat_id)
            references[raw] = (short, link)
    return references
