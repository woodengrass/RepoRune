-- rune SQLite cache schema. Fully derived from canonical files + the
-- deterministic code index — see DATA_MODEL.md §5. Never hand-edit
-- memory.db; it is always safe to delete and run `rune rebuild-cache`.

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- deterministic code index (populated starting Milestone 2)
CREATE TABLE IF NOT EXISTS files (
    path          TEXT PRIMARY KEY,
    language      TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    git_blob_hash TEXT,
    indexed_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol_id      TEXT PRIMARY KEY,
    file            TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    qualified_name  TEXT NOT NULL,
    kind            TEXT NOT NULL,
    signature       TEXT,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);

CREATE TABLE IF NOT EXISTS edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_symbol   TEXT REFERENCES symbols(symbol_id) ON DELETE CASCADE,
    source_file     TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    target_symbol   TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
    target_file     TEXT REFERENCES files(path) ON DELETE SET NULL,
    edge_type       TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_file, source_symbol);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_file, target_symbol);

-- scope
CREATE TABLE IF NOT EXISTS scopes (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    locked      INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL
);

-- Restored in Milestone 2 (was deliberately deferred in Milestone 1 —
-- see git history — because `files`/`symbols` stayed empty until the
-- indexer landed). `_materialize_scopes` in materialize.py pre-filters
-- out any scope member that doesn't match a currently-indexed file/
-- symbol before inserting, since `INSERT OR IGNORE` does not suppress
-- foreign-key violations in SQLite (only UNIQUE conflicts) — without
-- that pre-filter, one stale/typo'd membership in scopes.json would
-- abort the whole materialize.
CREATE TABLE IF NOT EXISTS scope_files (
    scope_id TEXT NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
    file     TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    PRIMARY KEY (scope_id, file)
);

CREATE TABLE IF NOT EXISTS scope_symbols (
    scope_id  TEXT NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
    symbol_id TEXT NOT NULL REFERENCES symbols(symbol_id) ON DELETE CASCADE,
    PRIMARY KEY (scope_id, symbol_id)
);

-- semantic summary (current_revision = MAX(revision) per scope_id, same
-- convention as decision/constraint/note below; full history stays in
-- semantic.jsonl)
CREATE TABLE IF NOT EXISTS semantic_objects (
    scope_id         TEXT PRIMARY KEY REFERENCES scopes(id) ON DELETE CASCADE,
    current_revision INTEGER NOT NULL,
    purpose          TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    generated_at     TEXT NOT NULL,
    model            TEXT NOT NULL,
    source_hash      TEXT NOT NULL,
    status           TEXT NOT NULL,
    last_error       TEXT
);

