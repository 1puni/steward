"""One explicit baseline for Git invoked in agent-writable repositories."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from steward_harness.runtime.execution import UntrustedExecutionBroker

_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SCP_REMOTE = re.compile(
    r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?:"
    r"[A-Za-z0-9._~/-]+"
)
_INVALID_REF_CHARACTERS = frozenset(" ~^:?*[\\")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}")
# Seven is Git's own floor for an abbreviation and forty is the whole thing.
# Deliberately narrower than `deploy/health.py`'s `[0-9a-f]{7,64}`, which asks a
# different question: that one classifies a string in a *foreign* health
# document, where 64 admits a service whose own object store is SHA-256. This
# one names an object in the controller's store, which `ControllerGitTransport`
# pins to `object_format: sha1`, so forty is the ceiling by construction.
_ABBREVIATED_OBJECT_ID = re.compile(r"[0-9a-f]{7,40}")

STEWARD_ACTOR_NAME = "Steward"
STEWARD_ACTOR_EMAIL = "steward@localhost"

# Ambient user and system Git configuration is another author's state. A
# checkpoint must record the same identity and produce the same tree whichever
# host it runs on, so controller-owned Git reads neither file.
ISOLATED_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}

# Every Git operation a provider can interrupt mid-sequence. A worktree holding
# any of these markers has unresolved state that only its owner can finish;
# staging, committing, or merging over it would silently adopt that half-applied
# work as the steward's own.
IN_PROGRESS_GIT_MARKERS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
)


def controller_git_dir(state_db: str | Path, repository: str) -> Path:
    """Return one repository's controller-owned bare object-store path."""
    state_path = Path(state_db)
    if not state_path.is_absolute() or state_path == Path("/"):
        raise ValueError("state database path must be bounded and absolute")
    if _REPOSITORY_NAME.fullmatch(repository) is None:
        raise ValueError("repository name must be a simple slug")
    return state_path.parent / "git" / f"{repository}.git"


def validate_git_branch(branch: str) -> None:
    """Accept one ordinary branch name, not arbitrary Git revision syntax."""
    components = branch.split("/")
    if (
        not branch
        or branch.startswith(("-", "/", "refs/"))
        or branch.endswith(("/", "."))
        or "//" in branch
        or ".." in branch
        or "@{" in branch
        or branch == "@"
        or any(character in _INVALID_REF_CHARACTERS for character in branch)
        or any(ord(character) < 32 or ord(character) == 127 for character in branch)
        or any(
            not component
            or component.startswith(".")
            or component.endswith(".lock")
            for component in components
        )
    ):
        raise ValueError(f"invalid Git branch name: {branch!r}")


def validate_git_remote_url(remote: str, *, allow_local: bool) -> None:
    """Accept only transports whose command shape is fixed by the controller."""
    if not remote or remote.startswith("-"):
        raise ValueError("repository remote_url has an invalid command shape")
    path = Path(remote)
    if path.is_absolute():
        if path == Path("/"):
            raise ValueError("repository remote_url must be a bounded absolute path")
        if not allow_local:
            raise ValueError("local Git remote is not allowed by this transport")
        return

    parsed = urlsplit(remote)
    if parsed.scheme in {"https", "ssh"} and parsed.hostname:
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("repository remote_url has an invalid port") from exc
        if not parsed.path or parsed.path == "/" or parsed.query or parsed.fragment:
            raise ValueError("repository remote_url must name one repository path")
        if parsed.password is not None:
            raise ValueError("repository remote_url must not embed a password")
        if parsed.scheme == "https" and parsed.username is not None:
            raise ValueError("repository HTTPS remote_url must not embed credentials")
        return
    if _SCP_REMOTE.fullmatch(remote):
        return
    raise ValueError(
        "repository remote_url must be HTTPS, SSH, scp-style SSH, "
        "or a bounded absolute local path"
    )


def hardened_git_argv(*args: str) -> list[str]:
    """Disable repository hooks, fsmonitor, signature commands, and pathspec magic.

    This narrows executable surface in a local repository. It is not a
    credential boundary: privileged Git must ultimately run from trusted state.
    """
    return [
        "git",
        "--no-pager",
        "--no-replace-objects",
        "--literal-pathspecs",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "log.showSignature=false",
        *args,
    ]


