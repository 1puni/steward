"""Every model-powered operation crosses one capability-aware boundary."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from concurrent.futures import ThreadPoolExecutor

import pytest

from steward_harness.cognition import Cognition, CognitionRequest
from steward_harness.provider_types import ModelChoice, ProviderFamily
from steward_harness.runtime.contracts import (
    Availability,
    MissingProviderSession,
    ProviderCapabilities,
    RuntimeExecutionError,
    RuntimeRequest,
    RuntimeResult,
    RuntimeUnavailable,
    SESSION_WORKSPACE_CAPABILITIES,
)


class FakeAdapter:
    def __init__(
        self,
        family: ProviderFamily,
        *,
        available: bool = True,
        capabilities: ProviderCapabilities = SESSION_WORKSPACE_CAPABILITIES,
    ) -> None:
        self.family = family
        self.is_available = available
        self.capabilities = capabilities
        self.requests: list[RuntimeRequest] = []

    def available(self) -> Availability:
        return Availability(
            self.is_available,
            None if self.is_available else "offline",
        )

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        session_id = request.provider_session_id or f"{self.family}-new"
        request.on_session_started(session_id)
        return RuntimeResult(
            output=f"reply from {self.family}",
            resolved=request.resolved,
            effective_model=request.resolved.model,
            provider_session_id=session_id,
        )

class FailingAfterSessionAdapter(FakeAdapter):
    failure = RuntimeExecutionError("adapter failed")

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        request.on_session_started("started-session")
        raise self.failure


class SessionlessAdapter(FakeAdapter):
    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        return RuntimeResult(
            output="stateless reply",
            resolved=request.resolved,
            effective_model=request.resolved.model,
            provider_session_id=None,
        )


class MissingSessionAdapter(FakeAdapter):
    def __init__(self, family: ProviderFamily, *, always_missing: bool = False) -> None:
        super().__init__(family)
        self.always_missing = always_missing

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        if request.provider_session_id is not None or self.always_missing:
            raise MissingProviderSession("saved session is gone")
        request.on_session_started(f"{self.family}-replacement")
        return RuntimeResult(
            output="rebuilt reply",
            resolved=request.resolved,
            effective_model=request.resolved.model,
            provider_session_id=f"{self.family}-replacement",
        )


class MissingAfterStartAdapter(FakeAdapter):
    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        raise MissingProviderSession(
            "failure after start", session_id="post-start-session"
        )


class RefusingAdapter(FakeAdapter):
    """Locally available, and the provider itself declines once it has the turn.

    This is a usage limit: nothing about the machine can predict it, so
    `available()` is honest in saying yes and the refusal only arrives per
    turn.
    """

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        raise RuntimeUnavailable(f"{self.family} has no capacity until Tuesday")


class RefusingOnRecoveryAdapter(FakeAdapter):
    """Declines only on the fresh attempt made after a saved session is gone."""

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        self.requests.append(request)
        if request.provider_session_id is not None:
            raise MissingProviderSession("saved session is gone")
        raise RuntimeUnavailable(f"{self.family} has no capacity until Tuesday")


def _request(tmp_path: Path, **overrides: object) -> CognitionRequest:
    values: dict[str, object] = {
        "execution_id": "turn-1",
        "profile": "balanced",
        "prompt": "Complete the operation.",
        "cwd": tmp_path,
        "timeout_seconds": 30,
        "provider_order": ("codex", "claude", "glm"),
    }
    values.update(overrides)
    return CognitionRequest(**values)  # type: ignore[arg-type]


def test_cancel_during_workspace_preparation_never_launches_provider(tmp_path):
    adapter = FakeAdapter("codex")
    cognition = Cognition({"codex": adapter})
    preparing, finish = Event(), Event()
    def prepare():
        preparing.set()
        assert finish.wait(5)
        return _request(tmp_path)
    with ThreadPoolExecutor() as executor:
        result = executor.submit(cognition.run, prepare, execution_id="turn-1")
        assert preparing.wait(5)
        try:
            assert cognition.cancel("turn-1")
        finally:
            finish.set()
        with pytest.raises(RuntimeExecutionError, match="cancelled"):
            result.result(timeout=5)
    assert not adapter.requests
    assert not cognition.cancel("turn-1")


def test_declared_provider_order_selects_the_first_available_adapter(
    tmp_path: Path,
) -> None:
    codex = FakeAdapter("codex", available=False)
    claude = FakeAdapter("claude")
    glm = FakeAdapter("glm")

    result = Cognition(
        {"codex": codex, "claude": claude, "glm": glm}
    ).run(_request(tmp_path))

    assert result.resolved.provider == "claude"
    assert not codex.requests
    assert len(claude.requests) == 1
    assert not glm.requests


def test_a_provider_that_refuses_the_turn_is_unavailable_not_failed(
    tmp_path: Path,
) -> None:
    """A refusal reaches the fallback; it does not end the turn.

    Availability used to be asked only of the local machine, so a provider
    that answered "no capacity" after being handed the work made the
    configured fallback unreachable in exactly the case it exists for.
    """
    codex = RefusingAdapter("codex")
    claude = FakeAdapter("claude")

    result = Cognition({"codex": codex, "claude": claude}).run(
        _request(tmp_path, provider_order=("codex", "claude"))
    )

    assert result.output == "reply from claude"
    assert len(codex.requests) == 1
    assert len(claude.requests) == 1


def test_a_refusal_while_recovering_a_lost_session_still_falls_through(
    tmp_path: Path,
) -> None:
    """The second attempt is inside the first one's handler, and must not escape it."""
    codex = RefusingOnRecoveryAdapter("codex")
    claude = FakeAdapter("claude")

    result = Cognition({"codex": codex, "claude": claude}).run(
        _request(
            tmp_path,
            provider_order=("codex", "claude"),
            provider_session_id="codex-old",
            session_provider="codex",
        )
    )

    assert result.output == "reply from claude"
    assert len(codex.requests) == 2


