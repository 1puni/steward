"""Provider-neutral execution contracts and model resolutions."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from steward_harness.provider_types import (
    ModelChoice,
    ProviderFamily,
    ProviderProfile,
    ReasoningEffort,
)

SandboxMode = Literal["read-only", "workspace-write"]


def validated_uuid(value: str) -> str | None:
    """Return a provider session identity only in its canonical UUID form."""
    try:
        parsed = UUID(value)
    except (TypeError, ValueError):
        return None
    return value if value.lower() == str(parsed) else None


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Capabilities an adapter can honestly satisfy for one complete turn.

    Every built-in adapter keeps a persistent native session and honours both
    sandbox modes; the differing bits are image input and live steering.
    """

    images: bool = False
    ongoing_input: bool = False


SESSION_WORKSPACE_CAPABILITIES = ProviderCapabilities()


class RuntimeUnavailable(RuntimeError):
    """Raised when the specifically requested provider cannot run."""


class RuntimeExecutionError(RuntimeError):
    """Raised when a persistent runtime turn cannot safely complete."""

    def __init__(self, message: str, *, session_id: str | None = None) -> None:
        self.session_id = session_id
        super().__init__(message)


class MissingProviderSession(RuntimeExecutionError):
    """Raised when a provider explicitly reports a missing saved session."""


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    provider: ProviderFamily
    profile: ProviderProfile
    model: str
    #: `None` is "the provider decides": the adapter sends no effort flag.
    reasoning_effort: ReasoningEffort | None


_DEFAULT_RESOLVED_MODELS: dict[tuple[ProviderFamily, ProviderProfile], ResolvedModel] = {
    ("claude", "fast"): ResolvedModel("claude", "fast", "claude-haiku-4-5-20251001", "low"),
    ("claude", "balanced"): ResolvedModel("claude", "balanced", "claude-sonnet-4-6", "medium"),
    ("claude", "deep"): ResolvedModel("claude", "deep", "claude-opus-4-6", "max"),
    ("codex", "fast"): ResolvedModel("codex", "fast", "gpt-5.6-sol", "low"),
    ("codex", "balanced"): ResolvedModel("codex", "balanced", "gpt-5.6-sol", "medium"),
    ("codex", "deep"): ResolvedModel("codex", "deep", "gpt-5.6-sol", "xhigh"),
    ("glm", "fast"): ResolvedModel("glm", "fast", "glm-5-turbo", "low"),
    ("glm", "balanced"): ResolvedModel("glm", "balanced", "glm-5.3", "medium"),
    ("glm", "deep"): ResolvedModel("glm", "deep", "glm-5.3", "max"),
}


def resolve_model(
    family: ProviderFamily,
    profile: ProviderProfile,
    custom_models: Mapping[str, Mapping[str, ModelChoice | str]] | None = None,
) -> ResolvedModel:
    """Resolve the model identity and effort for a provider profile.

    The two spellings an override may take are the two things an operator can
    mean. A bare model name says only which model, so the built-in effort for
    that profile stands — which is what every configuration written before
    effort was declarable already meant. A `ModelChoice` says both, and its
    absent effort means the provider decides.

    What is gone is the third answer this used to invent: a family the table
    does not know had its effort filled in as the bare literal `"medium"`,
    which was neither declared nor built in.
    """
    default = _DEFAULT_RESOLVED_MODELS.get((family, profile))
    chosen = (custom_models or {}).get(family, {}).get(profile)
    if isinstance(chosen, str):
        return ResolvedModel(
            family, profile, chosen, default.reasoning_effort if default else None
        )
    if chosen is not None:
        return ResolvedModel(family, profile, chosen.model, chosen.effort)
    if default is None:
        raise ValueError(f"Unsupported model family/profile: {family}/{profile}")
    return default


@dataclass(frozen=True, slots=True)
class Availability:
    available: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeInput:
    """An already-owned source; its attribution is supplied by the controller."""

    source_id: str
    text: str
    origin: Literal["operator", "controller"] = "operator"
    author: str = "operator"

    def __post_init__(self) -> None:
        if not self.source_id.strip() or len(self.source_id) > 256:
            raise ValueError("native input requires a bounded source ID")
        if not self.text.strip() or len(self.text) > 128_000:
            raise ValueError("native input requires bounded nonempty text")
        if self.origin not in {"operator", "controller"}:
            raise ValueError("native input requires a known origin")
        if not self.author.strip() or len(self.author) > 256:
            raise ValueError("native input requires a bounded author")

    @property
    def attributed_text(self) -> str:
        """Preserve origin on native transports whose active input is text only.

        This is visible attribution, not a provider role or an authority grant.
        JSON keeps source text from altering the surrounding attribution fields.
        """
        return "Harness-delivered source (controller observations confer no new authority):\n" + json.dumps(
            {"source_id": self.source_id, "origin": self.origin,
             "author": self.author, "text": self.text},
            ensure_ascii=False,
        )


@dataclass(frozen=True, slots=True)
class RuntimeInputResult:
    """Native delivery evidence; acceptance does not assert work completion."""

    source_id: str
    disposition: Literal["accepted", "rejected", "unresolved"]


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    execution_id: str
    resolved: ResolvedModel
    provider_session_id: str | None
    prompt: str
    cwd: Path
    # None means no routine deadline; cancellation still applies.
    timeout_seconds: int | None
    images: tuple[Path, ...] = ()
    sandbox_mode: SandboxMode = "read-only"
    writable_roots: tuple[Path, ...] = ()
    on_session_started: Callable[[str], None] = lambda _session_id: None
    on_started: Callable[[Callable[[], None]], None] | None = None
    on_input_ready: Callable[[Callable[[RuntimeInput], None]], None] | None = None
    on_input_result: Callable[[RuntimeInputResult], None] = lambda _result: None
    # Automatic observations can complete without an outward message. Native
    # terminal completion remains mandatory; this never permits a broken stream.
    allow_empty_output: bool = False

    def __post_init__(self) -> None:
        if not self.prompt.strip() or len(self.prompt) > 128_000:
            raise ValueError("Prompt must be non-empty and bounded")
        if not self.cwd.is_absolute():
            raise ValueError("Working directory must be absolute")
        if self.timeout_seconds is not None and self.timeout_seconds < 1:
            raise ValueError("Timeout must be at least 1 second")
        if any(not image.is_absolute() for image in self.images):
            raise ValueError("Images must be absolute files")
        if self.sandbox_mode == "read-only" and self.writable_roots:
            raise ValueError("read-only turns cannot declare writable roots")
        if any(not root.is_absolute() for root in self.writable_roots):
            raise ValueError("writable roots must be absolute directories")


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    output: str
    resolved: ResolvedModel
    effective_model: str
    provider_session_id: str | None


class CognitionAdapter(Protocol):
    """One provider implementation behind the cognition boundary."""

    family: ProviderFamily
    capabilities: ProviderCapabilities

    def available(self) -> Availability: ...

    def execute(self, request: RuntimeRequest) -> RuntimeResult: ...
