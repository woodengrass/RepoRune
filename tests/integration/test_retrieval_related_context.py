from __future__ import annotations

from pathlib import Path

import pytest

from rune.core.memory.proposals import approve, propose
from rune.core.project import init_project
from rune.core.retrieval.related_context import (
    RelatedContextValidationError,
    related_context,
)
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


def test_related_context_requires_at_least_one_selector(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    with pytest.raises(RelatedContextValidationError):
        related_context(layout)


def test_related_context_by_path_aggregates_scope_constraint_and_decision(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", source=ScopeSource.human, members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    c = propose(
        layout, type=RecordType.constraint, record_id="c1", content="no bare except",
        severity=Severity.must, persistence_mode=PersistenceMode.persistent, scopes=["app"],
    )
    approve(layout, c.proposal_id, resolved_by="alice")
    d = propose(layout, type=RecordType.decision, record_id="d1", content="use X", scopes=["app"])
    approve(layout, d.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    result = related_context(layout, path="app/services.py")
    assert [s.scope_id for s in result.scopes] == ["app"]
    assert [c.record_id for c in result.constraints] == ["c1"]
    assert [d.record_id for d in result.decisions] == ["d1"]


def test_related_context_by_query_finds_approved_decision(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    d = propose(layout, type=RecordType.decision, record_id="d1", content="use postgres for storage")
    approve(layout, d.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    result = related_context(layout, query="postgres")
    assert [dec.record_id for dec in result.decisions] == ["d1"]


def test_related_context_include_restricts_buckets(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", source=ScopeSource.human, members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    run_update(layout, full=True)

    result = related_context(layout, path="app/services.py", include={"constraints"})
    assert result.scopes == []
    assert result.constraints == []  # none proposed, but bucket was requested
    assert result.notes == []
    assert result.symbols == []


def test_related_context_max_items_caps_each_bucket(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    result = related_context(layout, path="app/services.py", max_items=1)
    assert len(result.symbols) <= 1


@pytest.mark.parametrize("max_items", [-1, -10])
def test_related_context_rejects_negative_max_items(python_simple_repo: Path, max_items: int) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    with pytest.raises(RelatedContextValidationError, match="max_items"):
        related_context(layout, path="app/services.py", max_items=max_items)


def test_related_context_rejects_unknown_include_bucket(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    with pytest.raises(RelatedContextValidationError, match="unknown include"):
        related_context(layout, path="app/services.py", include={"unknown"})


def test_related_context_allows_zero_max_items(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    result = related_context(layout, path="app/services.py", max_items=0)
    assert result.scopes == []
    assert result.constraints == []
    assert result.decisions == []
    assert result.notes == []
    assert result.semantic == []
    assert result.symbols == []


def test_related_context_by_query_reports_real_constraint_severity(python_simple_repo: Path) -> None:
    """A review pass caught this by hand: an earlier version guessed a
    query-derived constraint's severity from its `search()` rank instead
    of looking up the real record. This constraint reaches `query=` via
    `_absorb_search_result`, not `scope_for`, so it exercises the fixed
    lookup-based path.
    """
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", source=ScopeSource.human, members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    c = propose(
        layout, type=RecordType.constraint, record_id="c1", content="no bare except anywhere",
        severity=Severity.must, persistence_mode=PersistenceMode.persistent, scopes=["app"],
    )
    approve(layout, c.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    result = related_context(layout, query="bare except")
    assert len(result.constraints) == 1
    assert result.constraints[0].severity == "MUST"


def test_related_context_by_query_excludes_info_severity_constraint(python_simple_repo: Path) -> None:
    """The query path must apply the same MUST/SHOULD-only proactive-
    injection filter `scope_for`'s path/symbol paths already apply
    (`scope_for.INJECTED_CONSTRAINT_SEVERITIES`) -- an INFO constraint
    that happens to text-match a query must not appear in this bucket,
    the same way it never appears via `path=`/`symbol=`.
    """
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    c = propose(
        layout, type=RecordType.constraint, record_id="c1", content="informational only, fyi note",
        severity=Severity.info, persistence_mode=PersistenceMode.persistent,
    )
    approve(layout, c.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    result = related_context(layout, query="informational")
    assert result.constraints == []
