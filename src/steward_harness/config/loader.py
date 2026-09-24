"""Configuration loader for steward YAML documents."""

from __future__ import annotations

from pathlib import Path
import yaml

from steward_harness.config.schema import StewardConfig


def load_config(path: str | Path) -> StewardConfig:
    """Load, parse, and validate a steward YAML configuration file."""
    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as file:
        data = yaml.load(file, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))

    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML structure in {config_path}: expected mapping")

    return StewardConfig.model_validate(data)
