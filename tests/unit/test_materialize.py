from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from rune.core.project import init_project
from rune.core.storage.canonical import append_jsonl, write_json_model
from rune.core.storage.models import (
    IndexedFile,
    IndexedFileStatus,
    MemoryRevision,
    Note,
    NoteCategory,
    NoteStatus,
    PersistenceMode,
    Proposal,
    ProposalStatus,
    RecordStatus,
    RecordType,
    RevisionAuthor,
    Scope,
    ScopeMembers,
    ScopesFile,
    ScopeSource,
    Severity,
)
from rune.core.storage.sqlite.materialize import (
    CanonicalConflictError,
    CodeIndexData,
    rebuild_cache,
)


def _decision(record_id: str, revision: int, status: RecordStatus) -> MemoryRevision:
    return MemoryRevision(
        record_id=record_id,
        revision=revision,
        type=RecordType.decision,
        status=status,
        content="Use PostgreSQL",
        created_by=RevisionAuthor.human,
        approved_by="alice",
        created_at=f"2026-01-0{revision}T00:00:00Z",
    )


def test_current_revision_is_max_regardless_of_status(git_repo: Path) -> None:
    """Regression test for the current/visible bug: rev1=active,
    rev2=inactive must select rev2 as current, not fall back to rev1.
    """
    layout = init_project(git_repo)
    append_jsonl(layout.decisions_jsonl, _decision("d1", 1, RecordStatus.active))
    append_jsonl(layout.decisions_jsonl, _decision("d1", 2, RecordStatus.inactive))

    rebuild_cache(layout)

    conn = sqlite3.connect(str(layout.memory_db))
    current_revision = conn.execute(
        "SELECT current_revision FROM decision_records WHERE record_id = ?", ("d1",)
    ).fetchone()[0]
    assert current_revision == 2

    status = conn.execute(
        "SELECT status FROM decision_revisions WHERE record_id = ? AND revision = ?",
        ("d1", current_revision),
    ).fetchone()[0]
    assert status == "inactive"


def test_current_revision_review_required_stays_current(git_repo: Path) -> None:
    layout = init_project(git_repo)
    append_jsonl(layout.decisions_jsonl, _decision("d1", 1, RecordStatus.active))
    append_jsonl(layout.decisions_jsonl, _decision("d1", 2, RecordStatus.review_required))

    rebuild_cache(layout)

    conn = sqlite3.connect(str(layout.memory_db))
    current_revision = conn.execute(
        "SELECT current_revision FROM decision_records WHERE record_id = ?", ("d1",)
    ).fetchone()[0]
    assert current_revision == 2


def test_duplicate_revision_raises_conflict_and_preserves_old_cache(git_repo: Path) -> None:
    layout = init_project(git_repo)
    append_jsonl(layout.decisions_jsonl, _decision("d1", 1, RecordStatus.active))
    rebuild_cache(layout)
    good_hash = hashlib.sha256(layout.memory_db.read_bytes()).hexdigest()

    # introduce a conflicting duplicate revision
    append_jsonl(layout.decisions_jsonl, _decision("d1", 1, RecordStatus.active))

    with pytest.raises(CanonicalConflictError):
        rebuild_cache(layout)

    # the previously-working cache must be untouched, not deleted/corrupted
    assert layout.memory_db.exists()
    assert hashlib.sha256(layout.memory_db.read_bytes()).hexdigest() == good_hash


def _indexed_file(path: str) -> IndexedFile:
    return IndexedFile(
        path=path, language="python", content_hash="sha256:x", size=1, mtime=0.0,
        git_blob_hash=None, indexed_at="2026-01-01T00:00:00Z", status=IndexedFileStatus.ok,
    )


