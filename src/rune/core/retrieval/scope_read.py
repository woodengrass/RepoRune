"""`rune scope-read <scope_id>` / MCP `rune_scope_read`: the full
agent-facing view of a single scope by id -- metadata, members, its fresh
(or possibly_stale/stale-pointer) semantic summary, and its current+visible
Decisions/Constraints/Notes.

The counterpart to `core.retrieval.scope_for` (path -> scopes) for the
"I already know the scope_id, give me everything about it" direction --
built from the exact same per-scope-id query helpers `scope_for` exports
(`scope_summary`/`scope_constraints`/`scope_notes`/`scope_decisions`), not
a second, potentially-drifting copy of that SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rune.core.project import RuneLayout
from rune.core.retrieval.scope_for import (
    ScopeForConstraint,
    ScopeForDecision,
    ScopeForNote,
    scope_constraints,
    scope_decisions,
    scope_notes,
    scope_summary,
)
from rune.core.storage.sqlite.materialize import connect_for_read


class ScopeNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class ScopeReadResult:
    scope_id: str
    name: str
    description: str
    locked: bool
    source: str
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    summary: str | None = None
    summary_status: str | None = None
    decisions: list[ScopeForDecision] = field(default_factory=list)
    constraints: list[ScopeForConstraint] = field(default_factory=list)
    notes: list[ScopeForNote] = field(default_factory=list)


def scope_read(layout: RuneLayout, scope_id: str) -> ScopeReadResult:
    """Raises `ScopeNotFoundError` if `scope_id` isn't a currently
    materialized scope (unlike `scope_for`, which returns `[]` for "no
    scope owns this path" -- here the caller named a specific scope_id, so
    a miss is a genuine lookup error, not a normal empty result)."""
    conn = connect_for_read(layout)
    try:
        scope_row = conn.execute(
            "SELECT id, name, description, locked, source FROM scopes WHERE id = ?", (scope_id,)
        ).fetchone()
        if scope_row is None:
            raise ScopeNotFoundError(f"no scope with id {scope_id!r}")

        files = [
            row[0] for row in conn.execute("SELECT file FROM scope_files WHERE scope_id = ? ORDER BY file", (scope_id,))
        ]
        symbols = [
            row[0]
            for row in conn.execute(
                "SELECT symbol_id FROM scope_symbols WHERE scope_id = ? ORDER BY symbol_id", (scope_id,)
            )
        ]
        summary_text, summary_status = scope_summary(conn, scope_id)

        return ScopeReadResult(
            scope_id=scope_row["id"], name=scope_row["name"], description=scope_row["description"],
            locked=bool(scope_row["locked"]), source=scope_row["source"],
            files=files, symbols=symbols, summary=summary_text, summary_status=summary_status,
            decisions=scope_decisions(conn, scope_id),
            constraints=scope_constraints(conn, scope_id),
            notes=scope_notes(conn, scope_id),
        )
    finally:
        conn.close()


__all__ = ["ScopeNotFoundError", "ScopeReadResult", "scope_read"]
