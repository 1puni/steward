"""Canonical identity for externally admitted conversation turns."""

from __future__ import annotations

import re

_TURN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def validate_turn_id(event_id: str) -> str:
    """Return one bounded single-line turn ID or reject it before state mutation."""
    if _TURN_ID.fullmatch(event_id) is None:
        raise ValueError(
            "turn event ID must be 1-128 ASCII letters, digits, dots, underscores, or hyphens"
        )
    return event_id
