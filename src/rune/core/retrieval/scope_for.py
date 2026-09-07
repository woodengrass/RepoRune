"""`rune scope-for`: given a file path, which scope(s) it belongs to and
the context an OpenCode adapter should inject the first time an agent
touches that scope in a session (ARCHITECTURE.md §6's `tool.execute.
before` flow, IMPLEMENTATION_PLAN.md Milestone 7's spike target).

This is the one new query shape Milestone 7 needed that wasn't already
pinned down by an earlier round's design discussion -- `rune search`/
`rune check`'s JSON shapes were decided the same way, inline during
implementation, since a new read-only CLI query's output shape isn't a
canonical schema change. Composed entirely from already-established
building blocks: `semantic_objects` (Milestone 5), `constraint_scopes`/
`note_scopes` (Milestone 6) -- current+visible filtering follows exactly
the same rules `core.retrieval.search`/`check` already use.

The per-scope-id query helpers below (`scope_summary`/`scope_constraints`/
`scope_notes`/`scope_decisions`) take an open connection rather than a
`RuneLayout` and are exported for reuse -- Milestone 8's `rune_scope_read`
(look up a *known* scope_id directly) and `rune_related_context` need
exactly the same per-scope queries `scope_for` (look up scope_id(s) *by
path*) already does, and duplicating the SQL a second time would let the
two drift out of sync.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from rune.core.project import RuneLayout
from rune.core.retrieval.search import possibly_stale_pointer
from rune.core.retrieval.symbol_search import symbol_search
from rune.core.storage.sqlite.materialize import connect_for_read

VISIBLE_CONSTRAINT_STATUSES = {"active", "review_required", "stale"}
VISIBLE_NOTE_STATUSES = {"active", "stale"}
VISIBLE_DECISION_STATUSES = {"active", "review_required"}
# ARCHITECTURE.md §7.1: only MUST/SHOULD are ever proactively injected;
# INFO-severity constraints are discoverable via `rune search` but don't
# justify spending context budget on every scope activation.
INJECTED_CONSTRAINT_SEVERITIES = {"MUST", "SHOULD"}


@dataclass(frozen=True)
class ScopeForConstraint:
    record_id: str
    severity: str
    content: str
    status: str
    warning: str | None


@dataclass(frozen=True)
class ScopeForNote:
    id: str
    category: str
    content: str
    status: str
    warning: str | None


@dataclass(frozen=True)
class ScopeForDecision:
    record_id: str
    content: str
    status: str
    warning: str | None


def scope_summary(conn: sqlite3.Connection, scope_id: str) -> tuple[str | None, str | None]:
    """(summary_text, summary_status) for `scope_id`: `(None, None)` if no
    semantic summary was ever generated; the real `purpose` text for
    `fresh`; `possibly_stale_pointer()`'s text (never the possibly-wrong
    prose itself) for `possibly_stale`/`stale`; `(None, status)` for
    `unavailable`/`orphaned` (a status exists, but nothing to show).
    """
    row = conn.execute(
        "SELECT purpose, status, payload_json FROM semantic_objects WHERE scope_id = ?", (scope_id,)
    ).fetchone()
    if row is None:
        return None, None
    status = row["status"]
    if status == "fresh":
        return row["purpose"], status
    if status in ("possibly_stale", "stale"):
        payload = json.loads(row["payload_json"])
        return possibly_stale_pointer(list(payload.get("source_files", {}))), status
    return None, status


def scope_constraints(conn: sqlite3.Connection, scope_id: str) -> list[ScopeForConstraint]:
    rows = conn.execute(
        "SELECT r.record_id, v.severity, v.content, v.status "
        "FROM constraint_scopes cs "
        "JOIN constraint_records r ON r.record_id = cs.record_id "
        "JOIN constraint_revisions v "
        "  ON v.record_id = r.record_id AND v.revision = r.current_revision "
        "WHERE cs.scope_id = ? AND cs.revision = r.current_revision",
        (scope_id,),
    ).fetchall()
    out = [
        ScopeForConstraint(
            record_id=row["record_id"], severity=row["severity"], content=row["content"],
            status=row["status"],
            warning=row["status"] if row["status"] in ("review_required", "stale") else None,
        )
        for row in rows
        if row["status"] in VISIBLE_CONSTRAINT_STATUSES and row["severity"] in INJECTED_CONSTRAINT_SEVERITIES
    ]
    out.sort(key=lambda c: (c.severity != "MUST", c.record_id))
    return out


def scope_notes(conn: sqlite3.Connection, scope_id: str) -> list[ScopeForNote]:
    rows = conn.execute(
        "SELECT r.id, v.category, v.content, v.status "
        "FROM note_scopes ns "
        "JOIN note_records r ON r.id = ns.id "
        "JOIN note_revisions v ON v.id = r.id AND v.revision = r.current_revision "
        "WHERE ns.scope_id = ? AND ns.revision = r.current_revision",
        (scope_id,),
    ).fetchall()
    out = [
        ScopeForNote(
            id=row["id"], category=row["category"], content=row["content"],
            status=row["status"], warning="[STALE]" if row["status"] == "stale" else None,
        )
        for row in rows
        if row["status"] in VISIBLE_NOTE_STATUSES
    ]
    out.sort(key=lambda n: n.id)
    return out


def scope_decisions(conn: sqlite3.Connection, scope_id: str) -> list[ScopeForDecision]:
    """Current+visible Decisions scoped to `scope_id` -- there was
    previously no per-scope Decision query anywhere in `core.retrieval`
    (`scope_for` only ever returned Constraints/Notes), needed once
    Milestone 8's `rune_scope_read`/`rune_related_context` MCP tools
    wanted a scope's Decisions alongside its Constraints/Notes.
    """
    rows = conn.execute(
        "SELECT r.record_id, v.content, v.status "
        "FROM decision_scopes ds "
        "JOIN decision_records r ON r.record_id = ds.record_id "
        "JOIN decision_revisions v "
        "  ON v.record_id = r.record_id AND v.revision = r.current_revision "
        "WHERE ds.scope_id = ? AND ds.revision = r.current_revision",
        (scope_id,),
    ).fetchall()
    out = [
        ScopeForDecision(
            record_id=row["record_id"], content=row["content"], status=row["status"],
            warning="review_required" if row["status"] == "review_required" else None,
        )
        for row in rows
        if row["status"] in VISIBLE_DECISION_STATUSES
    ]
    out.sort(key=lambda d: d.record_id)
    return out


@dataclass(frozen=True)
class ScopeForScope:
    scope_id: str
    name: str
    description: str
    summary: str | None
    # `None` when there's no usable summary at all (never generated, or
    # unavailable/orphaned -- nothing to show); the real `purpose` text
    # for `fresh`; `possibly_stale_pointer()`'s text (never the possibly-
    # wrong prose itself) for `possibly_stale`/`stale`, same rule
    # `core.retrieval.search._search_semantic` already applies.
    summary_status: str | None
    constraints: list[ScopeForConstraint] = field(default_factory=list)
    notes: list[ScopeForNote] = field(default_factory=list)


def scope_for(layout: RuneLayout, path: str) -> list[ScopeForScope]:
    """Every current scope `path` is a member of (DATA_MODEL.md §4: a
    file can belong to zero, one, or multiple scopes -- an empty list is
    a normal result, not an error), each with its scope summary,
    current+visible MUST/SHOULD constraints scoped to it, and
    current+visible notes scoped to it.
    """
    if not layout.memory_db.exists():
        return []
    conn = connect_for_read(layout)
    try:
        # A scope can include a file directly or include one of its symbols.
        # Both forms make touching the owning file an activation event.
        scope_rows = conn.execute(
            "SELECT s.id, s.name, s.description FROM scopes s "
            "WHERE EXISTS (SELECT 1 FROM scope_files sf "
            "              WHERE sf.scope_id = s.id AND sf.file = ?) "
            "   OR EXISTS (SELECT 1 FROM scope_symbols ss "
            "              JOIN symbols sym ON sym.symbol_id = ss.symbol_id "
            "              WHERE ss.scope_id = s.id AND sym.file = ?) "
            "ORDER BY s.id",
            (path, path),
        ).fetchall()

        results: list[ScopeForScope] = []
        for scope_row in scope_rows:
            scope_id = scope_row["id"]
            summary_text, summary_status = scope_summary(conn, scope_id)
            results.append(
                ScopeForScope(
                    scope_id=scope_id, name=scope_row["name"], description=scope_row["description"],
                    summary=summary_text, summary_status=summary_status,
                    constraints=scope_constraints(conn, scope_id), notes=scope_notes(conn, scope_id),
                )
            )
        return results
    finally:
        conn.close()


def resolve_scope_for(
    layout: RuneLayout, *, path: str | None = None, symbol: str | None = None
) -> tuple[str | None, list[ScopeForScope]]:
    """`scope_for` by either a known `path` or a `symbol` name -- resolves
    `symbol` to its owning file via `symbol_search` (first full-text match;
    best-effort, same as every other "resolve a free-text symbol name"
    caller in this codebase) before delegating to `scope_for`. Exactly one
    of `path`/`symbol` is expected (mirrors `scope_for`'s single-path
    contract rather than silently picking one if both are given).

    Extracted so this resolution -- previously written directly in the MCP
    server's `rune_scope_for` tool -- lives in `core.retrieval` instead of
    the protocol layer (ARCHITECTURE.md §5: "no business logic in the
    protocol layer, only in core"), and so a future CLI `--symbol` option
    could reuse it without a second copy.

    Returns `(None, [])` if `path` is `None` and `symbol` resolves to no
    symbol (nothing to look up); otherwise `(resolved_path, scope_for(...))`.
    """
    resolved_path = path
    if resolved_path is None and symbol is not None:
        matches = symbol_search(layout, query=symbol, limit=1)
        if not matches:
            return None, []
        resolved_path = matches[0].file
    if resolved_path is None:
        return None, []
    return resolved_path, scope_for(layout, resolved_path)