def test_scope_membership_supports_multiple_scopes_per_file(git_repo: Path) -> None:
    layout = init_project(git_repo)
    scopes = ScopesFile(
        scopes=[
            Scope(
                id="auth", name="Auth", source=ScopeSource.human,
                members=ScopeMembers(files=["src/shared.py"]),
            ),
            Scope(
                id="api", name="API", source=ScopeSource.human,
                members=ScopeMembers(files=["src/shared.py"]),
            ),
        ]
    )
    write_json_model(layout.scopes_json, scopes)

    # scope_files.file now carries a real FK to files(path) (restored in
    # Milestone 2) -- the member file must actually be indexed.
    rebuild_cache(layout, code_index=CodeIndexData(files=[_indexed_file("src/shared.py")]))

    conn = sqlite3.connect(str(layout.memory_db))
    rows = conn.execute(
        "SELECT scope_id FROM scope_files WHERE file = ? ORDER BY scope_id", ("src/shared.py",)
    ).fetchall()
    assert [r[0] for r in rows] == ["api", "auth"]


def test_scope_membership_referencing_a_nonindexed_file_is_dropped_not_fatal(
    git_repo: Path,
) -> None:
    """A scope member that doesn't match any currently-indexed file (typo
    in scopes.json, or the file was deleted from the repo) must not abort
    materialize -- `scope_files.file` has a real FK to files(path), and
    `INSERT OR IGNORE` does not suppress FK violations in SQLite, so this
    only works because `_materialize_scopes` pre-filters. Confirmed by
    reproducing the crash before adding that filter.
    """
    layout = init_project(git_repo)
    scopes = ScopesFile(
        scopes=[
            Scope(
                id="auth", name="Auth", source=ScopeSource.human,
                members=ScopeMembers(files=["src/does_not_exist.py"]),
            ),
        ]
    )
    write_json_model(layout.scopes_json, scopes)

    stats = rebuild_cache(layout)  # no code_index -> files table stays empty

    conn = sqlite3.connect(str(layout.memory_db))
    assert conn.execute("SELECT COUNT(*) FROM scope_files").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM scopes").fetchone()[0] == 1
    assert stats["scopes"] == 1


def test_note_current_revision_is_max(git_repo: Path) -> None:
    layout = init_project(git_repo)
    note_v1 = Note(
        id="n1", revision=1, category=NoteCategory.pitfall, content="bug in package X",
        why_persist="recurring", source=RevisionAuthor.agent,
        created_at="2026-01-01T00:00:00Z", last_verified_at="2026-01-01T00:00:00Z",
        status=NoteStatus.active,
    )
    note_v2 = note_v1.model_copy(update={
        "revision": 2, "status": NoteStatus.archived,
        "content": "bug in package X (fixed upstream, verified 2026-09)",
        "source": RevisionAuthor.agent,
    })
    append_jsonl(layout.notes_jsonl, note_v1)
    append_jsonl(layout.notes_jsonl, note_v2)

    rebuild_cache(layout)

    conn = sqlite3.connect(str(layout.memory_db))
    current_revision = conn.execute(
        "SELECT current_revision FROM note_records WHERE id = ?", ("n1",)
    ).fetchone()[0]
    assert current_revision == 2
    status = conn.execute(
        "SELECT status FROM note_revisions WHERE id = ? AND revision = ?", ("n1", 2)
    ).fetchone()[0]
    assert status == "archived"


def test_rebuild_cache_is_idempotent_given_same_canonical_state(git_repo: Path) -> None:
    """The rebuild-cache equivalence guarantee: rebuilding twice from the
    same canonical state must produce the same logical content (row counts
    per table), since memory.db is defined as a pure projection.
    """
    layout = init_project(git_repo)
    append_jsonl(layout.decisions_jsonl, _decision("d1", 1, RecordStatus.active))

    stats_first = rebuild_cache(layout)
    stats_second = rebuild_cache(layout)

    assert stats_first == stats_second


