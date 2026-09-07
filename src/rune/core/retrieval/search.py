"""`rune search`: SQLite FTS5 lookup across scope summaries, Decisions,
Constraints, and Notes, ranked by ARCHITECTURE.md §4.8's eight-layer
priority order (this round's revision over the original six-layer
version, inserting the Global/Scoped MUST Constraint split):

    1. Global MUST Constraint    2. Scoped MUST Constraint
    3. Active Decision           4. Scoped SHOULD Constraint
    5. Fresh Semantic Summary    6. Fresh Note
    7. Stale Note                8. Historical data (history mode only)

Only MUST is split by global/scoped -- ARCHITECTURE.md §4.8 introduces
that split specifically for MUST, so SHOULD and INFO-severity constraints
(global or scoped) share rank 4; there's no separate rank for a Decision/
Constraint that's current+visible-with-a-warning (`review_required`/
`stale`) either, since the eight-layer list doesn't name one -- those are
returned at their severity/type's normal rank with `warning` set, rather
than invented a new rank the spec doesn't define.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from rune.core.project import RuneLayout
from rune.core.storage.sqlite.materialize import CacheUnusableError, connect_for_read

RANK_GLOBAL_MUST = 1
RANK_SCOPED_MUST = 2
RANK_ACTIVE_DECISION = 3
RANK_SHOULD_CONSTRAINT = 4
RANK_FRESH_SEMANTIC = 5
RANK_FRESH_NOTE = 6
RANK_STALE_NOTE = 7
RANK_HISTORICAL = 8

_DECISION_VISIBLE = {"active", "review_required"}
_CONSTRAINT_VISIBLE = {"active", "review_required", "stale"}
_NOTE_VISIBLE = {"active", "stale"}


@dataclass(frozen=True)
class SearchResult:
    kind: str  # "constraint" | "decision" | "semantic" | "note"
    rank: int
    id: str  # record_id / scope_id / note_id
    text: str
    status: str
    warning: str | None = None
    revision: int = 0
    # The revision this hit came from; 0 for semantic (not revision-
    # addressed the same way). A `warning="superseded"` result's
    # `revision` is NOT the record's current revision -- see
    # `_search_decisions` for why that distinction matters for
    # `--history`.


def fts_phrase(query: str) -> str:
    """FTS5 phrase-query form of an arbitrary user string: quoted, with
    embedded double-quotes doubled per FTS5's own escaping rule. V1
    deliberately does not expose FTS5's full query syntax (AND/OR/NEAR/
    column filters) to the CLI -- a plain phrase match is simpler and
    can't raise an `OperationalError` on a query string containing
    hyphens/colons/etc., which a passthrough MATCH would.
    """
    return '"' + query.replace('"', '""') + '"'


_ALL_KINDS = frozenset({"constraint", "decision", "semantic", "note"})


def search(
    layout: RuneLayout,
    query: str,
    *,
    history: bool = False,
    limit: int = 50,
    kinds: frozenset[str] | None = None,
) -> list[SearchResult]:
    """Runs `query` against every FTS5 index and returns matches sorted by
    rank (ascending -- rank 1 first), then by id for a stable order within
    a rank.

    `kinds`, when given, restricts which of `{"constraint", "decision",
    "semantic", "note"}` are queried at all (not just filtered after the
    fact -- skips the FTS5 query entirely for an excluded kind). `None`
    (the default) means all four, preserving every existing caller's
    behavior unchanged. Symbol search is not one of these kinds -- it's a
    structured field lookup, not a phrase match over prose, and lives in
    `core.retrieval.symbol_search` instead.

    `history=True` additionally includes:
    - non-visible *current* revisions (`inactive`/`orphaned` Decisions/
      Constraints, `expired`/`orphaned`/`archived` Notes), and
    - a **superseded** revision whose own text matches but whose record's
      current revision doesn't (DATA_MODEL.md §3: "history 模式可看到全部
      revision" -- e.g. a Decision that said "use redis" in revision 1,
      replaced by "use postgres" in revision 2, must still be findable by
      searching "redis" in history mode, even though "redis" appears
      nowhere in the current revision's text).

    Raises `CacheUnusableError` if `memory.db` exists but isn't openable
    as a real cache (0 bytes, truncated); returns `[]` if it doesn't
    exist at all (nothing has been indexed yet, not an error).
    """
    wanted = kinds if kinds is not None else _ALL_KINDS
    if not layout.memory_db.exists():
        return []
    conn = connect_for_read(layout)
    try:
        phrase = fts_phrase(query)
        results: list[SearchResult] = []
        if "constraint" in wanted:
            results.extend(_search_constraints(conn, phrase, history))
        if "decision" in wanted:
            results.extend(_search_decisions(conn, phrase, history))
        if "semantic" in wanted:
            results.extend(_search_semantic(conn, phrase))
        if "note" in wanted:
            results.extend(_search_notes(conn, phrase, history))
        results.sort(key=lambda r: (r.rank, r.id))
        return results[:limit]
    finally:
        conn.close()


def _search_decisions(conn: sqlite3.Connection, phrase: str, history: bool) -> list[SearchResult]:
    rows = conn.execute(
        "SELECT f.record_id AS record_id, f.revision AS hit_revision, "
        "       r.current_revision AS current_revision, v.status AS status, v.content AS content "
        "FROM fts_decisions f "
        "JOIN decision_records r ON r.record_id = f.record_id "
        "JOIN decision_revisions v ON v.record_id = f.record_id AND v.revision = f.revision "
        "WHERE fts_decisions MATCH ?",
        (phrase,),
    ).fetchall()
    out: list[SearchResult] = []
    for row in rows:
        is_current = row["hit_revision"] == row["current_revision"]
        if is_current:
            visible = row["status"] in _DECISION_VISIBLE
            if not visible and not history:
                continue
            warning = "review_required" if row["status"] == "review_required" else None
            rank = RANK_ACTIVE_DECISION if visible else RANK_HISTORICAL
        else:
            if not history:
                continue
            warning = "superseded"
            rank = RANK_HISTORICAL
        out.append(
            SearchResult(
                kind="decision", rank=rank, id=row["record_id"], text=row["content"],
                status=row["status"], warning=warning, revision=row["hit_revision"],
            )
        )
    return out


def _search_constraints(conn: sqlite3.Connection, phrase: str, history: bool) -> list[SearchResult]:
    rows = conn.execute(
        "SELECT f.record_id AS record_id, f.revision AS hit_revision, "
        "       r.current_revision AS current_revision, v.status AS status, v.content AS content, "
        "       v.severity AS severity, "
        "       EXISTS(SELECT 1 FROM constraint_scopes cs WHERE cs.record_id = f.record_id "
        "              AND cs.revision = f.revision) AS is_scoped "
        "FROM fts_constraints f "
        "JOIN constraint_records r ON r.record_id = f.record_id "
        "JOIN constraint_revisions v ON v.record_id = f.record_id AND v.revision = f.revision "
        "WHERE fts_constraints MATCH ?",
        (phrase,),
    ).fetchall()
    out: list[SearchResult] = []
    for row in rows:
        is_current = row["hit_revision"] == row["current_revision"]
        if is_current:
            visible = row["status"] in _CONSTRAINT_VISIBLE
            if not visible and not history:
                continue
            warning = row["status"] if row["status"] in ("review_required", "stale") else None
            if not visible:
                rank = RANK_HISTORICAL
            elif row["severity"] == "MUST":
                rank = RANK_GLOBAL_MUST if not row["is_scoped"] else RANK_SCOPED_MUST
            else:
                rank = RANK_SHOULD_CONSTRAINT
        else:
            if not history:
                continue
            warning = "superseded"
            rank = RANK_HISTORICAL
        out.append(
            SearchResult(
                kind="constraint", rank=rank, id=row["record_id"], text=row["content"],
                status=row["status"], warning=warning, revision=row["hit_revision"],
            )
        )
    return out


def possibly_stale_pointer(source_files: list[str]) -> str:
    """The message Milestone 5's round-15 decision (IMPLEMENTATION_PLAN.md
    item 83) requires retrieval to return in place of a `possibly_stale`/
    `stale` scope summary's actual text: an outdated LLM-generated summary
    risks looking authoritative while being wrong, which is worse than
    pointing the reader at the real source directly.
    """
    files = ", ".join(sorted(source_files)) if source_files else "(no source files recorded)"
    return f"this scope's summary is outdated -- read these files directly: {files}"


def _search_semantic(conn: sqlite3.Connection, phrase: str) -> list[SearchResult]:
    """`fresh` summaries return their real `purpose` text at
    `RANK_FRESH_SEMANTIC`. `possibly_stale`/`stale` still match (the old
    `purpose` text stays in the FTS index -- `mark_possibly_stale`/a
    failed refresh both copy it forward) but their result text is
    replaced with `possibly_stale_pointer()`, never the possibly-wrong
    prose itself. `unavailable`/`orphaned` never had usable content and
    are skipped entirely -- there's nothing to point at.
    """
    rows = conn.execute(
        "SELECT s.scope_id AS scope_id, s.purpose AS purpose, s.status AS status, "
        "       s.payload_json AS payload_json "
        "FROM fts_semantic f "
        "JOIN semantic_objects s ON s.scope_id = f.scope_id "
        "WHERE fts_semantic MATCH ?",
        (phrase,),
    ).fetchall()
    out: list[SearchResult] = []
    for row in rows:
        status = row["status"]
        if status == "fresh":
            text = row["purpose"]
        elif status in ("possibly_stale", "stale"):
            payload = json.loads(row["payload_json"])
            text = possibly_stale_pointer(list(payload.get("source_files", {})))
        else:
            continue
        out.append(
            SearchResult(kind="semantic", rank=RANK_FRESH_SEMANTIC, id=row["scope_id"], text=text, status=status)
        )
    return out


def _search_notes(conn: sqlite3.Connection, phrase: str, history: bool) -> list[SearchResult]:
    rows = conn.execute(
        "SELECT f.note_id AS note_id, f.revision AS hit_revision, "
        "       r.current_revision AS current_revision, v.status AS status, v.content AS content "
        "FROM fts_notes f "
        "JOIN note_records r ON r.id = f.note_id "
        "JOIN note_revisions v ON v.id = f.note_id AND v.revision = f.revision "
        "WHERE fts_notes MATCH ?",
        (phrase,),
    ).fetchall()
    out: list[SearchResult] = []
    for row in rows:
        is_current = row["hit_revision"] == row["current_revision"]
        if is_current:
            visible = row["status"] in _NOTE_VISIBLE
            if not visible and not history:
                continue
            warning = "[STALE]" if row["status"] == "stale" else None
            if not visible:
                rank = RANK_HISTORICAL
            elif row["status"] == "stale":
                rank = RANK_STALE_NOTE
            else:
                rank = RANK_FRESH_NOTE
        else:
            if not history:
                continue
            warning = "superseded"
            rank = RANK_HISTORICAL
        out.append(
            SearchResult(
                kind="note", rank=rank, id=row["note_id"], text=row["content"],
                status=row["status"], warning=warning, revision=row["hit_revision"],
            )
        )
    return out


__all__ = ["CacheUnusableError", "SearchResult", "fts_phrase", "possibly_stale_pointer", "search"]
