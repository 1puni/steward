"""Steward-native configuration is explicit and stays outside publication trees."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from steward_harness.config.schema import (
    ProviderConfig,
    StewardConfig,
    UntrustedExecutionConfig,
)
from steward_harness.provider_types import ModelChoice
from steward_harness.runtime.contracts import RuntimeRequest, resolve_model
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.providers import build_runtimes
from steward_harness.runtime.providers.codex_app_server import CodexAppServerRuntime


def runtimes(tmp_path):
    homes = {name: str(tmp_path / name) for name in ("codex", "claude", "glm")}
    for path in homes.values():
        Path(path).mkdir()
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    return build_runtimes(ProviderConfig(native_homes=homes), broker), broker


def test_explicit_native_homes_select_runtime_and_do_not_inherit_personal_home(
    tmp_path,
):
    providers, broker = runtimes(tmp_path)
    assert isinstance(providers["codex"], CodexAppServerRuntime)
    assert providers["codex"].capabilities.ongoing_input
    personal = {
        "CODEX_HOME": "/personal/codex",
        "CLAUDE_CONFIG_DIR": "/personal/claude",
        "GH_TOKEN": "secret",
    }
    environment = providers["claude"].environment(personal)
    assert environment["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude")
    assert "CODEX_HOME" not in environment
    provider_env = broker._provider_environment(environment)
    assert provider_env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude")
    assert "GH_TOKEN" not in provider_env
    assert "CLAUDE_CONFIG_DIR" not in broker._command_environment(environment)
    codex = providers["codex"].environment(personal)
    assert codex["CODEX_HOME"] == str(tmp_path / "codex")
    assert "CLAUDE_CONFIG_DIR" not in codex
    assert "CODEX_HOME" not in broker._command_environment(codex)


@pytest.mark.parametrize("homes,missing", [({}, "codex, claude, glm"), ({"codex": "/srv/codex"}, "claude, glm")])
def test_builtin_factory_requires_all_selected_homes(homes, missing):
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    with pytest.raises(ValueError, match="Missing provider.native_homes") as caught:
        build_runtimes(ProviderConfig(native_homes=homes), broker)
    assert missing in str(caught.value)
    assert "Provision" in str(caught.value)
    assert "remove these providers" in str(caught.value)


def test_factory_builds_only_selected_builtins_without_provisioning_homes(tmp_path):
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    home = tmp_path / "codex"
    providers = build_runtimes(ProviderConfig(
        fallback_families=(), native_homes={"codex": str(home)},
    ), broker)
    assert set(providers) == {"codex"}
    assert not home.exists()
    assert build_runtimes(ProviderConfig(
        default_family="custom-adapter", fallback_families=(),
    ), broker) == {}


def test_factory_threads_anthropic_router_configuration(tmp_path):
    homes = {name: str(tmp_path / name) for name in ("claude", "glm")}
    token = tmp_path / "router-token"
    token.write_text("ci_live_router", encoding="utf-8")
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    providers = build_runtimes(ProviderConfig(
        default_family="claude", fallback_families=("glm",),
        native_homes=homes,
        glm_credential_path=str(token),
        glm_anthropic_base_url="https://gateway.example.com",
        claude_anthropic_base_url="https://gateway.example.com",
        claude_credential_path=str(token),
    ), broker)
    glm = providers["glm"]
    assert glm.base_url == "https://gateway.example.com"
    assert glm.environment()["ANTHROPIC_BASE_URL"] == "https://gateway.example.com"
    claude = providers["claude"]
    assert claude.base_url == "https://gateway.example.com"
    assert claude.credential_path == token
    environment = claude.environment()
    assert environment["ANTHROPIC_BASE_URL"] == "https://gateway.example.com"
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "ci_live_router"


def test_native_claude_loads_workflows_with_native_sandbox_still_enforced(tmp_path):
    providers, _ = runtimes(tmp_path)
    request = RuntimeRequest(
        execution_id="native",
        resolved=resolve_model("claude", "fast"),
        provider_session_id=None,
        prompt="work",
        cwd=tmp_path,
        timeout_seconds=10,
        sandbox_mode="workspace-write",
    )
    command = providers["claude"]._command(request, None)
    assert "--safe-mode" not in command
    assert "--strict-mcp-config" not in command
    assert "Agent" in command
    assert "--settings" in command
    assert "--dangerously-skip-permissions" not in command
    readonly = providers["claude"]._command(
        replace(request, sandbox_mode="read-only"), None
    )
    assert "Agent" not in readonly


@pytest.mark.parametrize("provider", ["claude", "glm"])
def test_effort_is_declarable_and_its_absence_omits_the_flag(tmp_path, provider):
    """Pinning a model used to be half a configuration.

    `glm` is where the other half bites: it runs through the Claude adapter
    against a third-party gateway, so it inherits Claude's effort vocabulary
    with no promise the gateway accepts a word of it. "Let the provider
    decide" had no spelling at all, because the flag went out unconditionally.
    """
    providers, _ = runtimes(tmp_path)
    request = RuntimeRequest(
        execution_id="effort",
        resolved=resolve_model(provider, "deep"),
        provider_session_id=None,
        prompt="work",
        cwd=tmp_path,
        timeout_seconds=10,
    )

    def command_for(configured):
        return providers[provider]._command(
            replace(request, resolved=resolve_model(provider, "deep", configured)), None
        )

    # A bare model name is what every configuration written before this said,
    # and it still says only which model: the built-in effort stands.
    def effort_in(command):
        index = command.index("--effort") if "--effort" in command else None
        return command[index + 1] if index is not None else None

    pinned = command_for({provider: {"deep": "pinned-model"}})
    assert pinned[pinned.index("--model") + 1] == "pinned-model"
    assert effort_in(pinned) == resolve_model(provider, "deep").reasoning_effort

    declared = command_for(
        {provider: {"deep": ModelChoice(model="pinned-model", effort="low")}}
    )
    assert effort_in(declared) == "low"

    delegated = command_for({provider: {"deep": ModelChoice(model="pinned-model")}})
    assert effort_in(delegated) is None
    assert delegated[delegated.index("--model") + 1] == "pinned-model"


@pytest.mark.parametrize("provider", ["claude", "glm"])
def test_native_memory_tracks_current_worktree_not_private_home(tmp_path, provider):
    providers, _ = runtimes(tmp_path)
    (tmp_path / "isolated world").mkdir()
    request = RuntimeRequest(
        execution_id="memory",
        resolved=resolve_model(provider, "fast"),
        provider_session_id=None,
        prompt="remember",
        cwd=tmp_path / "isolated world",
        timeout_seconds=10,
        sandbox_mode="workspace-write",
    )
    for mode in ("workspace-write", "read-only"):
        command = providers[provider]._command(replace(request, sandbox_mode=mode), None)
        settings = json.loads(command[command.index("--settings") + 1])
        assert settings["autoMemoryDirectory"] == str(request.cwd / "memories" / provider)
        assert settings["autoMemoryEnabled"] is (mode == "workspace-write")
        assert settings["sandbox"]["enabled"] is True
    assert not (request.cwd / "memories").exists(), "controller must not create model-owned memory files"


@pytest.mark.parametrize(
    "homes",
    [
        {"codex": "relative"},
        {"codex": "/"},
        {"codex": "/srv/native", "glm": "/srv/native/child"},
    ],
)
def test_native_home_paths_must_be_absolute_and_distinct(homes):
    with pytest.raises(ValidationError):
        ProviderConfig(native_homes=homes)


def test_native_home_cannot_contain_controller_database():
    with pytest.raises(ValidationError, match="controller-owned"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "test", "slug": "test"},
                "execution": {"user": "steward", "home": "/home/steward"},
                "provider": {
                    "native_homes": {"codex": "/var/lib/private"},
                    "state_db": "/var/lib/private/state.db",
                },
            }
        )


def test_native_home_cannot_be_checkpointed_with_world():
    with pytest.raises(ValidationError, match="private runtime data"):
        StewardConfig.model_validate(
            {
                "identity": {"name": "test", "slug": "test"},
                "world": {"root": "/srv/world"},
                "provider": {"native_homes": {"codex": "/srv/world/provider"}},
            }
        )
