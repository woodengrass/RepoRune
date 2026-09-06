from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from rune.core.project import init_project
from rune.core.storage.canonical import append_jsonl, write_json_model
from rune.core.storage.models import (
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
from rune.core.storage.sqlite.materialize import CanonicalConflictError, rebuild_cache


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

    rebuild_cache(layout)

    conn = sqlite3.connect(str(layout.memory_db))
    rows = conn.execute(
        "SELECT scope_id FROM scope_files WHERE file = ? ORDER BY scope_id", ("src/shared.py",)
    ).fetchall()
    assert [r[0] for r in rows] == ["api", "auth"]


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