-- decision (current_revision = MAX(revision), independent of status)
CREATE TABLE IF NOT EXISTS decision_records (
    record_id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_revisions (
    record_id TEXT NOT NULL REFERENCES decision_records(record_id) ON DELETE CASCADE,
    revision  INTEGER NOT NULL,
    status    TEXT NOT NULL,
    content   TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    critical  INTEGER NOT NULL DEFAULT 0,
    source_document TEXT,
    source_section  TEXT,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (record_id, revision)
);
CREATE TABLE IF NOT EXISTS decision_scopes (
    record_id TEXT NOT NULL, revision INTEGER NOT NULL, scope_id TEXT NOT NULL,
    FOREIGN KEY (record_id, revision) REFERENCES decision_revisions(record_id, revision) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS decision_files (
    record_id TEXT NOT NULL, revision INTEGER NOT NULL, file TEXT NOT NULL,
    FOREIGN KEY (record_id, revision) REFERENCES decision_revisions(record_id, revision) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS decision_symbols (
    record_id TEXT NOT NULL, revision INTEGER NOT NULL, symbol_id TEXT NOT NULL,
    FOREIGN KEY (record_id, revision) REFERENCES decision_revisions(record_id, revision) ON DELETE CASCADE
);

-- constraint (mirrors decision + severity/persistence_mode/snapshot columns)
CREATE TABLE IF NOT EXISTS constraint_records (
    record_id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS constraint_revisions (
    record_id TEXT NOT NULL REFERENCES constraint_records(record_id) ON DELETE CASCADE,
    revision  INTEGER NOT NULL,
    status    TEXT NOT NULL,
    content   TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    severity  TEXT NOT NULL,
    persistence_mode TEXT NOT NULL,
    source_hashes_json TEXT NOT NULL DEFAULT '{}',
    scope_hashes_json  TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT,
    source_document TEXT,
    source_section  TEXT,
    machine_check_hint TEXT,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (record_id, revision)
);
CREATE TABLE IF NOT EXISTS constraint_scopes (
    record_id TEXT NOT NULL, revision INTEGER NOT NULL, scope_id TEXT NOT NULL,
    FOREIGN KEY (record_id, revision) REFERENCES constraint_revisions(record_id, revision) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS constraint_files (
    record_id TEXT NOT NULL, revision INTEGER NOT NULL, file TEXT NOT NULL,
    FOREIGN KEY (record_id, revision) REFERENCES constraint_revisions(record_id, revision) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS constraint_symbols (
    record_id TEXT NOT NULL, revision INTEGER NOT NULL, symbol_id TEXT NOT NULL,
    FOREIGN KEY (record_id, revision) REFERENCES constraint_revisions(record_id, revision) ON DELETE CASCADE
);

-- pending proposal (derived cache; source of truth is .rune/proposals.jsonl)
CREATE TABLE IF NOT EXISTS pending_proposals (
    proposal_id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL,
    type        TEXT NOT NULL,
    record_id   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT
);

-- note (revision-based, mirrors decision; current_revision = MAX(revision))
CREATE TABLE IF NOT EXISTS note_records (
    id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS note_revisions (
    id          TEXT NOT NULL REFERENCES note_records(id) ON DELETE CASCADE,
    revision    INTEGER NOT NULL,
    category    TEXT NOT NULL,
    content     TEXT NOT NULL,
    why_persist TEXT NOT NULL,
    importance  REAL NOT NULL,
    confidence  REAL NOT NULL,
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    last_verified_at TEXT NOT NULL,
    expires_at  TEXT,
    source_hashes_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL,
    PRIMARY KEY (id, revision)
);
CREATE TABLE IF NOT EXISTS note_scopes (
    id TEXT NOT NULL, revision INTEGER NOT NULL, scope_id TEXT NOT NULL,
    FOREIGN KEY (id, revision) REFERENCES note_revisions(id, revision) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS note_files (
    id TEXT NOT NULL, revision INTEGER NOT NULL, file TEXT NOT NULL,
    FOREIGN KEY (id, revision) REFERENCES note_revisions(id, revision) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS note_symbols (
    id TEXT NOT NULL, revision INTEGER NOT NULL, symbol_id TEXT NOT NULL,
    FOREIGN KEY (id, revision) REFERENCES note_revisions(id, revision) ON DELETE CASCADE
);

-- Semantic worker run metrics (Milestone 5, ARCHITECTURE.md §4.5's six
-- run-level metrics). Purely operational/historical data, not canonical --
-- there is no semantic_run_metrics.jsonl backing this, so unlike every
-- other table here it is NOT cleared and rebuilt from canonical on every
-- materialize pass (see _ROOT_TABLES_TO_CLEAR in materialize.py): one row
-- is appended per `rune update` run that actually attempted a semantic
-- refresh, so history accumulates across runs within one memory.db. Lost
-- entirely if memory.db itself is deleted/rebuilt from scratch (`rune
-- rebuild-cache` after wiping .rune/cache/) -- accepted, since there is
-- deliberately no canonical record of past runs to rebuild it from.
CREATE TABLE IF NOT EXISTS semantic_run_metrics (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at                 TEXT NOT NULL,
    provider               TEXT NOT NULL,
    model                  TEXT NOT NULL,
    scopes_attempted       INTEGER NOT NULL,
    schema_success_rate    REAL NOT NULL,
    reference_strip_rate   REAL NOT NULL,
    fallback_rate          REAL NOT NULL,
    provider_error_rate    REAL NOT NULL,
    total_cost             REAL NOT NULL,
    total_latency_seconds  REAL NOT NULL
);

-- FTS5 (rebuilt in full on every materialize pass)
CREATE VIRTUAL TABLE IF NOT EXISTS fts_semantic USING fts5(scope_id UNINDEXED, text);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_decisions USING fts5(record_id UNINDEXED, text);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_constraints USING fts5(record_id UNINDEXED, text);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_notes USING fts5(note_id UNINDEXED, text);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_symbols USING fts5(symbol_id UNINDEXED, text);
