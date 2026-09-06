from __future__ import annotations

from pathlib import Path

import pytest

from rune.core.memory.notes import note_add, note_update
from rune.core.memory.proposals import approve, deactivate, propose
from rune.core.project import init_project
from rune.core.retrieval.search import (
    RANK_ACTIVE_DECISION,
    RANK_FRESH_NOTE,
    RANK_GLOBAL_MUST,
    RANK_HISTORICAL,
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
    # Every revision is indexed (DATA_MODEL.md §3: history mode can see
    # the full revision history), so both rev1 (superseded by the
    # deactivation) and rev2 (current, inactive -- deactivate() carries
    # content forward unchanged, so it still says "Use Redis" too) match.
    assert len(history_results) == 2
    by_revision = {r.revision: r for r in history_results}
    assert by_revision[1].warning == "superseded"
    assert by_revision[2].status == "inactive"
    assert by_revision[2].warning is None


def test_search_history_finds_superseded_revision_content(git_repo: Path) -> None:
    """The exact scenario a relayed review confirmed by hand: a Decision
    whose rev1 said "use redis" gets replaced by rev2 saying "use
    postgres" (still active). Searching "redis" must find nothing by
    default (rev1 is superseded, not current) but the superseded text
    itself must be reachable in history mode -- before this fix, FTS only
    ever indexed the current revision's text, so "redis" was never in the
    index at all, not even in history mode.
    """
    layout = init_project(git_repo)
    p1 = propose(layout, type=RecordType.decision, record_id="d1", content="use redis")
    approve(layout, p1.proposal_id, resolved_by="alice")
    p2 = propose(layout, type=RecordType.decision, record_id="d1", content="use postgres")
    approve(layout, p2.proposal_id, resolved_by="alice")
    run_update(layout, full=True)

    assert search(layout, "redis") == []
    assert search(layout, "postgres")[0].status == "active"

    history_results = search(layout, "redis", history=True)
    assert len(history_results) == 1
    assert history_results[0].revision == 1
    assert history_results[0].warning == "superseded"
    assert history_results[0].rank == RANK_HISTORICAL
    assert history_results[0].text == "use redis"

    # the current revision's own text is unaffected by history mode
    assert search(layout, "postgres", history=True)[0].warning is None


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
    """`expired` is excluded from default retrieval, but its current
    revision is still found in history mode (only `archived`/`expired`/
    `orphaned` are non-visible, not un-searchable). Both the current
    `expired` revision and the superseded `active` revision that preceded
    it match here since `note_update` without `content=...` carries the
    text forward unchanged -- two hits is correct, not a duplicate bug.
    """
    layout = init_project(git_repo)
    note = note_add(layout, category=NoteCategory.pitfall, content="c", why_persist="w")
    note_update(layout, note.id, status=NoteStatus.expired)
    run_update(layout, full=True)

    assert search(layout, "c") == []
    history_results = search(layout, "c", history=True)
    assert len(history_results) == 2
    by_revision = {r.revision: r for r in history_results}
    assert by_revision[1].warning == "superseded"
    assert by_revision[2].status == "expired"


def test_search_raises_cache_unusable_error_on_corrupt_db(git_repo: Path) -> None:
    """A relayed review confirmed by hand: a 0-byte/corrupt memory.db used
    to surface a raw sqlite3.OperationalError traceback instead of an
    actionable message.
    """
    from rune.core.retrieval.search import CacheUnusableError

    layout = init_project(git_repo)
    layout.memory_db.parent.mkdir(parents=True, exist_ok=True)
    layout.memory_db.write_bytes(b"")

    with pytest.raises(CacheUnusableError):
        search(layout, "anything")
