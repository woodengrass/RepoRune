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
    assert payload["protocol_version"] == 1
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


def test_status_does_not_count_an_unreadable_indexed_file_as_deleted(git_repo: Path, monkeypatch) -> None:
    import rune.core.index.scanner as scanner_module

    (git_repo / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    runner.invoke(app, ["init", "--path", str(git_repo)])
    real_hash = scanner_module.content_hash_of_file

    def failing_hash(path: Path) -> str:
        if path.name == "a.py":
            raise PermissionError("simulated sharing violation")
        return real_hash(path)

    monkeypatch.setattr(scanner_module, "content_hash_of_file", failing_hash)
    result = runner.invoke(app, ["status", "--path", str(git_repo), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["files_deleted"] == 0


def test_status_survives_a_corrupt_memory_db(git_repo: Path) -> None:
    """Relayed review, reproduced by hand: `compute_status` used a raw
    `sqlite3.connect` (not `connect_for_read`, unlike `rune search`/
    `check`/`bootstrap`), so a 0-byte `memory.db` crashed `rune status`
    with a raw `sqlite3.OperationalError: no such table: files` instead
    of the same clean-degrade contract those other readers already had.
    """
    (git_repo / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    runner.invoke(app, ["init", "--path", str(git_repo)])
    memory_db = git_repo / ".rune" / "cache" / "memory.db"
    memory_db.write_bytes(b"")

    result = runner.invoke(app, ["status", "--path", str(git_repo), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["cache_exists"] is True
    assert payload["cache_usable"] is False
    assert payload["files_indexed"] == 0
    assert payload["symbols_indexed"] == 0
    assert payload["working_tree_fresh"] is False


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
    assert payload["protocol_version"] == 1
    assert len(payload["results"]) == 1
    assert payload["results"][0]["revision"] == 1


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


def test_note_update_cli_clear_flags_replace_lists_with_empty(git_repo: Path) -> None:
    """Relayed review, reproduced by hand: `note update`'s CLI layer used
    `evidence or None` to decide whether to touch the evidence/scopes/
    files/symbols lists -- which meant "explicitly clear the list" and
    "didn't mention this flag at all" were indistinguishable (both are
    Typer's `[]` default for a repeatable option), so there was no way to
    clear one via the CLI even though `core.memory.notes.note_update`
    already supports `[]` meaning "replace with empty" (as opposed to
    `None` meaning "leave untouched"). Fixed with explicit `--clear-*`
    flags, mirroring the existing `--clear-expires-at`.
    """
    import json as json_module

    runner.invoke(app, ["init", "--path", str(git_repo)])
    add_output = runner.invoke(
        app, ["note", "add", "--category", "pitfall", "--content", "c", "--why-persist", "w",
              "--evidence", "e1", "--evidence", "e2", "--path", str(git_repo)]
    ).output
    note_id = add_output.split()[2]

    result = runner.invoke(
        app, ["note", "update", note_id, "--clear-evidence", "--path", str(git_repo)]
    )
    assert result.exit_code == 0, result.output

    lines = (git_repo / ".rune" / "notes.jsonl").read_text(encoding="utf-8").splitlines()
    revisions = [json_module.loads(line) for line in lines if line.strip()]
    latest = max(revisions, key=lambda r: r["revision"])
    assert latest["evidence"] == []


def test_scope_for_cli_json_round_trip(git_repo: Path) -> None:
    """The spike ARCHITECTURE.md §6/IMPLEMENTATION_PLAN.md Milestone 7
    calls for before full adapter development: `rune scope-for --path ...
    --json` must be a working, scriptable round trip an adapter (or, here,
    a plain subprocess call standing in for one) can drive."""
    import json as json_module

    (git_repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    runner.invoke(app, ["init", "--path", str(git_repo)])
    runner.invoke(
        app, ["scope", "create", "core", "--name", "Core", "--file", "a.py", "--path", str(git_repo)]
    )
    runner.invoke(app, ["update", "--path", str(git_repo)])

    result = runner.invoke(app, ["scope-for", "a.py", "--json", "--path", str(git_repo)])
    assert result.exit_code == 0, result.output
    payload = json_module.loads(result.output)
    assert payload["protocol_version"] == 1
    assert payload["path"] == "a.py"
    assert [s["scope_id"] for s in payload["scopes"]] == ["core"]


def test_bootstrap_cli_hard_and_soft_json_round_trip(git_repo: Path) -> None:
    """`rune bootstrap --mode hard|soft --json` (ARCHITECTURE.md §7.6) --
    the two payloads an OpenCode adapter calls on session.created /
    session.compacted."""
    import json as json_module

    (git_repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--path", str(git_repo)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["bootstrap", "--mode", "hard", "--json", "--path", str(git_repo)])
    assert result.exit_code == 0, result.output
    payload = json_module.loads(result.output)
    assert payload["protocol_version"] == 1
    assert payload["mode"] == "hard"
    assert payload["constraints"] == []
    assert payload["overflow"] is False

    result = runner.invoke(app, ["bootstrap", "--mode", "soft", "--json", "--path", str(git_repo)])
    assert result.exit_code == 0, result.output
    payload = json_module.loads(result.output)
    assert payload["protocol_version"] == 1
    assert payload["mode"] == "soft"
    assert payload["project_name"] is not None

    result = runner.invoke(app, ["bootstrap", "--mode", "bogus", "--path", str(git_repo)])
    assert result.exit_code == 1


def test_decision_constraint_note_propose_json_output(git_repo: Path) -> None:
    """The OpenCode adapter's custom tools (decision_propose/
    constraint_propose/note_add, ARCHITECTURE.md §6) need machine-readable
    output from these CLI entry points to report back to the agent."""
    import json as json_module

    result = runner.invoke(app, ["init", "--path", str(git_repo)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, [
        "decision", "propose", "d1", "--content", "use postgres", "--json", "--path", str(git_repo),
    ])
    assert result.exit_code == 0, result.output
    payload = json_module.loads(result.output)
    assert payload == {
        "protocol_version": 1,
        "proposal_id": payload["proposal_id"],
        "record_id": "d1",
        "status": "pending",
    }

    result = runner.invoke(app, [
        "constraint", "propose", "c1", "--content", "no bare except", "--severity", "MUST",
        "--persistence-mode", "persistent", "--json", "--path", str(git_repo),
    ])
    assert result.exit_code == 0, result.output
    payload = json_module.loads(result.output)
    assert payload == {
        "protocol_version": 1,
        "proposal_id": payload["proposal_id"],
        "record_id": "c1",
        "status": "pending",
    }

    result = runner.invoke(app, [
        "note", "add", "--category", "pitfall", "--content", "watch this", "--why-persist", "bit us once",
        "--json", "--path", str(git_repo),
    ])
    assert result.exit_code == 0, result.output
    payload = json_module.loads(result.output)
    assert payload["protocol_version"] == 1
    assert payload["category"] == "pitfall"


def test_doctor_cli_json_reports_checks(git_repo: Path) -> None:
    runner.invoke(app, ["init", "--path", str(git_repo)])
    result = runner.invoke(app, ["doctor", "--json", "--path", str(git_repo)])
    assert result.exit_code == 1, result.output  # unhealthy: semantic.model is empty by default
    payload = json.loads(result.output)
    assert payload["protocol_version"] == 1
    assert payload["healthy"] is False
    assert any(c["name"] == "config" and c["level"] == "ok" for c in payload["checks"])


def test_symbol_search_cli_json(python_simple_repo: Path) -> None:
    runner.invoke(app, ["init", "--path", str(python_simple_repo)])
    result = runner.invoke(app, ["symbol-search", "--name", "get_user", "--json", "--path", str(python_simple_repo)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["protocol_version"] == 1
    assert any(r["name"] == "get_user" for r in payload["results"])


def test_related_context_cli_json(python_simple_repo: Path) -> None:
    runner.invoke(app, ["init", "--path", str(python_simple_repo)])
    result = runner.invoke(
        app, ["related-context", "--path-filter", "app/services.py", "--json", "--path", str(python_simple_repo)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["protocol_version"] == 1
    assert "symbols" in payload


def test_related_context_cli_requires_a_selector(python_simple_repo: Path) -> None:
    runner.invoke(app, ["init", "--path", str(python_simple_repo)])
    result = runner.invoke(app, ["related-context", "--path", str(python_simple_repo)])
    assert result.exit_code == 1
