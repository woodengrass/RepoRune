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
from urllib.parse import quote

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


class CacheUnusableError(Exception):
    """Raised by `connect_for_read` when `memory.db` exists but isn't a
    usable rune cache -- 0 bytes, truncated, or otherwise not opening as
    the expected schema (`sqlite3.DatabaseError`, which `OperationalError`
    is a subclass of, covers "file is not a database" and "no such
    table" alike). Confirmed by hand: `rune search`/`rune check` against a
    0-byte `memory.db` previously surfaced a raw `sqlite3.OperationalError`
    traceback instead of a message telling the user what to do. The fix
    is `rune rebuild-cache` -- memory.db is always safe to discard and
    regenerate from canonical + a fresh scan.
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
    try:
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
    except BaseException:
        # `sqlite3.connect()` succeeds even for a corrupt/0-byte file --
        # it's the first PRAGMA that actually fails to open it as a
        # database. Without closing here, the failed `Connection` still
        # holds the file open; a caller that reacts to the error by
        # trying to delete and recreate `db_path` (`rebuild_cache`'s
        # corrupt-cache self-heal) would then hit a Windows
        # `PermissionError` on the unlink, since Windows refuses to
        # remove a file with an open handle (confirmed by hand).
        conn.close()
        raise
    conn.row_factory = sqlite3.Row
    return conn


def connect_for_read(layout: RuneLayout) -> sqlite3.Connection:
    """Read-only-in-spirit connection for `rune search`/`rune check`
    (readers never write to `memory.db`): opens the file and confirms
    it's actually a usable, **current-shape** rune cache before handing
    it back, rather than letting a 0-byte/truncated file, or an
    old-shape one, surface a raw `sqlite3.OperationalError` traceback the
    first time a query touches a missing table/column. Raises
    `CacheUnusableError` (not the underlying sqlite3 exception) so
    callers can give the user an actionable message instead of a stack
    trace.

    Checking `schema_meta.schema_version` merely being *queryable* isn't
    enough on its own -- confirmed by hand: an old-shape cache (its
    `schema_version` row still says the *previous* `CACHE_SCHEMA_VERSION`,
    e.g. right after upgrading `rune` itself, before the next write
    triggers `rebuild_cache`'s own self-heal via `_ensure_compatible_
    cache_schema`) passes that check fine and then crashes on the first
    query touching a column/table that only exists in the newer shape
    (e.g. `fts_decisions.revision`, added when this round's `--history`
    fix bumped 2->3). This function is read-only, so it can't self-heal
    by discarding the file the way the writer path does -- it just
    reports the mismatch clearly instead of crashing on a raw sqlite3
    error, and points at `rune rebuild-cache` to fix it.
    """
    try:
        # URI-escape the path while preserving separators and a Windows drive
        # colon. Otherwise `#` and `?` in a repository path become URI syntax.
        db_uri_path = quote(layout.memory_db.as_posix(), safe="/:")
        conn = sqlite3.connect(f"file:{db_uri_path}?mode=ro", uri=True)
    except (sqlite3.Error, ValueError) as exc:
        raise CacheUnusableError(
            f"{layout.memory_db} is missing or isn't a usable cache ({exc}). "
            "Run `rune rebuild-cache` to regenerate it."
        ) from exc
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        stored_version = int(row[0]) if row is not None else None
    except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
        conn.close()
        raise CacheUnusableError(
            f"{layout.memory_db} exists but isn't a usable cache ({exc}). "
            "Run `rune rebuild-cache` to regenerate it."
        ) from exc
    if stored_version != CACHE_SCHEMA_VERSION:
        conn.close()
        raise CacheUnusableError(
            f"{layout.memory_db} is an old-shape cache (schema_version={stored_version!r}, "
            f"expected {CACHE_SCHEMA_VERSION}). Run `rune rebuild-cache` to regenerate it."
        )
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_schema_sql_text())


