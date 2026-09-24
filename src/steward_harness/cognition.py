"""The sole provider-neutral entry point for model-powered work."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from steward_harness.provider_types import (
    ModelChoice,
    ProviderFamily,
    ProviderProfile,
)
from steward_harness.runtime.contracts import (
    CognitionAdapter,
    MissingProviderSession,
    RuntimeExecutionError,
    RuntimeRequest,
    RuntimeResult,
    RuntimeInput,
    RuntimeInputResult,
    RuntimeUnavailable,
    SandboxMode,
    resolve_model,
)


def _CANCELLED() -> None:
    """Sentinel stopper: a cancel that arrived before or after any child."""


@dataclass(frozen=True, slots=True)
class CognitionRequest:
    """One model-powered operation with no provider-specific lifecycle fields."""

    execution_id: str
    profile: ProviderProfile
    prompt: str
    cwd: Path
    # None means no routine deadline; cancellation still applies.
    timeout_seconds: int | None
    provider_order: tuple[ProviderFamily, ...]
    model: ModelChoice | None = None
    provider_session_id: str | None = None
    session_provider: ProviderFamily | None = None
    images: tuple[Path, ...] = ()
    sandbox_mode: SandboxMode = "read-only"
    allow_empty_output: bool = False
    on_session_started: Callable[[ProviderFamily, str], None] = (
        lambda _provider, _session_id: None
    )
    on_session_invalidated: Callable[[ProviderFamily], None] = lambda _provider: None
    on_input_ready: Callable[[Callable[[RuntimeInput], None]], None] | None = None
    on_input_result: Callable[[RuntimeInputResult], None] = lambda _result: None

    def __post_init__(self) -> None:
        if not self.execution_id:
            raise ValueError("Cognition execution ID must be nonblank")
        if not self.prompt.strip():
            raise ValueError("Cognition prompt must be nonblank")
        if not self.cwd.is_absolute():
            raise ValueError("Cognition cwd must be absolute")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("Cognition timeout must be positive")
        if not self.provider_order:
            raise ValueError("Cognition request requires a provider")
        if any(not provider.strip() for provider in self.provider_order):
            raise ValueError("Cognition provider IDs must be nonblank")
        if len(set(self.provider_order)) != len(self.provider_order):
            raise ValueError("Cognition provider order must be unique")
        if (self.provider_session_id is None) != (self.session_provider is None):
            raise ValueError(
                "A saved session requires both its provider and session ID"
            )


class Cognition:
    """Select, guard, and run any model-powered operation."""

    def __init__(
        self,
        adapters: Mapping[ProviderFamily, CognitionAdapter],
        custom_models: Mapping[str, Mapping[str, ModelChoice | str]] | None = None,
        *,
        writable_roots: tuple[Path, ...] = (),
    ) -> None:
        self._adapters = dict(adapters)
        for provider, adapter in self._adapters.items():
            if not provider.strip() or not adapter.family.strip():
                raise ValueError("Provider adapter IDs must be nonblank")
            if adapter.family != provider:
                raise ValueError(
                    f"Adapter registered as {provider!r} belongs to {adapter.family!r}"
                )
        self._custom_models = custom_models
        self._writable_roots = tuple(path.resolve() for path in writable_roots)
        # execution_id -> how to stop the child running it, or None between
        # providers when no child is up. This is the whole of cancellation: it
        # exists while the process exists and is gone when it is.
        self._active: dict[str, Callable[[], None] | None] = {}
        self._lock = RLock()

    def cancel(self, execution_id: str) -> bool:
        """Stop the running execution. False if it is not running here."""
        with self._lock:
            if execution_id not in self._active:
                return False
            stop = self._active[execution_id]
            self._active[execution_id] = _CANCELLED
        if stop is not None:
            stop()
        return True

    def _register(self, execution_id: str, stop: Callable[[], None]) -> None:
        """Adopt the child's stopper, honouring a cancel that landed first."""
        with self._lock:
            if self._active.get(execution_id) is _CANCELLED:
                stop()
                return
            self._active[execution_id] = stop

    def run(self, request: CognitionRequest | Callable[[], CognitionRequest],
            *, execution_id: str | None = None) -> RuntimeResult:
        # Workspace preparation can take time before a provider exists. It is
        # still part of the live invocation that the operator can cancel.
        if isinstance(request, CognitionRequest):
            execution_id = request.execution_id
        if not execution_id:
            raise ValueError("deferred cognition requires its execution ID")
        with self._lock:
            if execution_id in self._active:
                raise RuntimeExecutionError(
                    f"Execution ID {execution_id!r} is already running"
                )
            self._active[execution_id] = None

        def cancelled() -> bool:
            with self._lock:
                return self._active.get(execution_id) is _CANCELLED

        unavailable: list[str] = []
        try:
            if callable(request):
                request = request()
            if request.execution_id != execution_id:
                raise ValueError("prepared cognition changed its execution ID")
            for provider in request.provider_order:
                if cancelled():
                    raise RuntimeExecutionError(
                        f"Execution ID {request.execution_id!r} was cancelled"
                    )
                adapter = self._adapters.get(provider)
                if adapter is None:
                    unavailable.append(f"{provider}: not registered")
                    continue
                if request.images and not adapter.capabilities.images:
                    unavailable.append(f"{provider}: missing images")
                    continue
                availability = adapter.available()
                if not availability.available:
                    detail = f": {availability.reason}" if availability.reason else ""
                    unavailable.append(f"{provider}: unavailable{detail}")
                    continue

                saved_session = (
                    request.provider_session_id
                    if request.session_provider == provider
                    else None
                )
                resolved = resolve_model(provider, request.profile,
                    {provider: {request.profile: request.model}} if request.model else self._custom_models)
                def execute(provider_session_id: str | None) -> tuple[RuntimeRequest, RuntimeResult]:
                    started_session: str | None = None

                    def session_started(session_id: str) -> None:
                        nonlocal started_session
                        started_session = session_id
                        request.on_session_started(provider, session_id)

                    runtime_request = RuntimeRequest(
                        execution_id=request.execution_id,
                        resolved=resolved,
                        provider_session_id=provider_session_id,
                        prompt=request.prompt,
                        cwd=request.cwd,
                        timeout_seconds=request.timeout_seconds,
                        images=request.images,
                        sandbox_mode=request.sandbox_mode,
                        allow_empty_output=request.allow_empty_output,
                        writable_roots=self._writable_roots
                        if request.sandbox_mode == "workspace-write" else (),
                        on_session_started=session_started,
                        on_started=lambda stop: self._register(
                            request.execution_id, stop
                        ),
                        on_input_ready=request.on_input_ready
                        if adapter.capabilities.ongoing_input else None,
                        on_input_result=request.on_input_result,
                    )
                    try:
                        result = adapter.execute(runtime_request)
                    except RuntimeExecutionError as error:
                        if error.session_id is None:
                            error.session_id = started_session
                        raise
                    return runtime_request, result

                try:
                    try:
                        runtime_request, result = execute(saved_session)
                    except MissingProviderSession as error:
                        if saved_session is None or error.session_id is not None:
                            raise
                        request.on_session_invalidated(provider)
                        if cancelled():
                            raise RuntimeExecutionError(
                                f"Execution ID {request.execution_id!r} was cancelled"
                            )
                        runtime_request, result = execute(None)
                except RuntimeUnavailable as error:
                    # The provider refused the turn once it had it: no capacity
                    # left, no usable credential. That is the same fact
                    # `available()` reports above, learned a moment later, so it
                    # gets the same answer — say why, and try the next provider.
                    #
                    # Availability was only ever asked of the local machine:
                    # whether the CLI is executable, whether a credential file
                    # parses. A provider answering "not until Tuesday" is
                    # unavailable in every sense that matters, and treating that
                    # as a failed turn made the configured fallback unreachable
                    # in precisely the case it was configured for. One usage
                    # limit then cost 989 turns in nine hours, because every
                    # caller retried the one provider that had already said no.
                    unavailable.append(f"{provider}: unavailable: {error}")
                    continue

                self._validate_result(runtime_request, result)
                if cancelled():
                    raise RuntimeExecutionError(
                        f"Execution ID {request.execution_id!r} was cancelled",
                        session_id=result.provider_session_id,
                    )
                return result

            detail = "; ".join(unavailable)
            raise RuntimeUnavailable(f"No provider can satisfy this turn: {detail}")
        finally:
            with self._lock:
                self._active.pop(execution_id, None)

    @staticmethod
    def _validate_result(request: RuntimeRequest, result: RuntimeResult) -> None:
        if result.resolved != request.resolved:
            raise RuntimeError("Adapter returned a different model selection")
        if not result.effective_model:
            raise RuntimeError("Adapter returned no effective model")
        if not result.provider_session_id:
            raise RuntimeError("Adapter completed without its persistent session")
