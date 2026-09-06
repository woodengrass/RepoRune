from __future__ import annotations

from pathlib import Path

from rune.core.memory.notes import note_add, note_update
from rune.core.memory.proposals import approve, deactivate, propose
from rune.core.project import init_project
from rune.core.retrieval.search import (
    RANK_ACTIVE_DECISION,
    RANK_FRESH_NOTE,
    RANK_GLOBAL_MUST,
    RANK_SCOPED_MUST,
    RANK_STALE_NOTE,
    search,
)
from rune.core.storage.canonical import write_json_model
from rune.core.storage.models import (
    NoteCategory,
    NoteStatus,
    PersistenceMode,
    RecordType,
    Scope,
    ScopesFile,
    ScopeSource,
    Severity,
)
from rune.core.update import run_update


def test_search_missing_cache_returns_empty(git_repo: Path) -> None:
    layout = init_project(git_repo)
    assert search(layout, "anything") == []


def test_search_finds_active_decision(git_repo: Path) -> None:
    layout = init_project(git_repo)
    proposal = propose(layout, type=RecordType.decision, record_id="d1", content="Use PostgreSQL")
    approve(layout, proposal.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    results = search(layout, "postgresql")
    assert len(results) == 1
    assert results[0].kind == "decision"
    assert results[0].rank == RANK_ACTIVE_DECISION
    assert results[0].id == "d1"


def test_search_excludes_inactive_decision_by_default(git_repo: Path) -> None:
    layout = init_project(git_repo)
    proposal = propose(layout, type=RecordType.decision, record_id="d1", content="Use Redis")
    approve(layout, proposal.proposal_id, resolved_by="alice")
    deactivate(layout, RecordType.decision, "d1", by="bob")
    run_update(layout, full=True)

    assert search(layout, "redis") == []
    history_results = search(layout, "redis", history=True)
    assert len(history_results) == 1
    assert history_results[0].status == "inactive"


def test_search_ranks_global_must_before_scoped_must(git_repo: Path) -> None:
    layout = init_project(git_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[Scope(id="app", name="App", source=ScopeSource.human)]),
    )
    global_p = propose(
        layout, type=RecordType.constraint, record_id="global-1",
        content="always run tests before commit", severity=Severity.must,
        persistence_mode=PersistenceMode.persistent,
    )
    approve(layout, global_p.proposal_id, resolved_by="alice")
    scoped_p = propose(
        layout, type=RecordType.constraint, record_id="scoped-1",
        content="always run tests in app scope", severity=Severity.must,
        persistence_mode=PersistenceMode.persistent, scopes=["app"],
    )
    approve(layout, scoped_p.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    results = search(layout, "tests")
    assert [r.id for r in results] == ["global-1", "scoped-1"]
    assert results[0].rank == RANK_GLOBAL_MUST
    assert results[1].rank == RANK_SCOPED_MUST


def test_search_finds_fresh_note_and_ranks_stale_lower(git_repo: Path) -> None:
    layout = init_project(git_repo)
    note = note_add(
        layout, category=NoteCategory.pitfall, content="watch the retry loop timing",
        why_persist="bit us once",
    )
    run_update(layout, full=True)
    results = search(layout, "retry")
    assert len(results) == 1
    assert results[0].rank == RANK_FRESH_NOTE
    assert results[0].warning is None

    note_update(layout, note.id, status=NoteStatus.stale)
    run_update(layout, full=True)
    results = search(layout, "retry")
    assert len(results) == 1
    assert results[0].rank == RANK_STALE_NOTE
    assert results[0].warning == "[STALE]"


def test_search_excludes_expired_note_even_with_history(git_repo: Path) -> None:
    """`expired` is excluded from default retrieval AND is one of the
    statuses `check_semantic_health`-style terminal states cover in
    history mode too, per DATA_MODEL.md §6 -- confirm it still shows in
    history mode (only `archived`/`expired`/`orphaned` are non-visible).
    """
    layout = init_project(git_repo)
    note = note_add(layout, category=NoteCategory.pitfall, content="c", why_persist="w")
    note_update(layout, note.id, status=NoteStatus.expired)
    run_update(layout, full=True)

    assert search(layout, "c") == []
    assert len(search(layout, "c", history=True)) == 1