# Bumped whenever schema.sql's table *shapes* change in a way `CREATE
# TABLE IF NOT EXISTS` can't self-heal on an already-existing memory.db —
# a new column on an existing table, a changed column type, a new NOT
# NULL constraint, etc. (adding a brand new table doesn't need a bump:
# IF NOT EXISTS already handles that fine either way). Distinct from
# schema_versions.CURRENT_SCHEMA_VERSION, which is about canonical JSONL/
# JSON records' own `schema_version` field — an orthogonal concern from
# the derived SQLite cache's table shapes.
#
# Confirmed by hand this gap was real: an old memory.db predating
# Milestone 5 (semantic_objects without the current_revision column this
# milestone added) crashed every subsequent `rune update`/`rebuild-cache`
# with an unhandled `OperationalError: no such column: current_revision`
# — the only "fix" was manually deleting `.rune/cache/`. `_ensure_
# compatible_cache_schema` below makes that automatic: memory.db is
# always fully derived from canonical (ARCHITECTURE.md invariant #1), so
# a version mismatch is handled the exact same safe way `rune
# rebuild-cache` already promises — discard and rebuild fresh — just
# triggered without the user needing to know to do it themselves.
CACHE_SCHEMA_VERSION = 3
# Bumped for Milestone 6's review round: fts_decisions/fts_constraints/
# fts_notes gained a `revision` column (index every revision, not just
# current) so `rune search --history` can actually find a superseded
# revision's own text -- an old-shape memory.db without that column would
# make `_materialize_fts`'s INSERT fail with `OperationalError: table
# fts_decisions has 2 columns but 3 values were supplied`.


