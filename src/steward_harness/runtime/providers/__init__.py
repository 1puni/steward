"""Configured provider adapters, each with its own stewardship-owned native home."""

from pathlib import Path

from steward_harness.config.schema import ProviderConfig
from steward_harness.runtime.contracts import CognitionAdapter, ProviderFamily
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.process import ProcessController
from steward_harness.runtime.providers.claude import ClaudeRuntime
from steward_harness.runtime.providers.codex_app_server import CodexAppServerRuntime

BUILTIN_FAMILIES = frozenset({"claude", "codex", "glm"})


def build_runtimes(
    provider: ProviderConfig,
    execution_broker: UntrustedExecutionBroker,
) -> dict[ProviderFamily, CognitionAdapter]:
    """Instantiate configured builtins only from their declared native homes."""
    homes = {name: Path(path) for name, path in provider.native_homes.items()}
    selected = provider.family_order
    missing = [name for name in selected if name in BUILTIN_FAMILIES and name not in homes]
    if missing:
        raise ValueError(
            "Missing provider.native_homes for configured builtins: " + ", ".join(missing)
            + ". Provision separate stewardship-owned native homes, or remove these "
            "providers from the configured provider order."
        )
    controller = ProcessController(execution_broker)
    runtimes: dict[ProviderFamily, CognitionAdapter] = {}
    if "claude" in selected:
        runtimes["claude"] = ClaudeRuntime(
            Path(provider.claude_executable),
            controller=controller,
            native_home=homes["claude"],
            base_url=provider.claude_anthropic_base_url,
            credential_path=(
                Path(provider.claude_credential_path)
                if provider.claude_credential_path is not None else None
            ),
        )
    if "glm" in selected:
        runtimes["glm"] = ClaudeRuntime(
            Path(provider.claude_executable),
            controller=controller,
            native_home=homes["glm"],
            base_url=provider.glm_anthropic_base_url,
            credential_path=Path(provider.glm_credential_path),
            family="glm",
        )
    if "codex" in selected:
        runtimes["codex"] = CodexAppServerRuntime(
            Path(provider.codex_executable),
            controller=controller,
            native_home=homes["codex"],
        )
    return runtimes