def test_decision_critical_and_source_fields_persist(git_repo: Path) -> None:
    """Global Code Standards / Hard Policy Injection support fields
    (ARCHITECTURE.md §7): a Decision marked `critical=True` with a
    CODE_STANDARDS.md traceability pointer must round-trip into SQLite.
    """
    layout = init_project(git_repo)
    decision = MemoryRevision(
        record_id="canonical-storage-model",
        revision=1,
        type=RecordType.decision,
        status=RecordStatus.active,
        content="Canonical text is authoritative; SQLite is derived.",
        critical=True,
        source_document="CODE_STANDARDS.md",
        source_section="Architecture Boundaries",
        created_by=RevisionAuthor.human,
        approved_by="alice",
        created_at="2026-01-01T00:00:00Z",
    )
    append_jsonl(layout.decisions_jsonl, decision)

    rebuild_cache(layout)

    conn = sqlite3.connect(str(layout.memory_db))
    row = conn.execute(
        "SELECT critical, source_document, source_section FROM decision_revisions "
        "WHERE record_id = ? AND revision = ?",
        ("canonical-storage-model", 1),
    ).fetchone()
    assert row == (1, "CODE_STANDARDS.md", "Architecture Boundaries")


def test_decision_critical_defaults_to_false(git_repo: Path) -> None:
    layout = init_project(git_repo)
    append_jsonl(layout.decisions_jsonl, _decision("d1", 1, RecordStatus.active))

    rebuild_cache(layout)

    conn = sqlite3.connect(str(layout.memory_db))
    critical = conn.execute(
        "SELECT critical FROM decision_revisions WHERE record_id = ? AND revision = ?",
        ("d1", 1),
    ).fetchone()[0]
    assert critical == 0


def test_global_must_constraint_persists_machine_check_hint(git_repo: Path) -> None:
    """A Global MUST Constraint (scopes=[]) with a machine-checkable rule
    should carry its `machine_check_hint` and source traceability through
    to SQLite untouched (ARCHITECTURE.md §7.2, §7.8).
    """
    layout = init_project(git_repo)
    constraint = MemoryRevision(
        record_id="no-business-logic-in-adapters",
        revision=1,
        type=RecordType.constraint,
        status=RecordStatus.active,
        content="Adapters must contain no business logic.",
        severity=Severity.must,
        persistence_mode=PersistenceMode.persistent,
        source_document="CODE_STANDARDS.md",
        source_section="Architecture Boundaries",
        machine_check_hint="ruff",
        created_by=RevisionAuthor.human,
        approved_by="alice",
        created_at="2026-01-01T00:00:00Z",
    )
    append_jsonl(layout.constraints_jsonl, constraint)

    rebuild_cache(layout)

    conn = sqlite3.connect(str(layout.memory_db))
    row = conn.execute(
        "SELECT severity, persistence_mode, source_document, source_section, "
        "machine_check_hint FROM constraint_revisions "
        "WHERE record_id = ? AND revision = ?",
        ("no-business-logic-in-adapters", 1),
    ).fetchone()
    assert row == ("MUST", "persistent", "CODE_STANDARDS.md", "Architecture Boundaries", "ruff")

    # global-ness is derived, not a stored flag: no rows in constraint_scopes
    scope_rows = conn.execute(
        "SELECT * FROM constraint_scopes WHERE record_id = ?",
        ("no-business-logic-in-adapters",),
    ).fetchall()
    assert scope_rows == []


