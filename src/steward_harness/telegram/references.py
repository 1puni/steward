"""Resolve emitted turn IDs only from the world's observed published history."""

import logging
import subprocess

from steward_harness.citations import GitCitations
from steward_harness.git_transport import ControllerGitTransport, GitTransportError
from steward_harness.telegram.format import TURN_REFERENCE

log = logging.getLogger(__name__)


def turn_reference_entities(
    world: ControllerGitTransport | None, text: str,
) -> dict[str, tuple[str, str]]:
    references = set(TURN_REFERENCE.findall(text))
    if world is None or not references:
        return {}
    try:
        # This read validates the controller-owned store. Never inspect the
        # agent's mutable checkout or fetch just to decorate a message.
        observed = world.observed_tip()
        if observed is None:
            return {}
        reader = GitCitations(world.git_dir, web_url=world.remote_url, revision=observed[0])
        resolved = {}
        for reference in references:
            try:
                citation = reader.resolve(reference)
                resolved[reference] = (citation.label, citation.url)
            except ValueError:
                # Missing/ambiguous turns and unsupported remotes stay explicit.
                continue
        return resolved
    except (ValueError, OSError, subprocess.SubprocessError, GitTransportError):
        # Formatting is optional; inaccessible evidence must not block delivery.
        log.warning("World citations unavailable; preserving original references")
        return {}
