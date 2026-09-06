"""canonical -> SQLite materialization. Never calls an LLM (spec §44).

`rebuild_cache` performs a full rebuild: clear every table, recreate
schema, and repopulate everything in one transaction that commits
atomically at the end (ARCHITECTURE.md §4.7) — so a reader never observes
a half-materialized database.

Milestone 1 scope: code index tables (files/symbols/edges) are created but
stay empty unless the caller passes `code_index` explicitly. Milestone 2's
`core.update` is the first caller to actually populate them — it re-derives
this data straight from source on every run (ARCHITECTURE.md invariant #2:
source code is the only source of truth for code facts), so unlike the
canonical-memory tables there is no JSONL/JSON file backing files/symbols/
edges. Everything else (scopes, decisions, constraints, notes, proposals,
semantic summaries) is fully materialized here since the canonical writers
already exist.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from rune.core.project import RuneLayout
from rune.core.storage.canonical import read_json_model, read_jsonl
from rune.core.storage.models import (
    Edge,
    EdgeType,
    IndexedFile,
    IndexedFileStatus,
    MemoryRevision,
    Note,
    Proposal,
    RecordType,
    ScopesFile,
    ScopeSummary,
    Symbol,
    SymbolKind,
)


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
    # SQLite ignores declared foreign keys unless this is turned on per
    # connection — without it, every `REFERENCES ... ON DELETE CASCADE` in
    # schema.sql is decorative only. `scope_files.file`/`scope_symbols.
    # symbol_id` deliberately do NOT declare a FK to files/symbols yet (see
    # schema.sql comment): those tables stay empty until Milestone 2, while
    # scopes.json can already carry file/symbol members in Milestone 1
    # (e.g. via --force-preserved content), so enforcing that particular FK
    # now would break legitimate M1 materialization.
    conn.execute("PRAGMA foreign_keys=ON;")
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


@dataclass(frozen=True)
class CodeIndexData:
    """The decisive-index tables' content for one materialize pass.
    Milestone 2's `core.update` builds this fresh on every run — files/
    symbols/edges have no canonical JSON/JSONL backing them (see module
    docstring), so there is nothing to "read from disk" the way
    scopes/decisions/etc. are.
    """

    files: list[IndexedFile] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


def _materialize_code_index(conn: sqlite3.Connection, code_index: CodeIndexData) -> None:
    for f in code_index.files:
        conn.execute(
            "INSERT INTO files "
            "(path, language, content_hash, size, mtime, git_blob_hash, indexed_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f.path, f.language, f.content_hash, f.size, f.mtime,
                f.git_blob_hash, f.indexed_at, f.status.value,
            ),
        )
    for s in code_index.symbols:
        conn.execute(
            "INSERT INTO symbols "
            "(symbol_id, file, name, qualified_name, kind, signature, start_line, end_line) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s.symbol_id, s.file, s.name, s.qualified_name, s.kind.value,
                s.signature, s.start_line, s.end_line,
            ),
        )
    for e in code_index.edges:
        conn.execute(
            "INSERT INTO edges "
            "(source_symbol, source_file, target_symbol, target_file, edge_type, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                e.source_symbol, e.source_file, e.target_symbol, e.target_file,
                e.edge_type.value, e.confidence,
            ),
        )


def read_current_code_index(layout: RuneLayout) -> CodeIndexData:
    """Reads the currently-materialized files/symbols/edges back out of
    memory.db. Used by `core.update` to know what was indexed last time
    (for change detection) and to reuse symbols/edges for files whose
    content hash hasn't changed, without re-parsing them. Returns an
    empty CodeIndexData if memory.db doesn't exist yet (first run).
    """
    if not layout.memory_db.exists():
        return CodeIndexData()
    conn = connect(layout.memory_db)
    try:
        files = [
            IndexedFile(
                path=row["path"],
                language=row["language"],
                content_hash=row["content_hash"],
                size=row["size"],
                mtime=row["mtime"],
                git_blob_hash=row["git_blob_hash"],
                indexed_at=row["indexed_at"],
                status=IndexedFileStatus(row["status"]),
            )
            for row in conn.execute("SELECT * FROM files")
        ]
        symbols = [
            Symbol(
                symbol_id=row["symbol_id"],
                file=row["file"],
                name=row["name"],
                qualified_name=row["qualified_name"],
                kind=SymbolKind(row["kind"]),
                signature=row["signature"],
                start_line=row["start_line"],
                end_line=row["end_line"],
            )
            for row in conn.execute("SELECT * FROM symbols")
        ]
        edges = [
            Edge(
                source_symbol=row["source_symbol"],
                source_file=row["source_file"],
                target_symbol=row["target_symbol"],
                target_file=row["target_file"],
                edge_type=EdgeType(row["edge_type"]),
                confidence=row["confidence"],
            )
            for row in conn.execute("SELECT * FROM edges")
        ]
        return CodeIndexData(files=files, symbols=symbols, edges=edges)
    finally:
        conn.close()


# Tables that own their family via ON DELETE CASCADE — clearing just these
# six also clears every dependent table (symbols/edges under files;
# scope_files/scope_symbols/semantic_objects under scopes; the *_scopes/
# *_files/*_symbols satellite tables under each decision/constraint/note
# revision). See schema.sql for the FK graph. pending_proposals has no
# parent, so it's cleared directly.
_ROOT_TABLES_TO_CLEAR = (
    "files",
    "scopes",
    "decision_records",
    "constraint_records",
    "note_records",
    "pending_proposals",
)


def _clear_all_content(conn: sqlite3.Connection) -> None:
    for table in _ROOT_TABLES_TO_CLEAR:
        conn.execute(f"DELETE FROM {table};")


def rebuild_cache(
    layout: RuneLayout, code_index: CodeIndexData | None = None
) -> dict[str, int]:
    """Fully rebuilds memory.db from canonical files (and, from Milestone 2
    on, the caller-supplied `code_index`). Zero LLM calls, zero network.
    Returns a small stats dict for `rune rebuild-cache`/`rune update`
    output.

    `code_index` has no canonical JSON/JSONL backing (files/symbols/edges
    are re-derived from source on every run — ARCHITECTURE.md invariant
    #2), so this function does not compute it itself; `core.update` does
    (either a full fresh scan+parse for `rune rebuild-cache`, or an
    incremental diff-and-reuse pass for `rune update`) and passes the
    result in. Omitting it (the Milestone 1 behavior, and what plain
    canonical-only tests use) simply leaves files/symbols/edges empty.

    Rebuilds **in place**, inside a single SQLite transaction (clear every
    table, then re-insert everything, then commit) rather than building a
    separate file and swapping it in. An earlier version used a temp-file
    + os.replace() swap for crash safety, but that approach turned out to
    be broken on Windows: os.replace() raises PermissionError when any
    other connection (e.g. a concurrent reader) still has memory.db open,
    which means the writer itself would fail whenever a reader was active
    — worse than the problem it was meant to solve. A single transaction
    on the real file gets the same crash-safety from SQLite's own
    rollback journal (an interrupted rebuild rolls back to the previous
    committed state on next open, same as any other failed transaction),
    and it composes correctly with WAL readers: a reader's already-open
    read transaction keeps seeing its snapshot until it starts a new one,
    without the underlying file ever needing to be swapped out.
    """
    code_index = code_index if code_index is not None else CodeIndexData()
    conn = connect(layout.memory_db)
    try:
        create_schema(conn)

        scopes_file = read_json_model(layout.scopes_json, ScopesFile) or ScopesFile()
        decisions = read_jsonl(layout.decisions_jsonl, MemoryRevision)
        constraints = read_jsonl(layout.constraints_jsonl, MemoryRevision)
        notes = read_jsonl(layout.notes_jsonl, Note)
        proposals = read_jsonl(layout.proposals_jsonl, Proposal)
        semantic = read_jsonl(layout.semantic_jsonl, ScopeSummary)

        conn.execute("BEGIN;")
        _clear_all_content(conn)
        _materialize_code_index(conn, code_index)
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
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "files": len(code_index.files),
        "symbols": len(code_index.symbols),
        "edges": len(code_index.edges),
        "scopes": len(scopes_file.scopes),
        "decisions": len(decisions),
        "constraints": len(constraints),
        "notes": len(notes),
        "proposals": len(proposals),
        "semantic_summaries": len(semantic),
    }