def test_proposal_rebuild_cache_round_trip(git_repo: Path) -> None:
    """A pending proposal survives cache rebuild (DATA_MODEL.md §2.5a): the
    `pending_proposals` table is a derived cache of proposals.jsonl, so
    deleting memory.db must never lose track of something awaiting human
    approval.
    """
    layout = init_project(git_repo)
    payload = _decision("new-decision", 1, RecordStatus.active)
    proposal = Proposal(
        proposal_id="p1",
        revision=1,
        type=RecordType.decision,
        record_id="new-decision",
        payload=payload,
        status=ProposalStatus.pending,
        created_by="agent",
        created_at="2026-01-01T00:00:00Z",
    )
    append_jsonl(layout.proposals_jsonl, proposal)

    rebuild_cache(layout)

    conn = sqlite3.connect(str(layout.memory_db))
    row = conn.execute(
        "SELECT current_revision, type, record_id, status, created_by "
        "FROM pending_proposals WHERE proposal_id = ?",
        ("p1",),
    ).fetchone()
    assert row == (1, "decision", "new-decision", "pending", "agent")

    # simulate cache loss: rebuild again from scratch and confirm nothing
    # about the pending proposal was lost
    rebuild_cache(layout)
    conn2 = sqlite3.connect(str(layout.memory_db))
    row2 = conn2.execute(
        "SELECT current_revision, status FROM pending_proposals WHERE proposal_id = ?",
        ("p1",),
    ).fetchone()
    assert row2 == (1, "pending")


def test_proposal_approval_appends_revision_and_current_updates(git_repo: Path) -> None:
    layout = init_project(git_repo)
    payload = _decision("new-decision", 1, RecordStatus.active)
    pending = Proposal(
        proposal_id="p1", revision=1, type=RecordType.decision,
        record_id="new-decision", payload=payload, status=ProposalStatus.pending,
        created_by="agent", created_at="2026-01-01T00:00:00Z",
    )
    approved = pending.model_copy(update={
        "revision": 2,
        "status": ProposalStatus.approved,
        "resolved_at": "2026-01-02T00:00:00Z",
        "resolved_by": "alice",
    })
    append_jsonl(layout.proposals_jsonl, pending)
    append_jsonl(layout.proposals_jsonl, approved)

    rebuild_cache(layout)

    conn = sqlite3.connect(str(layout.memory_db))
    row = conn.execute(
        "SELECT current_revision, status, resolved_by FROM pending_proposals "
        "WHERE proposal_id = ?",
        ("p1",),
    ).fetchone()
    assert row == (2, "approved", "alice")


def test_reader_sees_consistent_snapshot_during_rebuild(git_repo: Path) -> None:
    """ARCHITECTURE.md §4.7: a reader must never observe a half-
    materialized database. This test opens a long-lived read transaction
    against memory.db, performs a rebuild_cache() that changes the data,
    and asserts the reader — as long as its transaction stays open — keeps
    seeing the pre-rebuild snapshot rather than a mix of old and new rows
    or a crash from the underlying file being swapped out from under it.
    """
    layout = init_project(git_repo)
    append_jsonl(layout.decisions_jsonl, _decision("d1", 1, RecordStatus.active))
    rebuild_cache(layout)

    reader = sqlite3.connect(str(layout.memory_db))
    reader.execute("BEGIN;")
    first_read = reader.execute(
        "SELECT current_revision FROM decision_records WHERE record_id = ?", ("d1",)
    ).fetchone()
    assert first_read == (1,)

    # writer: rebuild with a second decision added, while the reader's
    # transaction above is still open
    append_jsonl(layout.decisions_jsonl, _decision("d2", 1, RecordStatus.active))
    rebuild_cache(layout)

    # the reader's open transaction must still see its original snapshot:
    # d2 must not appear, and d1 must be unchanged
    second_read = reader.execute(
        "SELECT record_id FROM decision_records ORDER BY record_id"
    ).fetchall()
    reader.rollback()
    reader.close()

    assert second_read == [("d1",)], (
        "reader's snapshot must not observe the rebuild that happened "
        "while its read transaction was still open"
    )

    # a fresh connection opened after the rebuild must see the new state
    fresh = sqlite3.connect(str(layout.memory_db))
    fresh_read = fresh.execute(
        "SELECT record_id FROM decision_records ORDER BY record_id"
    ).fetchall()
    fresh.close()
    assert fresh_read == [("d1",), ("d2",)]