def test_every_provider_refusing_names_each_reason(tmp_path: Path) -> None:
    codex, claude = RefusingAdapter("codex"), RefusingAdapter("claude")

    with pytest.raises(RuntimeUnavailable) as caught:
        Cognition({"codex": codex, "claude": claude}).run(
            _request(tmp_path, provider_order=("codex", "claude"))
        )

    detail = str(caught.value)
    assert "codex: unavailable" in detail and "claude: unavailable" in detail
    assert "no capacity until Tuesday" in detail


def test_cancellation_during_availability_stops_fallback(tmp_path):
    cognition = None

    class Unavailable(FakeAdapter):
        def available(self):
            cognition.cancel("turn-1")
            return Availability(False, "offline")

    first, fallback = Unavailable("claude"), FakeAdapter("codex")
    cognition = Cognition({"claude": first, "codex": fallback})
    with pytest.raises(RuntimeExecutionError, match="cancelled"):
        cognition.run(_request(tmp_path, provider_order=("claude", "codex")))
    assert first.requests == fallback.requests == []


def test_a_returned_run_is_no_longer_cancellable(tmp_path):
    """The ordering the world-turn holes were deleted in favour of.

    `record_candidate` writes its row *after* `cognition.run` returns, so
    "a prepared world revision was withdrawn" is unreachable -- but only
    because `run`'s `finally` pops `_active` before it hands control back.
    Nothing else pins that, and the two `strict=True` xfails that claimed to
    were calling deleted APIs and never reached an assertion. If the pop moves
    after anything a caller can observe, this fails here rather than silently
    reopening a state four modules assume cannot be entered.
    """
    cognition = Cognition({"codex": FakeAdapter("codex")})

    cognition.run(_request(tmp_path, provider_order=("codex",)))

    assert cognition.cancel("turn-1") is False

    class Failing(FakeAdapter):
        def execute(self, request):
            raise RuntimeExecutionError("codex: exploded")

    # And a run that ends by raising releases it too: the interrupted path
    # reaches `record_candidate` for rhythms, so it makes the same claim.
    failing = Cognition({"codex": Failing("codex")})
    with pytest.raises(RuntimeExecutionError):
        failing.run(_request(tmp_path, provider_order=("codex",)))
    assert failing.cancel("turn-1") is False


def test_active_execution_cannot_run_another_provider_under_the_same_id(tmp_path):
    class Reentrant(FakeAdapter):
        def execute(self, request):
            with pytest.raises(RuntimeExecutionError, match="already running"):
                cognition.run(_request(tmp_path, provider_order=("codex",)))
            return super().execute(request)

    first, other = Reentrant("claude"), FakeAdapter("codex")
    cognition = Cognition({"claude": first, "codex": other})
    cognition.run(_request(tmp_path, provider_order=("claude",)))
    assert other.requests == []
    cognition.run(_request(tmp_path, provider_order=("codex",)))
    assert len(other.requests) == 1


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_configured_operating_directories_reach_native_execution(tmp_path, mode):
    adapter = FakeAdapter("codex")
    roots = (tmp_path / "work", tmp_path / "repository")
    for root in roots:
        root.mkdir()
    cognition = Cognition({"codex": adapter}, writable_roots=roots)
    cognition.run(_request(tmp_path, sandbox_mode=mode))
    assert adapter.requests[0].writable_roots == (
        roots if mode == "workspace-write" else ()
    )


