from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rune.cli.main import app
from rune.core.project import RuneLayout
from rune.core.scopes.model import load_scopes

runner = CliRunner()


def test_init_then_status_json_reports_zero_modified(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--path", str(git_repo)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["status", "--path", str(git_repo), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["files_indexed"] == 1
    assert payload["working_tree_fresh"] is True
    assert payload["files_modified"] == 0
    assert payload["files_added"] == 0
    assert payload["files_deleted"] == 0


def test_status_reports_modified_added_and_deleted_counts(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    (git_repo / "b.py").write_text("def bar():\n    pass\n", encoding="utf-8")
    runner.invoke(app, ["init", "--path", str(git_repo)])

    # modify a.py, delete b.py, add c.py
    (git_repo / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (git_repo / "b.py").unlink()
    (git_repo / "c.py").write_text("def baz():\n    pass\n", encoding="utf-8")

    result = runner.invoke(app, ["status", "--path", str(git_repo), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["working_tree_fresh"] is False
    assert payload["files_modified"] == 1
    assert payload["files_added"] == 1
    assert payload["files_deleted"] == 1


def test_update_then_status_reports_fresh_again(git_repo: Path) -> None:
    """Tests working-tree freshness reporting, not semantic -- explicitly
    disables semantic so this doesn't get tangled up in the (correct,
    separately-tested) config_error a bare, unconfigured `semantic.
    enabled=True` default now produces.
    """
    (git_repo / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    runner.invoke(app, ["init", "--path", str(git_repo)])
    (git_repo / ".rune" / "config.toml").write_text(
        "[semantic]\nenabled = false\n", encoding="utf-8"
    )
    (git_repo / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")

    result = runner.invoke(app, ["update", "--path", str(git_repo)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["status", "--path", str(git_repo), "--json"])
    payload = json.loads(result.output)
    assert payload["working_tree_fresh"] is True
    assert payload["files_modified"] == 0


def test_update_fails_loudly_on_semantic_config_error(git_repo: Path, monkeypatch) -> None:
    """ARCHITECTURE.md §4.5's three-tier provider health check, round 15:
    a model that was actually configured (not the untouched empty-string
    default) with no matching API key env var is a real setup mistake --
    `rune update` must still complete the deterministic index (printed in
    the "Updated: ..." line) but report semantic's config error loudly and
    exit non-zero, not bury it as just another stat.
    """
    (git_repo / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    runner.invoke(app, ["init", "--path", str(git_repo)])
    (git_repo / ".rune" / "config.toml").write_text(
        '[semantic]\nmodel = "some/model"\n', encoding="utf-8"
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (git_repo / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")

    result = runner.invoke(app, ["update", "--path", str(git_repo)])
    assert result.exit_code == 1
    assert "Updated:" in result.output  # the deterministic index still ran and was reported
    assert "semantic" in result.output
    assert "OPENROUTER_API_KEY" in result.output


def test_update_marks_possibly_stale_when_no_provider_and_hash_changes(git_repo: Path, monkeypatch) -> None:
    """End-to-end for the round-15 possibly_stale trigger: a scope with a
    real prior summary whose member file content changes, on a run where
    semantic can't reach any provider (a config error here), must not
    keep reporting the old `fresh` status against content that no longer
    matches it.
    """
    import sqlite3

    from rune.core.hashing import content_hash
    from rune.core.storage.canonical import append_jsonl_many, write_json_model
    from rune.core.storage.models import (
        Scope,
        ScopeMembers,
        ScopesFile,
        ScopeSource,
        ScopeSummary,
        SemanticStatus,
    )

    (git_repo / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    runner.invoke(app, ["init", "--path", str(git_repo)])
    layout = RuneLayout(git_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.human,
                  members=ScopeMembers(files=["a.py"])),
        ]),
    )
    old_hash = content_hash((git_repo / "a.py").read_bytes())
    append_jsonl_many(
        layout.semantic_jsonl,
        [
            ScopeSummary(
                scope_id="app", revision=1, purpose="does foo things",
                generated_at="2026-01-01T00:00:00Z", model="m",
                source_hash=old_hash, source_files={"a.py": old_hash},
                status=SemanticStatus.fresh,
            )
        ],
    )
    result = runner.invoke(app, ["rebuild-cache", "--path", str(git_repo)])
    assert result.exit_code == 0, result.output

    (git_repo / ".rune" / "config.toml").write_text(
        '[semantic]\nmodel = "some/model"\n', encoding="utf-8"
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (git_repo / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")

    result = runner.invoke(app, ["update", "--path", str(git_repo)])
    assert result.exit_code == 1  # semantic config error, but the index still ran

    conn = sqlite3.connect(str(layout.memory_db))
    row = conn.execute(
        "SELECT current_revision, status FROM semantic_objects WHERE scope_id = 'app'"
    ).fetchone()
    assert row == (2, "possibly_stale")


def test_scope_suggest_rejection_has_no_canonical_side_effect(git_repo: Path) -> None:
    (git_repo / "app").mkdir()
    (git_repo / "app" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (git_repo / "app" / "b.py").write_text("y = 2\n", encoding="utf-8")
    assert runner.invoke(app, ["init", "--path", str(git_repo)]).exit_code == 0

    result = runner.invoke(app, ["scope", "suggest", "--path", str(git_repo)], input="n\n")

    assert result.exit_code == 0, result.output


def test_scope_suggest_before_cache_exists_fails_cleanly(git_repo: Path) -> None:
    """Regression test: a repo can legitimately have `.rune/scopes.json`
    committed while `.rune/cache/` (gitignored) doesn't exist yet on a
    fresh clone -- e.g. before the first `rune update`. `scope suggest`
    used to call `sqlite3.connect` unconditionally, which either raised an
    unhandled `OperationalError: unable to open database file` (cache
    directory missing) or silently created a stray empty `memory.db` and
    then crashed with `OperationalError: no such table: symbols` (cache
    directory present but the file itself missing) -- both reproduced by
    hand before this test was written. `status` already guarded this with
    `layout.memory_db.exists()`; `scope suggest` now does the same.
    """
    from rune.core.project import init_project
    from rune.core.update import run_update

    layout = init_project(git_repo)
    run_update(layout, full=True)
    layout.memory_db.unlink()

    result = runner.invoke(app, ["scope", "suggest", "--path", str(git_repo)])

    assert result.exit_code == 1
    assert not layout.memory_db.exists()  # no stray file left behind
    assert RuneLayout(git_repo).rune_dir.exists()  # sanity: didn't touch anything else
    assert load_scopes(RuneLayout(git_repo)).scopes == []


def test_search_json_output_includes_revision_field(git_repo: Path) -> None:
    """A relayed review confirmed by hand: `rune search --json` omitted
    the `revision` field SearchResult gained for the --history fix,
    leaving a machine consumer with no way to tell which revision a
    `superseded` hit came from.
    """
    import json as json_module

    runner.invoke(app, ["init", "--path", str(git_repo)])
    runner.invoke(
        app, ["decision", "propose", "d1", "--content", "use postgresql", "--path", str(git_repo)]
    )
    proposals = runner.invoke(app, ["proposal", "list", "--path", str(git_repo)]).output
    proposal_id = proposals.split()[0]
    runner.invoke(app, ["proposal", "approve", proposal_id, "--by", "alice", "--path", str(git_repo)])

    result = runner.invoke(app, ["search", "postgresql", "--json", "--path", str(git_repo)])
    assert result.exit_code == 0, result.output
    payload = json_module.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["revision"] == 1


def test_note_update_cli_exposes_scopes_files_symbols_and_expiry(git_repo: Path) -> None:
    """A relayed review confirmed by hand: `rune note update` didn't expose
    the scopes/files/symbols/importance/confidence/expires_at options
    core.memory.notes.note_update() already supported.
    """
    runner.invoke(app, ["init", "--path", str(git_repo)])
    add_output = runner.invoke(
        app, ["note", "add", "--category", "pitfall", "--content", "c", "--why-persist", "w",
              "--path", str(git_repo)]
    ).output
    note_id = add_output.split()[2]

    result = runner.invoke(
        app, ["note", "update", note_id, "--importance", "0.9", "--expires-at",
              "2099-01-01T00:00:00Z", "--path", str(git_repo)]
    )
    assert result.exit_code == 0, result.output
    assert "rev2" in result.output
