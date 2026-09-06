from __future__ import annotations

from pathlib import Path

from rune.core.memory.proposals import approve, deactivate, propose
from rune.core.project import init_project
from rune.core.retrieval.context import build_hard_bootstrap, build_soft_bootstrap
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


def test_hard_bootstrap_missing_cache_returns_empty(git_repo: Path) -> None:
    layout = init_project(git_repo)
    hard = build_hard_bootstrap(layout)
    assert hard.constraints == []
    assert hard.decisions == []
    assert hard.estimated_tokens == 0
    assert hard.overflow is False


def test_hard_bootstrap_includes_global_must_constraint(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="no bare except",
        severity=Severity.must, persistence_mode=PersistenceMode.persistent,
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    hard = build_hard_bootstrap(layout)
    assert [c.record_id for c in hard.constraints] == ["c1"]
    assert hard.estimated_tokens > 0
    assert hard.overflow is False


def test_hard_bootstrap_excludes_scoped_must_constraint(python_simple_repo: Path) -> None:
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
        layout, type=RecordType.constraint, record_id="c1", content="scoped rule",
        severity=Severity.must, persistence_mode=PersistenceMode.persistent, scopes=["app"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    hard = build_hard_bootstrap(layout)
    assert hard.constraints == []


def test_hard_bootstrap_excludes_should_constraint(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="preference",
        severity=Severity.should, persistence_mode=PersistenceMode.persistent,
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    hard = build_hard_bootstrap(layout)
    assert hard.constraints == []


def test_hard_bootstrap_excludes_inactive_constraint(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="rule",
        severity=Severity.must, persistence_mode=PersistenceMode.persistent,
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    deactivate(layout, RecordType.constraint, "c1", by="bob")
    run_update(layout, full=True)

    hard = build_hard_bootstrap(layout)
    assert hard.constraints == []


def test_hard_bootstrap_includes_critical_global_decision(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    proposal = propose(
        layout, type=RecordType.decision, record_id="d1", content="use PostgreSQL",
        critical=True,
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    hard = build_hard_bootstrap(layout)
    assert [d.record_id for d in hard.decisions] == ["d1"]


def test_hard_bootstrap_excludes_non_critical_decision(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    proposal = propose(
        layout, type=RecordType.decision, record_id="d1", content="minor call",
        critical=False,
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    hard = build_hard_bootstrap(layout)
    assert hard.decisions == []


def test_hard_bootstrap_excludes_scoped_critical_decision(python_simple_repo: Path) -> None:
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
        layout, type=RecordType.decision, record_id="d1", content="scoped decision",
        critical=True, scopes=["app"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    hard = build_hard_bootstrap(layout)
    assert hard.decisions == []


def test_hard_bootstrap_overflow_flagged_never_drops_constraints(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    for i in range(5):
        proposal = propose(
            layout, type=RecordType.constraint, record_id=f"c{i}",
            content="x" * 3000, severity=Severity.must,
            persistence_mode=PersistenceMode.persistent,
        )
        approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    hard = build_hard_bootstrap(layout)
    assert len(hard.constraints) == 5
    assert hard.overflow is True
    assert hard.estimated_tokens > hard.budget_tokens


def test_soft_bootstrap_missing_cache_returns_project_status_only(git_repo: Path) -> None:
    layout = init_project(git_repo)
    soft = build_soft_bootstrap(layout)
    assert soft.project_name is not None
    assert soft.scopes == []
    assert soft.decisions == []


def test_soft_bootstrap_includes_scope_summary_and_non_critical_decision(python_simple_repo: Path) -> None:
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
        layout, type=RecordType.decision, record_id="d1", content="minor call", critical=False,
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    soft = build_soft_bootstrap(layout)
    assert soft.working_tree_fresh is True
    assert soft.files_indexed > 0
    scope_ids = [s.scope_id for s in soft.scopes]
    assert "app" in scope_ids
    assert [d.record_id for d in soft.decisions] == ["d1"]


def test_soft_bootstrap_excludes_critical_decision(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    proposal = propose(
        layout, type=RecordType.decision, record_id="d1", content="use PostgreSQL", critical=True,
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    soft = build_soft_bootstrap(layout)
    assert soft.decisions == []
