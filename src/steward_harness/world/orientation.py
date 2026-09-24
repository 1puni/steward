"""Point cognition at its own filesystem instead of copying world knowledge."""

from __future__ import annotations

from datetime import UTC, datetime


def _workspace_header(now: datetime | None) -> str:
    current = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"Steward workspace ({current})\n"
        "Read AGENTS.md and README.md in your current workspace, then its charter "
        "(CHARTER.md, or rules.md) and docs/README.md as needed. "
    )


def world_orientation(now: datetime | None = None) -> str:
    """World files are read from the executing session's retained worktree."""
    return (
        _workspace_header(now)
        + "Read project knowledge from its owning files. Earlier exchanges are the "
        "messages of this world's turn commits: use git log and git show for history."
    )


def repository_orientation(now: datetime | None = None) -> str:
    """A worldless steward orients in the checkout of the repository it serves.

    There is no world to hold episodes, so the repository's own documentation
    is the durable surface. The workspace is read-only here: changes reach the
    repository through gated tasks, not through the conversation turn.
    """
    return (
        _workspace_header(now)
        + "Read project knowledge from its owning files. "
        "Use git log and git show for history. This workspace is read-only; "
        "changes reach the repository through gated tasks."
    )
