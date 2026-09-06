"""canonical -> SQLite materialization. Never calls an LLM (spec §44).

`rebuild_cache` performs a full rebuild: delete memory.db, recreate schema,
and repopulate every table from the canonical files in one transaction that
commits atomically at the end (ARCHITECTURE.md §4.7) — so a reader never
observes a half-materialized database.

Milestone 1 scope: code index tables (files/symbols/edges) are created but
stay empty; they are populated starting Milestone 2. Everything else
(scopes, decisions, constraints, notes, proposals, semantic summaries) is
fully materialized here since the canonical writers already exist.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from importlib import resources
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from rune.core.project import RuneLayout
from rune.core.storage.canonical import read_json_model, read_jsonl
from rune.core.storage.models import (
    MemoryRevision,
    Note,
    Proposal,
    RecordType,
    ScopesFile,
    ScopeSummary,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class CanonicalConflictError(Exception):
    """Raised when the same (id, revision) pair appears twice in a
    canonical JSONL file. Per DATA_MODEL.md §1/§8, V1 assumes a single
    writer and refuses to silently pick one of the conflicting lines.
    """


def _schema_sql_text() -> str:
    return (
        resources.files("rune.core.storage.sqlite")
        .joinpath("schema.sql")
        .read_text(encoding="utf-8")
    )


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_schema_sql_text())


def _group_current_by_id(
    records: list[MemoryRevision] | list[Note] | list[Proposal],
    id_field: str,
    label: str,
) -> dict[str, list]:
    """Groups revisions by their logical id, detects duplicate (id, revision)
    pairs, and returns {id: [revisions sorted by revision ascending]}.
    """
    grouped: dict[str, dict[int, object]] = defaultdict(dict)
    for record in records:
        rid = getattr(record, id_field)
        revision = record.revision
        if revision in grouped[rid]:
            raise CanonicalConflictError(
                f"{label}: duplicate ({id_field}={rid!r}, revision={revision}) "
                f"found in canonical JSONL — refusing to silently pick one. "
                f"Resolve manually (edit the file to remove/renumber one line)."
            )
        grouped[rid][revision] = record
    return {
        rid: [revisions_by_num[r] for r in sorted(revisions_by_num)]
        for rid, revisions_by_num in grouped.items()
    }


def _materialize_scopes(conn: sqlite3.Connection, scopes_file: ScopesFile) -> None:
    for scope in scopes_file.scopes:
        conn.execute(
            "INSERT INTO scopes (id, name, description, locked, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope.id, scope.name, scope.description, int(scope.locked), scope.source.value),
        )
        for file_path in scope.members.files:
            conn.execute(
                "INSERT OR IGNORE INTO scope_files (scope_id, file) VALUES (?, ?)",
                (scope.id, file_path),
            )
        for symbol_id in scope.members.symbols:
            conn.execute(
                "INSERT OR IGNORE INTO scope_symbols (scope_id, symbol_id) VALUES (?, ?)",
                (scope.id, symbol_id),
            )


def _materialize_decisions_or_constraints(
    conn: sqlite3.Connection,
    revisions: list[MemoryRevision],
    record_type: RecordType,
) -> None:
    table_prefix = record_type.value  # "decision" | "constraint"
    grouped = _group_current_by_id(revisions, "record_id", f"{table_prefix}s.jsonl")
    for record_id, revs in grouped.items():
        current = revs[-1]
        conn.execute(
            f"INSERT INTO {table_prefix}_records (record_id, current_revision) "
            f"VALUES (?, ?)",
            (record_id, current.revision),
        )
        for rev in revs:
            if record_type is RecordType.decision:
                conn.execute(
                    "INSERT INTO decision_revisions "
                    "(record_id, revision, status, content, rationale, critical, "
                    " source_document, source_section, created_by, approved_by, "
                    " created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rev.record_id, rev.revision, rev.status.value, rev.content,
                        rev.rationale, int(rev.critical), rev.source_document,
                        rev.source_section, rev.created_by.value, rev.approved_by,
                        rev.created_at,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO constraint_revisions "
                    "(record_id, revision, status, content, rationale, severity, "
                    " persistence_mode, source_hashes_json, scope_hashes_json, "
                    " expires_at, source_document, source_section, "
                    " machine_check_hint, created_by, approved_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rev.record_id, rev.revision, rev.status.value, rev.content,
                        rev.rationale,
                        rev.severity.value if rev.severity else None,
                        rev.persistence_mode.value if rev.persistence_mode else None,
                        json.dumps(rev.source_hashes),
                        json.dumps(rev.scope_hashes),
                        rev.expires_at, rev.source_document, rev.source_section,
                        rev.machine_check_hint,
                        rev.created_by.value, rev.approved_by, rev.created_at,
                    ),
                )
            for scope_id in rev.scopes:
                conn.execute(
                    f"INSERT INTO {table_prefix}_scopes (record_id, revision, scope_id) "
                    f"VALUES (?, ?, ?)",
                    (rev.record_id, rev.revision, scope_id),
                )
            for file_path in rev.files:
                conn.execute(
                    f"INSERT INTO {table_prefix}_files (record_id, revision, file) "
                    f"VALUES (?, ?, ?)",
                    (rev.record_id, rev.revision, file_path),
                )
            for symbol_id in rev.symbols:
                conn.execute(
                    f"INSERT INTO {table_prefix}_symbols (record_id, revision, symbol_id) "
                    f"VALUES (?, ?, ?)",
                    (rev.record_id, rev.revision, symbol_id),
                )


def _materialize_notes(conn: sqlite3.Connection, notes: list[Note]) -> None:
    grouped = _group_current_by_id(notes, "id", "notes.jsonl")
    for note_id, revs in grouped.items():
        current = revs[-1]
        conn.execute(
            "INSERT INTO note_records (id, current_revision) VALUES (?, ?)",
            (note_id, current.revision),
        )
        for rev in revs:
            conn.execute(
                "INSERT INTO note_revisions "
                "(id, revision, category, content, why_persist, importance, "
                " confidence, source, created_at, last_verified_at, expires_at, "
                " source_hashes_json, evidence_json, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rev.id, rev.revision, rev.category.value, rev.content,
                    rev.why_persist, rev.importance, rev.confidence,
                    rev.source.value, rev.created_at, rev.last_verified_at,
                    rev.expires_at, json.dumps(rev.source_hashes),
                    json.dumps(rev.evidence), rev.status.value,
                ),
            )
            for scope_id in rev.scopes:
                conn.execute(
                    "INSERT INTO note_scopes (id, revision, scope_id) VALUES (?, ?, ?)",
                    (rev.id, rev.revision, scope_id),
                )
            for file_path in rev.files:
                conn.execute(
                    "INSERT INTO note_files (id, revision, file) VALUES (?, ?, ?)",
                    (rev.id, rev.revision, file_path),
                )
            for symbol_id in rev.symbols:
                conn.execute(
                    "INSERT INTO note_symbols (id, revision, symbol_id) VALUES (?, ?, ?)",
                    (rev.id, rev.revision, symbol_id),
                )


def _materialize_proposals(conn: sqlite3.Connection, proposals: list[Proposal]) -> None:
    grouped = _group_current_by_id(proposals, "proposal_id", "proposals.jsonl")
    for proposal_id, revs in grouped.items():
        current = revs[-1]
        conn.execute(
            "INSERT INTO pending_proposals "
            "(proposal_id, current_revision, type, record_id, payload_json, "
            " status, created_by, created_at, resolved_at, resolved_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                proposal_id, current.revision, current.type.value,
                current.record_id, current.payload.model_dump_json(),
                current.status.value, current.created_by, current.created_at,
                current.resolved_at, current.resolved_by,
            ),
        )


def _materialize_semantic(conn: sqlite3.Connection, summaries: list[ScopeSummary]) -> None:
    # Last line per scope_id wins (append order = generation order); this is
    # not the Decision/Constraint/Note revision mechanism (ScopeSummary has
    # no `revision` field — see DATA_MODEL.md §2.4).
    latest: dict[str, ScopeSummary] = {}
    for summary in summaries:
        latest[summary.scope_id] = summary
    for scope_id, summary in latest.items():
        conn.execute(
            "INSERT INTO semantic_objects "
            "(scope_id, purpose, payload_json, generated_at, model, source_hash, "
            " status, last_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope_id, summary.purpose, summary.model_dump_json(),
                summary.generated_at, summary.model, summary.source_hash,
                summary.status.value, summary.last_error,
            ),
        )


def rebuild_cache(layout: RuneLayout) -> dict[str, int]:
    """Fully rebuilds memory.db from canonical files. Zero LLM calls, zero
    network. Returns a small stats dict for `rune rebuild-cache` output.

    Builds into a temporary sibling file and only replaces the real
    memory.db on success (os.replace). If canonical parsing or
    materialization fails partway (e.g. a CanonicalConflictError), the
    previous memory.db — which may still be a perfectly good, working
    cache — is left untouched. Deleting the old cache before confirming the
    new one is valid would turn "canonical has a conflict" into "you also
    lost your working cache," which defeats the point of memory.db being a
    safely-rebuildable derived artifact.
    """
    tmp_db = layout.memory_db.with_name(layout.memory_db.name + ".rebuilding.tmp")
    for suffix in ("", "-wal", "-shm"):
        stale_tmp = tmp_db.with_name(tmp_db.name + suffix)
        if stale_tmp.exists():
            stale_tmp.unlink()

    conn = connect(tmp_db)
    try:
        create_schema(conn)

        scopes_file = read_json_model(layout.scopes_json, ScopesFile) or ScopesFile()
        decisions = read_jsonl(layout.decisions_jsonl, MemoryRevision)
        constraints = read_jsonl(layout.constraints_jsonl, MemoryRevision)
        notes = read_jsonl(layout.notes_jsonl, Note)
        proposals = read_jsonl(layout.proposals_jsonl, Proposal)
        semantic = read_jsonl(layout.semantic_jsonl, ScopeSummary)

        conn.execute("BEGIN;")
        _materialize_scopes(conn, scopes_file)
        _materialize_decisions_or_constraints(conn, decisions, RecordType.decision)
        _materialize_decisions_or_constraints(conn, constraints, RecordType.constraint)
        _materialize_notes(conn, notes)
        _materialize_proposals(conn, proposals)
        _materialize_semantic(conn, semantic)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '1')"
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except BaseException:
        conn.rollback()
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            stale_tmp = tmp_db.with_name(tmp_db.name + suffix)
            if stale_tmp.exists():
                stale_tmp.unlink()
        raise
    else:
        conn.close()

    for suffix in ("", "-wal", "-shm"):
        target = layout.memory_db.with_name(layout.memory_db.name + suffix)
        source = tmp_db.with_name(tmp_db.name + suffix)
        if source.exists():
            source.replace(target)
        elif target.exists():
            # tmp build produced no -wal/-shm (checkpointed away) but a
            # stale one from a previous run may still sit next to the old db
            target.unlink()

    return {
        "scopes": len(scopes_file.scopes),
        "decisions": len(decisions),
        "constraints": len(constraints),
        "notes": len(notes),
        "proposals": len(proposals),
        "semantic_summaries": len(semantic),
    }