def _discard_cache_file(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        (db_path.parent / (db_path.name + suffix)).unlink(missing_ok=True)


def discard_cache(layout: RuneLayout) -> None:
    """Removes every derived-cache file after a failed post-commit
    canonical write. The next update or rebuild derives a fresh cache from
    source and canonical state instead of exposing an ahead-of-source DB.
    """
    _discard_cache_file(layout.memory_db)


def _ensure_compatible_cache_schema(
    layout: RuneLayout, conn: sqlite3.Connection
) -> sqlite3.Connection:
    """Returns a connection guaranteed to be on a fresh (or already
    matching) schema — discarding and recreating memory.db first if the
    on-disk `schema_meta.schema_version` doesn't match `CACHE_SCHEMA_
    VERSION` (including a pre-schema_meta-table memory.db, treated the
    same as any other incompatible shape).
    """
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        stored_version = int(row[0]) if row is not None else None
    except sqlite3.OperationalError:
        stored_version = None
    if stored_version == CACHE_SCHEMA_VERSION:
        return conn
    conn.close()
    _discard_cache_file(layout.memory_db)
    return connect(layout.memory_db)


def _group_current_by_id(
    records: list[MemoryRevision] | list[Note] | list[Proposal] | list[ScopeSummary],
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
    # `scope_files.file`/`scope_symbols.symbol_id` now carry a real FK to
    # files/symbols (restored in Milestone 2, per the deferral tracked in
    # schema.sql and DATA_MODEL.md §5 since Milestone 1). `_materialize_
    # code_index` runs before this function in `rebuild_cache`, so the
    # current code index is already visible on `conn` — query it rather
    # than threading the sets through the call chain. A scope member that
    # doesn't match anything currently indexed (deleted file, renamed
    # symbol, typo in scopes.json) is silently dropped from membership
    # here rather than raising: `INSERT OR IGNORE` does NOT suppress
    # foreign-key violations in SQLite (verified — it only suppresses
    # UNIQUE conflicts), so without this pre-filter a single stale
    # membership would abort the whole materialize.
    valid_files = {row[0] for row in conn.execute("SELECT path FROM files")}
    valid_symbols = {row[0] for row in conn.execute("SELECT symbol_id FROM symbols")}

    for scope in scopes_file.scopes:
        conn.execute(
            "INSERT INTO scopes (id, name, description, locked, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope.id, scope.name, scope.description, int(scope.locked), scope.source.value),
        )
        for file_path in scope.members.files:
            if file_path not in valid_files:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO scope_files (scope_id, file) VALUES (?, ?)",
                (scope.id, file_path),
            )
        for symbol_id in scope.members.symbols:
            if symbol_id not in valid_symbols:
                continue
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


def _materialize_fts(
    conn: sqlite3.Connection,
    decisions: list[MemoryRevision],
    constraints: list[MemoryRevision],
    notes: list[Note],
    semantic: list[ScopeSummary],
    symbols: list[Symbol],
) -> None:
    """Rebuilds every FTS5 index in full on each materialize pass
    (ARCHITECTURE.md §4.8, DATA_MODEL.md §5's `fts_*` tables). These are
    virtual tables with no FK relationship to the tables they index, so
    they are NOT covered by `_clear_all_content`'s cascading deletes --
    they must be cleared and repopulated here explicitly, same "full
    rebuild every time" contract as everything else in this function.

    `fts_decisions`/`fts_constraints`/`fts_notes` index **every**
    revision, not just current, each row carrying its own `revision`
    number: DATA_MODEL.md §3 promises "history 模式可看到全部 revision",
    which needs a superseded revision's own text to be findable at all --
    indexing only the current revision (as this used to do) meant
    `rune search --history` could never find a Decision that read
    "use redis" in revision 1 before being replaced by "use postgres" in
    revision 2, because "redis" was never in the FTS index to begin with.
    `core.retrieval.search` decides current-vs-historical (and thus
    visibility/rank) per hit by comparing the row's `revision` against
    the record's `current_revision`, not at index time.
    """
    conn.execute("DELETE FROM fts_decisions;")
    conn.execute("DELETE FROM fts_constraints;")
    conn.execute("DELETE FROM fts_notes;")
    conn.execute("DELETE FROM fts_semantic;")
    conn.execute("DELETE FROM fts_symbols;")

    for record_id, revs in _group_current_by_id(decisions, "record_id", "decisions.jsonl").items():
        for rev in revs:
            conn.execute(
                "INSERT INTO fts_decisions (record_id, revision, text) VALUES (?, ?, ?)",
                (record_id, rev.revision, f"{rev.content}\n{rev.rationale}"),
            )
    for record_id, revs in _group_current_by_id(constraints, "record_id", "constraints.jsonl").items():
        for rev in revs:
            conn.execute(
                "INSERT INTO fts_constraints (record_id, revision, text) VALUES (?, ?, ?)",
                (record_id, rev.revision, f"{rev.content}\n{rev.rationale}"),
            )
    for note_id, revs in _group_current_by_id(notes, "id", "notes.jsonl").items():
        for rev in revs:
            conn.execute(
                "INSERT INTO fts_notes (note_id, revision, text) VALUES (?, ?, ?)",
                (note_id, rev.revision, f"{rev.content}\n{rev.why_persist}"),
            )
    for scope_id, summary in current_scope_summaries(semantic).items():
        conn.execute(
            "INSERT INTO fts_semantic (scope_id, text) VALUES (?, ?)", (scope_id, summary.purpose)
        )
    for symbol in symbols:
        text = symbol.qualified_name if not symbol.signature else f"{symbol.qualified_name} {symbol.signature}"
        conn.execute("INSERT INTO fts_symbols (symbol_id, text) VALUES (?, ?)", (symbol.symbol_id, text))


def _materialize_semantic(conn: sqlite3.Connection, summaries: list[ScopeSummary]) -> None:
    # current_revision = MAX(revision) per scope_id, same convention (and
    # same duplicate-(id, revision) conflict detection) as Decision/
    # Constraint/Note — see DATA_MODEL.md §2.4's revised revision mechanism,
    # which replaced the earlier "last line wins by append order" scheme.
    #
    # A scope that no longer exists (deleted from scopes.json) can still
    # have semantic history in semantic.jsonl — `semantic_objects.scope_id`
    # has a real FK to scopes(id), so inserting a row for a vanished scope
    # would abort the whole materialize (confirmed by hand: deleting a
    # scope with prior summary history made every subsequent `rune update`/
    # `rebuild-cache` crash with IntegrityError). `core.update` is
    # responsible for appending an `orphaned` revision to semantic.jsonl
    # canonical history *before* this runs (mirroring Decision/Constraint's
    # existing orphan handling); this function's job is only to make sure
    # materialize never crashes on one, current or not — an `orphaned`
    # summary (or, defensively, any summary whose scope vanished without
    # one for some other reason) is excluded from `semantic_objects`
    # entirely, same as a dangling scope_files/scope_symbols membership
    # above.
    valid_scope_ids = {row[0] for row in conn.execute("SELECT id FROM scopes")}
    grouped = _group_current_by_id(summaries, "scope_id", "semantic.jsonl")
    for scope_id, revs in grouped.items():
        if scope_id not in valid_scope_ids:
            continue
        current = revs[-1]
        conn.execute(
            "INSERT INTO semantic_objects "
            "(scope_id, current_revision, purpose, payload_json, generated_at, "
            " model, source_hash, status, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scope_id, current.revision, current.purpose,
                current.model_dump_json(), current.generated_at, current.model,
                current.source_hash, current.status.value, current.last_error,
            ),
        )