def test_rebuild_cache_self_heals_an_old_shape_memory_db(git_repo: Path) -> None:
    """Regression test: a memory.db built before Milestone 5 added
    `current_revision` to `semantic_objects` used to crash every
    subsequent `rebuild_cache` call with `OperationalError: no such
    column: current_revision` the moment there was any real semantic.jsonl
    content to materialize -- the only "fix" was manually deleting
    `.rune/cache/`. Reproduced by hand before this fix by hand-crafting an
    old-shape memory.db (schema_version=1, semantic_objects without the
    column) with a matching canonical scope + semantic summary, then
    calling rebuild_cache. memory.db is always fully derived from
    canonical (ARCHITECTURE.md invariant #1), so the fix makes
    rebuild_cache detect the schema_meta version mismatch and discard the
    incompatible file itself, rather than requiring a human to know to
    delete it.
    """
    from rune.core.storage.models import ScopeSummary

    layout = init_project(git_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.human,
                  members=ScopeMembers(files=["app.py"])),
        ]),
    )
    append_jsonl(
        layout.semantic_jsonl,
        ScopeSummary(
            scope_id="app", revision=1, purpose="old purpose",
            generated_at="2026-01-01T00:00:00Z", model="m", source_hash="sha256:x",
        ),
    )

    layout.memory_db.parent.mkdir(parents=True, exist_ok=True)
    old_conn = sqlite3.connect(str(layout.memory_db))
    old_conn.execute("PRAGMA foreign_keys=ON")
    old_conn.executescript(
        """
        CREATE TABLE files (path TEXT PRIMARY KEY, language TEXT, content_hash TEXT,
            size INTEGER, mtime REAL, git_blob_hash TEXT, indexed_at TEXT, status TEXT DEFAULT 'ok');
        CREATE TABLE symbols (symbol_id TEXT PRIMARY KEY, file TEXT, name TEXT,
            qualified_name TEXT, kind TEXT, signature TEXT, start_line INTEGER, end_line INTEGER);
        CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_symbol TEXT,
            source_file TEXT, target_symbol TEXT, target_file TEXT, edge_type TEXT, confidence REAL);
        CREATE TABLE scopes (id TEXT PRIMARY KEY, name TEXT, description TEXT DEFAULT '',
            locked INTEGER DEFAULT 0, source TEXT);
        CREATE TABLE scope_files (scope_id TEXT, file TEXT, PRIMARY KEY (scope_id, file));
        CREATE TABLE scope_symbols (scope_id TEXT, symbol_id TEXT, PRIMARY KEY (scope_id, symbol_id));
        CREATE TABLE semantic_objects (scope_id TEXT PRIMARY KEY REFERENCES scopes(id)
            ON DELETE CASCADE, purpose TEXT NOT NULL, payload_json TEXT NOT NULL,
            generated_at TEXT NOT NULL, model TEXT NOT NULL, source_hash TEXT NOT NULL,
            status TEXT NOT NULL, last_error TEXT);
        CREATE TABLE decision_records (record_id TEXT PRIMARY KEY, current_revision INTEGER);
        CREATE TABLE constraint_records (record_id TEXT PRIMARY KEY, current_revision INTEGER);
        CREATE TABLE note_records (id TEXT PRIMARY KEY, current_revision INTEGER);
        CREATE TABLE pending_proposals (proposal_id TEXT PRIMARY KEY, current_revision INTEGER,
            type TEXT, record_id TEXT, payload_json TEXT, status TEXT, created_by TEXT,
            created_at TEXT, resolved_at TEXT, resolved_by TEXT);
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    old_conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '1')")
    old_conn.commit()
    old_conn.close()

    stats = rebuild_cache(layout, code_index=CodeIndexData())  # must not raise

    assert stats["semantic_summaries"] == 1
    conn = sqlite3.connect(str(layout.memory_db))
    columns = [row[1] for row in conn.execute("PRAGMA table_info(semantic_objects)")]
    assert "current_revision" in columns
    row = conn.execute(
        "SELECT current_revision, purpose FROM semantic_objects WHERE scope_id = 'app'"
    ).fetchone()
    assert row == (1, "old purpose")
