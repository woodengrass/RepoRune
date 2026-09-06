from __future__ import annotations

import sqlite3
from pathlib import Path

from rune.core.memory.notes import note_add
from rune.core.memory.proposals import approve, propose
from rune.core.project import init_project
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


def test_deleting_a_scope_orphans_its_decision_end_to_end(python_simple_repo: Path) -> None:
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
        layout, type=RecordType.decision, record_id="d1", content="c", scopes=["app"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")

    # scope "app" disappears
    write_json_model(layout.scopes_json, ScopesFile(scopes=[]))
    stats = run_update(layout, full=False)
    assert stats["decisions_transitioned"] == 1

    conn = sqlite3.connect(str(layout.memory_db))
    row = conn.execute(
        "SELECT r.current_revision, v.status FROM decision_records r "
        "JOIN decision_revisions v ON v.record_id = r.record_id AND v.revision = r.current_revision "
        "WHERE r.record_id = 'd1'"
    ).fetchone()
    assert row == (2, "orphaned")


def test_deleting_a_referenced_file_marks_decision_review_required(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    proposal = propose(
        layout, type=RecordType.decision, record_id="d1", content="c",
        files=["app/services.py"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")

    (python_simple_repo / "app" / "services.py").unlink()
    stats = run_update(layout, full=False)
    assert stats["decisions_transitioned"] == 1

    conn = sqlite3.connect(str(layout.memory_db))
    row = conn.execute(
        "SELECT r.current_revision, v.status FROM decision_records r "
        "JOIN decision_revisions v ON v.record_id = r.record_id AND v.revision = r.current_revision "
        "WHERE r.record_id = 'd1'"
    ).fetchone()
    assert row == (2, "review_required")


def test_modifying_a_referenced_file_does_not_move_the_decision(python_simple_repo: Path) -> None:
    """ARCHITECTURE.md §4.6: content changing must never trigger a
    Decision transition -- confirmed end-to-end through `rune update`,
    not just at the pure-function level.
    """
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    proposal = propose(
        layout, type=RecordType.decision, record_id="d1", content="c",
        files=["app/services.py"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")

    services = python_simple_repo / "app" / "services.py"
    services.write_text(services.read_text(encoding="utf-8") + "\n# a comment\n", encoding="utf-8")
    stats = run_update(layout, full=False)
    assert stats["decisions_transitioned"] == 0


def test_source_bound_constraint_content_change_marks_stale(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="no bare except",
        severity=Severity.must, persistence_mode=PersistenceMode.source_bound,
        files=["app/services.py"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")

    services = python_simple_repo / "app" / "services.py"
    services.write_text(services.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    stats = run_update(layout, full=False)
    assert stats["constraints_transitioned"] == 1

    conn = sqlite3.connect(str(layout.memory_db))
    row = conn.execute(
        "SELECT r.current_revision, v.status FROM constraint_records r "
        "JOIN constraint_revisions v ON v.record_id = r.record_id AND v.revision = r.current_revision "
        "WHERE r.record_id = 'c1'"
    ).fetchone()
    assert row == (2, "stale")


def test_note_orphans_when_its_scope_is_deleted(python_simple_repo: Path) -> None:
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
        layout, category=NoteCategory.observation, content="c", why_persist="w",
        scopes=["app"],
    )

    write_json_model(layout.scopes_json, ScopesFile(scopes=[]))
    stats = run_update(layout, full=False)
    assert stats["notes_transitioned"] == 1

    conn = sqlite3.connect(str(layout.memory_db))
    row = conn.execute(
        "SELECT r.current_revision, v.status FROM note_records r "
        "JOIN note_revisions v ON v.id = r.id AND v.revision = r.current_revision"
    ).fetchone()
    assert row == (2, "orphaned")


def test_full_rebuild_cache_also_runs_staleness_detection(python_simple_repo: Path) -> None:
    """Structural staleness has no LLM cost, so unlike the semantic
    refresh it must also run on `rune rebuild-cache` (full=True), not
    only on incremental `rune update`.
    """
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    proposal = propose(
        layout, type=RecordType.decision, record_id="d1", content="c",
        files=["app/services.py"],
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")

    (python_simple_repo / "app" / "services.py").unlink()
    stats = run_update(layout, full=True)
    assert stats["decisions_transitioned"] == 1