def current_scope_summaries(summaries: list[ScopeSummary]) -> dict[str, ScopeSummary]:
    """Public helper for `core.update`/`core.semantic.worker`, which need
    this run's current per-scope `ScopeSummary` *before* `rebuild_cache`
    runs (to decide which scopes are stale and need a refresh attempt).
    Reuses the same duplicate-(scope_id, revision) conflict detection
    `rebuild_cache` itself applies, so a canonical conflict is caught here
    too rather than only surfacing once the whole update tries to commit.
    """
    grouped = _group_current_by_id(summaries, "scope_id", "semantic.jsonl")
    return {scope_id: revs[-1] for scope_id, revs in grouped.items()}


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
    file_paths = {f.path for f in code_index.files}
    symbol_ids = {s.symbol_id for s in code_index.symbols}

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
        # An edge reused from an *unchanged* file can point at a file/
        # symbol that this same run just dropped (e.g. the file it used
        # to import was deleted, or the target symbol was renamed away).
        # Null out a stale target rather than inserting it: `target_file
        # REFERENCES files(path)` would otherwise raise IntegrityError and
        # abort the whole update over one dangling edge. This is the same
        # "unresolved" representation already used for bare package
        # imports (confidence stays 1.0 — the import statement itself is
        # still real, only its resolved target became stale).
        target_file = e.target_file if e.target_file in file_paths else None
        target_symbol = e.target_symbol if e.target_symbol in symbol_ids else None
        conn.execute(
            "INSERT INTO edges "
            "(source_symbol, source_file, target_symbol, target_file, edge_type, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                e.source_symbol, e.source_file, target_symbol, target_file,
                e.edge_type.value, e.confidence,
            ),
        )


def read_current_code_index(layout: RuneLayout) -> CodeIndexData:
    """Reads the currently-materialized files/symbols/edges back out of
    memory.db. Used by `core.update` to know what was indexed last time
    (for change detection) and to reuse symbols/edges for files whose
    content hash hasn't changed, without re-parsing them. Returns an
    empty CodeIndexData if memory.db doesn't exist yet (first run), and
    the same empty result if it exists but isn't a readable database at
    all (0 bytes, truncated) -- confirmed by hand: a corrupt `memory.db`
    made this raise a raw `sqlite3.OperationalError: no such table:
    files`, which propagated out of every write command that calls this
    indirectly via `core.memory.records.refresh_cache` (note add,
    proposal approve, ...), with no clean-error contract at all, unlike
    `rune search`/`check`'s `connect_for_read`. Deliberately narrower
    than `connect_for_read`'s check, though: this only catches
    `sqlite3.DatabaseError` (file genuinely won't open as a database),
    NOT an old-shape-but-structurally-valid cache -- `core.update` relies
    on being able to read an old-shape cache here *before*
    `rebuild_cache`'s own `_ensure_compatible_cache_schema` self-heal
    runs, so treating an old schema_version as unusable here would break
    that self-healing path.
    """
    if not layout.memory_db.exists():
        return CodeIndexData()
    try:
        conn = connect(layout.memory_db)
    except sqlite3.DatabaseError:
        # A corrupt/0-byte file fails even the `PRAGMA journal_mode`
        # `connect()` runs on every connection, before any table is ever
        # queried -- confirmed by hand this is where the crash actually
        # happened, not in the SELECT statements below.
        return CodeIndexData()
    try:
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
        except sqlite3.DatabaseError:
            return CodeIndexData()
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


@dataclass(frozen=True)
class SemanticRunMetricsRecord:
    """One `semantic_run_metrics` row (ARCHITECTURE.md §4.5's six run-level
    metrics, Milestone 5). `provider`/`model` are the run's *configured*
    primary provider/model, not a per-scope breakdown — which scope used
    the fallback model instead is already captured by `fallback_rate`.
    """

    run_at: str
    provider: str
    model: str
    scopes_attempted: int
    schema_success_rate: float
    reference_strip_rate: float
    fallback_rate: float
    provider_error_rate: float
    total_cost: float
    total_latency_seconds: float


def _materialize_semantic_run_metrics(
    conn: sqlite3.Connection, record: SemanticRunMetricsRecord | None
) -> None:
    if record is None:
        return
    conn.execute(
        "INSERT INTO semantic_run_metrics "
        "(run_at, provider, model, scopes_attempted, schema_success_rate, "
        " reference_strip_rate, fallback_rate, provider_error_rate, "
        " total_cost, total_latency_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record.run_at, record.provider, record.model, record.scopes_attempted,
            record.schema_success_rate, record.reference_strip_rate,
            record.fallback_rate, record.provider_error_rate,
            record.total_cost, record.total_latency_seconds,
        ),
    )