def test_new_provider_requires_only_an_adapter_and_declared_models(
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter("future-provider")

    result = Cognition(
        {"future-provider": adapter},
        custom_models={"future-provider": {"balanced": "future-model"}},
    ).run(
        _request(tmp_path, provider_order=("future-provider",))
    )

    assert result.resolved.provider == "future-provider"
    assert result.resolved.model == "future-model"
    # A family the built-in table does not know has no built-in effort to keep.
    # This used to read `"medium"`, which was neither declared by the operator
    # nor built into the harness: it was invented at the point of use.
    assert adapter.requests[0].resolved.reasoning_effort is None

    declared = FakeAdapter("future-provider")
    Cognition(
        {"future-provider": declared},
        custom_models={
            "future-provider": {"balanced": ModelChoice(model="future-model", effort="high")}
        },
    ).run(_request(tmp_path, provider_order=("future-provider",)))

    assert declared.requests[0].resolved.reasoning_effort == "high"


@pytest.mark.parametrize("ongoing_input", [False, True])
def test_optional_live_input_preserves_selected_provider(tmp_path, ongoing_input):
    from dataclasses import replace

    adapter = FakeAdapter("codex", capabilities=replace(
        SESSION_WORKSPACE_CAPABILITIES, ongoing_input=ongoing_input,
    ))
    ready = lambda sender: None
    result = Cognition({"codex": adapter}).run(_request(
        tmp_path, on_input_ready=ready,
    ))
    assert result.resolved.provider == "codex"
    assert adapter.requests[0].on_input_ready is (ready if ongoing_input else None)


def test_fallback_never_receives_another_providers_session(
    tmp_path: Path,
) -> None:
    codex = FakeAdapter("codex", available=False)
    claude = FakeAdapter("claude")
    started: list[tuple[ProviderFamily, str]] = []

    Cognition({"codex": codex, "claude": claude}).run(
        _request(
            tmp_path,
            provider_order=("codex", "claude"),
            provider_session_id="codex-current",
            session_provider="codex",
            on_session_started=lambda provider, session: started.append(
                (provider, session)
            ),
        )
    )

    request = claude.requests[0]
    assert request.provider_session_id is None
    assert request.prompt == "Complete the operation."
    assert started == [("claude", "claude-new")]


def test_current_provider_resumes_only_its_session(tmp_path: Path) -> None:
    codex = FakeAdapter("codex", capabilities=ProviderCapabilities(images=True))

    Cognition({"codex": codex}).run(
        _request(
            tmp_path,
            provider_order=("codex",),
            provider_session_id="codex-current",
            session_provider="codex",
        )
    )

    assert codex.requests[0].provider_session_id == "codex-current"
    assert codex.requests[0].prompt == "Complete the operation."


def test_missing_saved_session_retries_once_on_same_adapter_with_full_prompt(
    tmp_path: Path,
) -> None:
    codex = MissingSessionAdapter("codex")
    claude = FakeAdapter("claude")
    invalidated: list[ProviderFamily] = []

    result = Cognition({"codex": codex, "claude": claude}).run(
        _request(
            tmp_path,
            provider_order=("codex", "claude"),
            prompt="Full task and durable context.",
            provider_session_id="codex-stale",
            session_provider="codex",
            on_session_invalidated=invalidated.append,
        )
    )

    assert result.provider_session_id == "codex-replacement"
    assert invalidated == ["codex"]
    assert [(item.provider_session_id, item.prompt) for item in codex.requests] == [
        ("codex-stale", "Full task and durable context."),
        (None, "Full task and durable context."),
    ]
    assert not claude.requests


def test_missing_session_without_a_saved_token_is_not_replayed(tmp_path: Path) -> None:
    adapter = MissingSessionAdapter("codex", always_missing=True)

    with pytest.raises(MissingProviderSession):
        Cognition({"codex": adapter}).run(
            _request(tmp_path, provider_order=("codex",))
        )

    assert len(adapter.requests) == 1


def test_second_missing_session_failure_is_not_replayed_again(tmp_path: Path) -> None:
    adapter = MissingSessionAdapter("codex", always_missing=True)

    with pytest.raises(MissingProviderSession):
        Cognition({"codex": adapter}).run(
            _request(
                tmp_path,
                provider_order=("codex",),
                provider_session_id="codex-stale",
                session_provider="codex",
            )
        )

    assert [item.provider_session_id for item in adapter.requests] == [
        "codex-stale",
        None,
    ]


def test_missing_session_reported_after_start_is_not_replayed(tmp_path: Path) -> None:
    adapter = MissingAfterStartAdapter("codex")

    with pytest.raises(RuntimeExecutionError) as raised:
        Cognition({"codex": adapter}).run(
            _request(
                tmp_path,
                provider_order=("codex",),
                provider_session_id="codex-stale",
                session_provider="codex",
            )
        )

    assert raised.value.session_id == "post-start-session"
    assert len(adapter.requests) == 1


def test_capability_filter_never_silently_drops_images(tmp_path: Path) -> None:
    image = tmp_path / "input.png"
    image.write_bytes(b"image")
    claude = FakeAdapter("claude")
    codex = FakeAdapter("codex", capabilities=ProviderCapabilities(images=True))

    result = Cognition({"claude": claude, "codex": codex}).run(
        _request(
            tmp_path,
            provider_order=("claude", "codex"),
            images=(image,),
        )
    )

    assert result.resolved.provider == "codex"
    assert not claude.requests
    assert codex.requests[0].images == (image,)


def test_missing_capability_fails_before_any_adapter_runs(tmp_path: Path) -> None:
    claude = FakeAdapter("claude")
    image = tmp_path / "input.png"
    image.write_bytes(b"image")

    with pytest.raises(RuntimeUnavailable, match="missing images"):
        Cognition({"claude": claude}).run(
            _request(
                tmp_path,
                provider_order=("claude",),
                images=(image,),
            )
        )

    assert not claude.requests


def test_started_session_survives_an_adapter_failure(tmp_path: Path) -> None:
    adapter = FailingAfterSessionAdapter("claude")
    adapter.failure = RuntimeExecutionError("native provider failure")
    fallback = FakeAdapter("codex")

    with pytest.raises(RuntimeExecutionError) as raised:
        Cognition({"claude": adapter, "codex": fallback}).run(
            _request(
                tmp_path,
                provider_order=("claude", "codex"),
                provider_session_id="claude-saved",
                session_provider="claude",
            )
        )

    assert raised.value.session_id == "started-session"
    assert raised.value is adapter.failure
    assert len(adapter.requests) == 1
    assert not fallback.requests, "classification does not authorize replay of partial work"


def test_saved_session_cannot_exist_without_its_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="both its provider and session ID"):
        _request(tmp_path, provider_session_id="orphan")


