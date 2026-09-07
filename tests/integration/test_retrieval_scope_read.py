from __future__ import annotations

from pathlib import Path

import pytest

from rune.core.memory.proposals import approve, propose
from rune.core.project import init_project
from rune.core.retrieval.scope_read import ScopeNotFoundError, scope_read
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


def test_scope_read_missing_scope_raises(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    with pytest.raises(ScopeNotFoundError):
        scope_read(layout, "nope")


def test_scope_read_returns_metadata_members_and_constraint(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", description="the app module", locked=True, source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="no bare except",
        severity=Severity.must, persistence_mode=PersistenceMode.persistent, scopes=["app"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    result = scope_read(layout, "app")
    assert result.scope_id == "app"
    assert result.name == "App"
    assert result.description == "the app module"
    assert result.locked is True
    assert result.files == ["app/services.py"]
    assert [c.record_id for c in result.constraints] == ["c1"]


def test_scope_read_includes_scoped_decision(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    proposal = propose(layout, type=RecordType.decision, record_id="d1", content="use X", scopes=["app"])
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    result = scope_read(layout, "app")
    assert [d.record_id for d in result.decisions] == ["d1"]
