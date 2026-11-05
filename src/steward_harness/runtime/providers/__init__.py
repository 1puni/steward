"""Configured provider adapters, each with its own stewardship-owned native home."""

from collections.abc import Callable, Mapping
from pathlib import Path

from steward_harness.config.schema import ProviderConfig
from steward_harness.runtime.contracts import CognitionAdapter, ProviderFamily
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.process import ProcessController
from steward_harness.runtime.providers import claude, codex_app_server, deepseek

RuntimeFactory = Callable[
    [ProviderConfig, ProcessController, Mapping[str, Path]], CognitionAdapter
]

#: Every built-in family and the adapter module factory that instantiates it.
#: A new provider is one adapter module and one entry here.
BUILTIN_RUNTIMES: dict[ProviderFamily, RuntimeFactory] = {
    "claude": claude.build_claude,
    "glm": claude.build_glm,
    "codex": codex_app_server.build_codex,
    "deepseek": deepseek.build_deepseek,
}


def build_runtimes(
    provider: ProviderConfig,
    execution_broker: UntrustedExecutionBroker,
) -> dict[ProviderFamily, CognitionAdapter]:
    """Instantiate configured builtins only from their declared native homes."""
    homes = {name: Path(path) for name, path in provider.native_homes.items()}
    selected = [name for name in provider.family_order if name in BUILTIN_RUNTIMES]
    missing = [name for name in selected if name not in homes]
    if missing:
        raise ValueError(
            "Missing provider.native_homes for configured builtins: " + ", ".join(missing)
            + ". Provision separate stewardship-owned native homes, or remove these "
            "providers from the configured provider order."
        )
    controller = ProcessController(execution_broker)
    return {name: BUILTIN_RUNTIMES[name](provider, controller, homes) for name in selected}
