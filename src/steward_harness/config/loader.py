"""Configuration loader for steward YAML documents."""

from __future__ import annotations

from pathlib import Path
import yaml
from pydantic import ValidationError

from steward_harness.config.schema import StewardConfig


def load_config(path: str | Path) -> StewardConfig:
    """Load, parse, and validate a steward YAML configuration file."""
    return StewardConfig.model_validate(_read(path))


def load_known_config(path: str | Path) -> tuple[StewardConfig, list[str]]:
    """Load configuration that may be newer than this code, and what it ignored.

    Only a pinned deploy driver reads configuration this way. It is pinned
    outside the release so that a bad release cannot break the machinery that
    deploys it or rolls it back, and so it is older than the configuration
    every time an instance adopts a new field. Refusing that field stopped
    deployment while the service looked healthy. Ignoring it loses nothing
    the driver could have acted on: it can only enforce what it knows, and
    the controller re-proves its complete boundary under its own schema on
    every start. Only fields this code does not know are dropped; any other
    invalid value is still refused.
    """
    data = _read(path)
    ignored: list[str] = []
    while True:
        try:
            return StewardConfig.model_validate(data), ignored
        except ValidationError as error:
            errors = error.errors()
            if not errors or any(item["type"] != "extra_forbidden" for item in errors):
                raise
            for item in errors:
                *parents, key = item["loc"]
                container = data
                try:
                    for part in parents:
                        container = container[part]
                    del container[key]
                except (KeyError, IndexError, TypeError):
                    # A union branch's location names no data path: refuse.
                    raise error from None
                ignored.append(".".join(str(part) for part in item["loc"]))


def _read(path: str | Path) -> dict:
    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as file:
        data = yaml.load(file, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))

    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML structure in {config_path}: expected mapping")
    return data
