"""`rune changes` / MCP `rune_changes`: what Rune currently considers
changed -- working-tree diff (reusing the exact scan/diff `rune check`/
`rune status` already do, not a second implementation of it) plus every
scope whose semantic summary is currently `possibly_stale`/`stale`
project-wide (not only scopes touched by the current diff -- an agent
asking "what needs attention" wants the full backlog, not just today's
delta; `rune check`'s constraint list is already the "delta-only" view for
constraints specifically).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rune.core.project import RuneLayout
from rune.core.retrieval.check import check as run_check
from rune.core.storage.sqlite.materialize import connect_for_read


@dataclass(frozen=True)
class StaleSemanticSummary:
    scope_id: str
    status: str  # "possibly_stale" | "stale"


@dataclass(frozen=True)
class ChangesResult:
    changed_files: list[str] = field(default_factory=list)
    affected_scope_ids: list[str] = field(default_factory=list)
    stale_semantic: list[StaleSemanticSummary] = field(default_factory=list)


def changes(layout: RuneLayout) -> ChangesResult:
    check_result = run_check(layout)

    stale: list[StaleSemanticSummary] = []
    if layout.memory_db.exists():
        conn = connect_for_read(layout)
        try:
            rows = conn.execute(
                "SELECT scope_id, status FROM semantic_objects "
                "WHERE status IN ('possibly_stale', 'stale') ORDER BY scope_id"
            ).fetchall()
        finally:
            conn.close()
        stale = [StaleSemanticSummary(scope_id=row["scope_id"], status=row["status"]) for row in rows]

    return ChangesResult(
        changed_files=check_result.changed_files,
        affected_scope_ids=check_result.affected_scope_ids,
        stale_semantic=stale,
    )


__all__ = ["ChangesResult", "StaleSemanticSummary", "changes"]
