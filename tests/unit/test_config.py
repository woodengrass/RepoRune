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
    assert config.semantic.max_tokens == 4000
    assert config.semantic.reasoning.enabled is True
    assert config.semantic.reasoning.effort is None
    assert config.semantic.reasoning.max_tokens is None


def test_load_config_accepts_custom_reasoning_section(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[semantic]\n"
        'provider = "openrouter"\n'
        'model = "qwen/qwen3.8-flash"\n'
        "max_tokens = 8000\n"
        "[semantic.reasoning]\n"
        'effort = "low"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.semantic.max_tokens == 8000
    assert config.semantic.reasoning.effort == "low"
    assert config.semantic.reasoning.enabled is True  # untouched default


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


def test_load_config_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    """A typo'd or stray top-level key must be a loud error, not silently
    ignored — otherwise a misspelled config section (e.g. `[rune]` instead
    of `[semantic]`) would look accepted while doing nothing.
    """
    path = tmp_path / "config.toml"
    path.write_text('unexpected_field = 1\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_rejects_unknown_nested_field(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[semantic]\n"
        'provider = "openrouter"\n'
        "typo_field = true\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_rejects_unknown_reasoning_field(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[semantic.reasoning]\n"
        "typo_field = true\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(path)