def steward_commit_argv(
    *args: str,
    name: str = STEWARD_ACTOR_NAME,
    email: str = STEWARD_ACTOR_EMAIL,
) -> list[str]:
    """Prefix a commit-producing Git call with the steward's recorded identity.

    Signing is refused rather than inherited: an ambient `commit.gpgsign` would
    make a checkpoint depend on a key the execution identity does not hold.
    """
    return [
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "-c",
        "commit.gpgsign=false",
        *args,
    ]


def run_agent_git(
    broker: UntrustedExecutionBroker,
    *args: str,
    cwd: str | Path,
    timeout: float = 60,
    extra_env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Git that touches agent-owned state without controller credentials."""
    return broker.run(
        hardened_git_argv(*args),
        cwd=cwd,
        timeout=timeout,
        extra_env=extra_env,
        input_text=input_text,
    )


def redact_command_output(detail: str, *, tail: bool = False) -> str:
    """Bound command reasons, stripping control bytes and common credential forms.

    Every path that turns Git output into operator-facing text goes through here:
    remote URLs can carry an embedded token, and an unbounded stderr reaches the
    same reply channel as a one-line failure.
    """
    detail = re.sub(r"https?://[^\s]+", "[URL redacted]", detail)
    detail = re.sub(
        r"(?i)(authorization|api[_-]?key|access[_-]?token|token|password|secret)"
        r"[\"'\s]*[:=][^\n]+", r"\1=[redacted]", detail,
    )
    detail = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", detail)
    detail = "".join(c for c in detail if c == "\n" or ord(c) >= 32)
    return (detail[-1000:] if tail else detail[:1000]).strip()


def validate_object_id(sha: str) -> str:
    """Validate a full lowercase SHA-1 object ID and return it."""
    if _OBJECT_ID.fullmatch(sha) is None:
        raise ValueError(f"Git object ID must be a full lowercase SHA-1, got {sha!r}")
    return sha


def abbreviated_object_id(value: str) -> str | None:
    """Classify operator text: an abbreviation, a whole object ID, or neither.

    Returns the abbreviation, or ``None`` when ``value`` is already whole and
    no object store has to say what it meant. Anything that is not lowercase
    hex raises here, which is how ``main``, ``HEAD``, ``HEAD@{1}`` and ``v1.0``
    never reach Git as a revision at all: an operator deploying a branch name
    would be asking for whatever it points at next, which is not what a pinned
    commit means.

    Only an operator-input boundary may call this, and it must expand what it
    gets back before anything else sees it. An abbreviation names an object
    relative to one store; a ref, a receipt or a release directory holding one
    would be recording something that is not an identity.
    """
    if _ABBREVIATED_OBJECT_ID.fullmatch(value) is None:
        raise ValueError(
            f"Git object ID must be 7 to 40 lowercase hex characters, got {value!r}"
        )
    return None if _OBJECT_ID.fullmatch(value) else value


def agent_git(
    broker: UntrustedExecutionBroker,
    *args: str,
    cwd: str | Path,
    timeout: float = 120,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    error: Callable[[str], BaseException] | type[subprocess.CalledProcessError] = subprocess.CalledProcessError,
) -> str:
    """Run run_agent_git, raise on non-zero exit, return stripped stdout.

    Pass ``error=SomeErrorClass`` where ``SomeErrorClass("message")`` is valid
    to raise that type instead of ``subprocess.CalledProcessError``.
    """
    result = run_agent_git(
        broker, *args, cwd=cwd, timeout=timeout, extra_env=env, input_text=input_text
    )
    if result.returncode != 0:
        if error is subprocess.CalledProcessError:
            raise subprocess.CalledProcessError(
                result.returncode, result.args,
                output=result.stdout, stderr=result.stderr,
            )
        raise error(  # type: ignore[call-arg]
            f"Git command failed (exit {result.returncode}): "
            f"{redact_command_output(result.stderr)}"
        )
    return result.stdout.strip()
