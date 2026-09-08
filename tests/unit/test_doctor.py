from __future__ import annotations

import tomllib
from pathlib import Path

import tomli_w

from rune.core import doctor
from rune.core.doctor import CheckLevel, run_doctor
from rune.core.project import init_project


def _check(report, name: str):
    return next(c for c in report.checks if c.name == name)


def test_doctor_reports_healthy_on_fresh_project(git_repo: Path) -> None:
    layout = init_project(git_repo)
    report = run_doctor(layout)
    assert _check(report, "git").level == CheckLevel.ok
    assert _check(report, "config").level == CheckLevel.ok
    assert _check(report, "tree_sitter_parsers").level == CheckLevel.ok
    assert _check(report, "canonical_schema_versions").level == CheckLevel.ok


def test_doctor_warns_on_missing_cache(git_repo: Path) -> None:
    layout = init_project(git_repo)
    report = run_doctor(layout)
    assert _check(report, "cache").level == CheckLevel.warn


def test_doctor_errors_on_semantic_enabled_without_model(git_repo: Path) -> None:
    """Default SemanticConfig is `enabled=True, model=""` -- Milestone 5's
    round-17 decision (HANDOFF.md) says an empty model must fail loudly,
    not be silently treated as disabled. `rune doctor`'s static-only
    provider check must apply the same rule.
    """
    layout = init_project(git_repo)
    report = run_doctor(layout)
    provider_check = _check(report, "semantic_provider")
    assert provider_check.level == CheckLevel.error
    assert "semantic.model is empty" in provider_check.message
    assert not report.healthy


def test_doctor_ok_when_semantic_disabled(git_repo: Path) -> None:
    layout = init_project(git_repo)
    data = tomllib.loads(layout.config_path.read_text(encoding="utf-8"))
    data["semantic"]["enabled"] = False
    layout.config_path.write_text(tomli_w.dumps(data), encoding="utf-8")

    report = run_doctor(layout)
    assert _check(report, "semantic_provider").level == CheckLevel.ok


def test_doctor_never_raises_on_corrupt_cache(git_repo: Path) -> None:
    layout = init_project(git_repo)
    layout.memory_db.parent.mkdir(parents=True, exist_ok=True)
    layout.memory_db.write_bytes(b"not a real sqlite file")
    report = run_doctor(layout)
    assert _check(report, "cache").level == CheckLevel.error
    assert not report.healthy


def test_doctor_continues_after_an_unexpected_check_failure(git_repo: Path, monkeypatch) -> None:
    layout = init_project(git_repo)

    def broken_treesitter(_checks) -> None:
        raise RuntimeError("unexpected parser failure")

    monkeypatch.setattr(doctor, "_check_treesitter", broken_treesitter)
    report = run_doctor(layout)

    assert _check(report, "tree_sitter_parsers").level == CheckLevel.error
    assert "unexpected parser failure" in _check(report, "tree_sitter_parsers").message
    assert _check(report, "canonical_schema_versions").level == CheckLevel.ok
