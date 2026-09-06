"""Load and validate .rune/config.toml into RuneConfig (DATA_MODEL.md §7)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import tomli_w
from pydantic import ValidationError

from rune.core.storage.canonical import atomic_write_text
from rune.core.storage.models import RuneConfig


class ConfigError(Exception):
    pass


def default_config() -> RuneConfig:
    return RuneConfig()


def load_config(config_path: Path) -> RuneConfig:
    if not config_path.exists():
        raise ConfigError(
            f"config not found: {config_path} (run `rune init` first)"
        )
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc
    try:
        return RuneConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {config_path}:\n{exc}") from exc


def write_default_config(config_path: Path) -> None:
    """Writes a fresh config skeleton. Only called when the file is missing
    (e.g. `rune init`, or `rune init --force` repairing a missing file) —
    never overwrites an existing config.toml.
    """
    atomic_write_text(
        config_path,
        tomli_w.dumps(default_config().model_dump(mode="json", exclude_none=True)),
    )
