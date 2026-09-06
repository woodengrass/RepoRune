from __future__ import annotations

from pathlib import Path

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
