"""Resolve emitted turn IDs only from the world's observed published history."""

import logging
import re
import subprocess

from steward_harness.citations import GitCitations
from steward_harness.git_transport import ControllerGitTransport, GitTransportError
from steward_harness.telegram.format import TURN_REFERENCE

log = logging.getLogger(__name__)


def _world_web_url(remote: str) -> str:
    """Use the controller's trusted OpenSSH config, never alias-name heuristics.

    This is only for an operator-configured world remote. GitCitations itself
    must not evaluate SSH config for remotes read from an agent-owned checkout.
    """
    separator = "/" if remote.startswith("ssh://") else ":"
    ssh = re.fullmatch(
        rf"git@([A-Za-z0-9][A-Za-z0-9.-]*){separator}([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
        remote.removeprefix("ssh://"),
    )
    if ssh is None or ssh[1] == "github.com":
        return remote
    result = subprocess.run(
        ["ssh", "-G", "-o", "CanonicalizeHostname=no", "-o", "PermitLocalCommand=no", f"git@{ssh[1]}"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
    )
    settings = dict(line.split(None, 1) for line in result.stdout.splitlines() if " " in line)
    if result.returncode or settings.get("hostname", "").lower() != "github.com":
        raise ValueError("configured SSH alias has no verified GitHub web identity")
    return "https://github.com/" + ssh[2]


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
        reader = GitCitations(world.git_dir, web_url=_world_web_url(world.remote_url), revision=observed[0])
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