def rebuild_cache(
    layout: RuneLayout,
    code_index: CodeIndexData | None = None,
    scopes_override: ScopesFile | None = None,
    semantic_override: list[ScopeSummary] | None = None,
    semantic_run_metrics: SemanticRunMetricsRecord | None = None,
    decisions_override: list[MemoryRevision] | None = None,
    constraints_override: list[MemoryRevision] | None = None,
    notes_override: list[Note] | None = None,
) -> dict[str, int | float | str | None]:
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

    `scopes_override`, when given, is materialized in place of reading
    `layout.scopes_json` from disk. This exists solely so `core.update`'s
    incremental auto-assignment (Milestone 4) can have its just-computed,
    not-yet-written `ScopesFile` land in this same SQLite transaction
    *before* the canonical `scopes.json` write happens — see the call site
    for why the ordering (commit cache first, write canonical scopes.json
    only after) matters for all-or-nothing update semantics. Every other
    canonical file (decisions/constraints/notes/proposals/semantic) still
    always reads fresh from disk here; scopes.json is the only one with an
    in-flight in-memory mutation to reconcile in Milestone 4's scope.

    `semantic_override`, when given, is the *complete* current set of
    `ScopeSummary` (existing current revisions plus this run's new ones) to
    materialize in place of reading `layout.semantic_jsonl`. Same rationale
    as `scopes_override` (Milestone 5): `core.semantic.worker`'s freshly
    computed revisions must land in this same transaction, but the
    canonical `semantic.jsonl` append only happens after `rebuild_cache`
    actually succeeds — see the call site.

    `semantic_run_metrics`, when given, appends one row to the (never
    cleared, purely historical) `semantic_run_metrics` table in this same
    transaction — `core.update` only passes one when a semantic refresh
    actually ran this update (never for `rune rebuild-cache`, which makes
    no LLM calls at all).

    `decisions_override`/`constraints_override`/`notes_override`, when
    given, are materialized in place of reading the corresponding
    canonical JSONL from disk — same rationale and pattern as
    `scopes_override`/`semantic_override` above (Milestone 6):
    `core.memory.staleness`'s freshly computed lifecycle-transition
    revisions must land in this same transaction, with the canonical
    append deferred until after `rebuild_cache` succeeds.

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
    try:
        conn = connect(layout.memory_db)
    except sqlite3.DatabaseError:
        # A 0-byte/truncated/corrupt memory.db fails even the `PRAGMA
        # journal_mode` `connect()` runs on every connection -- before
        # `_ensure_compatible_cache_schema` below ever gets a `conn` to
        # inspect. `rebuild_cache` is the one place in this codebase
        # that's always allowed to discard memory.db (it's a from-
        # scratch rebuild by definition), so this is the same self-heal
        # `_ensure_compatible_cache_schema` already does for an
        # old-shape cache, just triggered one failure mode earlier
        # (confirmed by hand: without this, `rune update`/`rebuild-cache`
        # against a corrupt memory.db raised a raw `sqlite3.DatabaseError:
        # file is not a database` instead of self-healing).
        _discard_cache_file(layout.memory_db)
        conn = connect(layout.memory_db)
    conn = _ensure_compatible_cache_schema(layout, conn)
    try:
        create_schema(conn)

        scopes_file = (
            scopes_override
            if scopes_override is not None
            else read_json_model(layout.scopes_json, ScopesFile) or ScopesFile()
        )
        decisions = (
            decisions_override
            if decisions_override is not None
            else read_jsonl(layout.decisions_jsonl, MemoryRevision)
        )
        constraints = (
            constraints_override
            if constraints_override is not None
            else read_jsonl(layout.constraints_jsonl, MemoryRevision)
        )
        notes = notes_override if notes_override is not None else read_jsonl(layout.notes_jsonl, Note)
        proposals = read_jsonl(layout.proposals_jsonl, Proposal)
        semantic = (
            semantic_override
            if semantic_override is not None
            else read_jsonl(layout.semantic_jsonl, ScopeSummary)
        )

        conn.execute("BEGIN;")
        _clear_all_content(conn)
        _materialize_code_index(conn, code_index)
        _materialize_scopes(conn, scopes_file)
        _materialize_decisions_or_constraints(conn, decisions, RecordType.decision)
        _materialize_decisions_or_constraints(conn, constraints, RecordType.constraint)
        _materialize_notes(conn, notes)
        _materialize_proposals(conn, proposals)
        _materialize_semantic(conn, semantic)
        _materialize_semantic_run_metrics(conn, semantic_run_metrics)
        _materialize_fts(conn, decisions, constraints, notes, semantic, code_index.symbols)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (str(CACHE_SCHEMA_VERSION),),
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
        # These are user-facing logical-record counts. Canonical JSONL keeps
        # every revision, but one cache record represents each current ID.
        "decisions": len(_group_current_by_id(decisions, "record_id", "decisions.jsonl")),
        "constraints": len(_group_current_by_id(constraints, "record_id", "constraints.jsonl")),
        "notes": len(_group_current_by_id(notes, "id", "notes.jsonl")),
        "proposals": len(_group_current_by_id(proposals, "proposal_id", "proposals.jsonl")),
        "semantic_summaries": len(current_scope_summaries(semantic)),
    }
