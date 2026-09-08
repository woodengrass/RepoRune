from __future__ import annotations

from pathlib import Path

import pytest

from rune.core.memory.proposals import approve, propose
from rune.core.project import init_project
from rune.core.retrieval.check import check
from rune.core.storage.canonical import write_json_model
from rune.core.storage.models import (
    PersistenceMode,
    RecordType,
    Scope,
    ScopeMembers,
    ScopesFile,
    ScopeSource,
    Severity,
)
from rune.core.update import run_update


def test_check_missing_cache_returns_empty(git_repo: Path) -> None:
    layout = init_project(git_repo)
    result = check(layout)
    assert result.changed_files == []
    assert result.constraints == []


def test_check_no_changes_returns_empty(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    result = check(layout)
    assert result.changed_files == []


def test_check_does_not_treat_an_unreadable_file_as_deleted(
    python_simple_repo: Path, monkeypatch
) -> None:
    import rune.core.index.scanner as scanner_module

    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    real_hash = scanner_module.content_hash_of_file

    def failing_hash(path: Path) -> str:
        if path.name == "services.py":
            raise PermissionError("simulated sharing violation")
        return real_hash(path)

    monkeypatch.setattr(scanner_module, "content_hash_of_file", failing_hash)

    assert check(layout).changed_files == []


def test_check_surfaces_constraint_for_modified_scope(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="no bare except",
        severity=Severity.must, persistence_mode=PersistenceMode.persistent, scopes=["app"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    services = python_simple_repo / "app" / "services.py"
    services.write_text(services.read_text(encoding="utf-8") + "\n# x\n", encoding="utf-8")

    result = check(layout)
    assert result.changed_files == ["app/services.py"]
    assert result.affected_scope_ids == ["app"]
    assert len(result.constraints) == 1
    assert result.constraints[0].record_id == "c1"


def test_check_excludes_inactive_constraint(python_simple_repo: Path) -> None:
    from rune.core.memory.proposals import deactivate

    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="c",
        severity=Severity.must, persistence_mode=PersistenceMode.persistent, scopes=["app"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    deactivate(layout, RecordType.constraint, "c1", by="bob")
    run_update(layout, full=True)

    services = python_simple_repo / "app" / "services.py"
    services.write_text(services.read_text(encoding="utf-8") + "\n# x\n", encoding="utf-8")

    result = check(layout)
    assert result.constraints == []


def test_check_ignores_unrelated_scope(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", source=ScopeSource.human,
                  members=ScopeMembers(files=["app/main.py"])),
        ]),
    )
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="c",
        severity=Severity.must, persistence_mode=PersistenceMode.persistent, scopes=["app"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    # modify a file NOT in scope "app"
    services = python_simple_repo / "app" / "services.py"
    services.write_text(services.read_text(encoding="utf-8") + "\n# x\n", encoding="utf-8")

    result = check(layout)
    assert result.changed_files == ["app/services.py"]
    assert result.constraints == []


def test_check_includes_global_must_constraint_for_any_change(python_simple_repo: Path) -> None:
    """Confirmed with the user: `rune check` should surface current
    global MUST constraints (scopes==[]) too, not just scope-matched
    ones -- a global MUST rule is relevant to any change by definition.
    """
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="global-1",
        content="always run the full test suite before committing",
        severity=Severity.must, persistence_mode=PersistenceMode.persistent,
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    services = python_simple_repo / "app" / "services.py"
    services.write_text(services.read_text(encoding="utf-8") + "\n# x\n", encoding="utf-8")

    result = check(layout)
    ids = [c.record_id for c in result.constraints]
    assert "global-1" in ids
    global_entry = next(c for c in result.constraints if c.record_id == "global-1")
    assert global_entry.scope_ids == []


def test_check_global_must_only_included_when_something_changed(python_simple_repo: Path) -> None:
    """A global MUST constraint must not appear when nothing changed --
    `check` still reports "no changes" rather than always surfacing
    global rules regardless of working-tree state.
    """
    layout = init_project(python_simple_repo)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="global-1", content="c",
        severity=Severity.must, persistence_mode=PersistenceMode.persistent,
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    result = check(layout)
    assert result.changed_files == []
    assert result.constraints == []


def test_check_raises_cache_unusable_error_on_corrupt_db(git_repo: Path) -> None:
    from rune.core.storage.sqlite.materialize import CacheUnusableError

    layout = init_project(git_repo)
    layout.memory_db.parent.mkdir(parents=True, exist_ok=True)
    layout.memory_db.write_bytes(b"")

    with pytest.raises(CacheUnusableError):
        check(layout)


def test_check_finds_constraint_bound_directly_to_a_changed_file(python_simple_repo: Path) -> None:
    """A relayed review confirmed by hand: `rune check` only ever looked
    up constraints through scope membership (constraint_scopes), so a
    constraint bound directly to `files=[...]` with no `scopes` at all
    (e.g. a source_bound constraint) was invisible even when the exact
    file it's bound to changed. Uses SHOULD severity specifically so the
    global-MUST fallback path can't be what's finding it.
    """
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="prefer f-strings",
        severity=Severity.should, persistence_mode=PersistenceMode.source_bound,
        files=["app/services.py"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    services = python_simple_repo / "app" / "services.py"
    services.write_text(services.read_text(encoding="utf-8") + "\n# x\n", encoding="utf-8")

    result = check(layout)
    assert [c.record_id for c in result.constraints] == ["c1"]
    assert result.constraints[0].scope_ids == []


def test_check_finds_constraint_bound_to_a_symbol_in_a_changed_file(python_simple_repo: Path) -> None:
    import sqlite3

    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    conn = sqlite3.connect(str(layout.memory_db))
    symbol_id = conn.execute(
        "SELECT symbol_id FROM symbols WHERE qualified_name = 'UserService.get_user'"
    ).fetchone()[0]
    conn.close()

    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="must validate input",
        severity=Severity.should, persistence_mode=PersistenceMode.source_bound,
        symbols=[symbol_id],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    services = python_simple_repo / "app" / "services.py"
    services.write_text(services.read_text(encoding="utf-8") + "\n# x\n", encoding="utf-8")

    result = check(layout)
    assert [c.record_id for c in result.constraints] == ["c1"]


def test_check_matches_a_symbol_only_scope(python_simple_repo: Path) -> None:
    import sqlite3

    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    conn = sqlite3.connect(str(layout.memory_db))
    symbol_id = conn.execute(
        "SELECT symbol_id FROM symbols WHERE qualified_name = 'UserService.get_user'"
    ).fetchone()[0]
    conn.close()
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="service", name="Service", source=ScopeSource.human,
                  members=ScopeMembers(symbols=[symbol_id])),
        ]),
    )
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="must validate input",
        severity=Severity.should, persistence_mode=PersistenceMode.persistent, scopes=["service"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    services = python_simple_repo / "app" / "services.py"
    services.write_text(services.read_text(encoding="utf-8") + "\n# x\n", encoding="utf-8")
    result = check(layout)
    assert result.affected_scope_ids == ["service"]
    assert [constraint.record_id for constraint in result.constraints] == ["c1"]


def test_check_batches_more_than_sqlite_parameter_limit(python_simple_repo: Path) -> None:
    """Large change sets must not produce a single over-limit IN query or
    one scope lookup per path."""
    from rune.core.retrieval.check import _SQLITE_BATCH_SIZE

    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    for index in range(_SQLITE_BATCH_SIZE + 1):
        path = python_simple_repo / "generated" / f"file_{index}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")

    result = check(layout)

    assert len(result.changed_files) == _SQLITE_BATCH_SIZE + 1
