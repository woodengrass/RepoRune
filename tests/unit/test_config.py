from __future__ import annotations

from pathlib import Path

import pytest

from rune.core.config import ConfigError, load_config, write_default_config


def test_write_default_config_then_load_roundtrips(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_default_config(config_path)
    config = load_config(config_path)
    assert config.version == 1
    assert config.proposals.commit_to_git is False
    assert config.pricing.input_per_million == 0.0
    assert config.bootstrap.hard_budget_tokens == 3000
    assert config.bootstrap.soft_budget_tokens == 8000
    assert config.bootstrap.must_count_warn_threshold == 30


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "config.toml")


def test_load_config_invalid_toml_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("this is not [valid toml", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_bad_field_type_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('version = "not-an-int"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
