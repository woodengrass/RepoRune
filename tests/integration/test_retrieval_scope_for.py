from __future__ import annotations

from pathlib import Path

from rune.core.memory.notes import note_add
from rune.core.memory.proposals import approve, propose
from rune.core.project import init_project
from rune.core.retrieval.scope_for import resolve_scope_for, scope_for
from rune.core.storage.canonical import write_json_model
from rune.core.storage.models import (
    NoteCategory,
    PersistenceMode,
    RecordType,
    Scope,
    ScopeMembers,
    ScopesFile,
    ScopeSource,
    Severity,
)
from rune.core.update import run_update


def test_scope_for_missing_cache_returns_empty(git_repo: Path) -> None:
    layout = init_project(git_repo)
    assert scope_for(layout, "a.py") == []


def test_scope_for_file_not_in_any_scope_returns_empty(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    assert scope_for(layout, "app/services.py") == []


def test_scope_for_returns_scope_and_scoped_must_constraint(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", description="the app module", source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="no bare except",
        severity=Severity.must, persistence_mode=PersistenceMode.persistent, scopes=["app"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    results = scope_for(layout, "app/services.py")
    assert len(results) == 1
    assert results[0].scope_id == "app"
    assert results[0].name == "App"
    assert results[0].description == "the app module"
    assert [c.record_id for c in results[0].constraints] == ["c1"]


def test_scope_for_excludes_info_severity_constraint(python_simple_repo: Path) -> None:
    """ARCHITECTURE.md §7.1: only MUST/SHOULD are proactively injected on
    scope activation -- INFO-severity constraints are discoverable via
    `rune search` but not pushed into every scope activation.
    """
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
        layout, type=RecordType.constraint, record_id="c1", content="fyi only",
        severity=Severity.info, persistence_mode=PersistenceMode.persistent, scopes=["app"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    results = scope_for(layout, "app/services.py")
    assert results[0].constraints == []


def test_scope_for_excludes_inactive_constraint(python_simple_repo: Path) -> None:
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

    results = scope_for(layout, "app/services.py")
    assert results[0].constraints == []


def test_scope_for_includes_scoped_note(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    note_add(
        layout, category=NoteCategory.pitfall, content="watch the retry loop",
        why_persist="bit us once", scopes=["app"],
    )
    run_update(layout, full=True)

    results = scope_for(layout, "app/services.py")
    assert len(results[0].notes) == 1
    assert results[0].notes[0].content == "watch the retry loop"


def test_scope_for_file_in_multiple_scopes(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
            Scope(id="core", name="Core", source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    run_update(layout, full=True)

    results = scope_for(layout, "app/services.py")
    assert sorted(s.scope_id for s in results) == ["app", "core"]


def test_scope_for_matches_a_symbol_only_scope(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    import sqlite3

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
    run_update(layout, full=True)

    assert [scope.scope_id for scope in scope_for(layout, "app/services.py")] == ["service"]


def test_resolve_scope_for_by_path(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", source=ScopeSource.human, members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    run_update(layout, full=True)

    resolved_path, results = resolve_scope_for(layout, path="app/services.py")
    assert resolved_path == "app/services.py"
    assert [s.scope_id for s in results] == ["app"]


def test_resolve_scope_for_by_symbol(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", source=ScopeSource.human, members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    run_update(layout, full=True)

    resolved_path, results = resolve_scope_for(layout, symbol="UserService")
    assert resolved_path == "app/services.py"
    assert [s.scope_id for s in results] == ["app"]


def test_resolve_scope_for_unresolvable_symbol_returns_empty(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    resolved_path, results = resolve_scope_for(layout, symbol="NoSuchSymbolAtAll")
    assert resolved_path is None
    assert results == []