def test_adapter_without_a_persistent_session_is_a_defect(tmp_path: Path) -> None:
    adapter = SessionlessAdapter("claude")

    with pytest.raises(RuntimeError, match="without its persistent session"):
        Cognition({"claude": adapter}).run(_request(tmp_path, provider_order=("claude",)))


def test_provider_switch_keeps_the_task_context_but_starts_a_fresh_session(
    tmp_path: Path,
) -> None:
    claude = FakeAdapter("claude")
    codex = FakeAdapter("codex", capabilities=ProviderCapabilities(images=True))
    cognition = Cognition({"claude": claude, "codex": codex})

    first = cognition.run(
        _request(
            tmp_path,
            execution_id="task-1-attempt-1",
            provider_order=("claude",),
            prompt="Task: repair the parser. Current checkpoint: abc123.",
        )
    )
    cognition.run(
        _request(
            tmp_path,
            execution_id="task-1-attempt-2",
            provider_order=("codex",),
            prompt="Task: repair the parser. Current checkpoint: def456.",
            provider_session_id=first.provider_session_id,
            session_provider="claude",
        )
    )

    switched = codex.requests[0]
    assert switched.cwd == tmp_path
    assert switched.prompt == "Task: repair the parser. Current checkpoint: def456."
    assert switched.provider_session_id is None


def test_wrong_adapter_identity_is_a_contract_fault_not_a_provider_outage(tmp_path):
    from dataclasses import replace

    class WrongIdentity(FakeAdapter):
        def execute(self, request):
            result = super().execute(request)
            return replace(result, resolved=replace(result.resolved, provider="another-provider"))

    fallback = FakeAdapter("codex")
    with pytest.raises(RuntimeError, match="different model selection") as raised:
        Cognition({"claude": WrongIdentity("claude"), "codex": fallback}).run(
            _request(tmp_path, provider_order=("claude", "codex"))
        )
    assert not isinstance(raised.value, RuntimeExecutionError)
    assert fallback.requests == []
