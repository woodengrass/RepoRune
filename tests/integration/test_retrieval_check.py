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
