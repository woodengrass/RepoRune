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
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from rune.core.project import RuneLayout
from rune.core.retrieval.search import possibly_stale_pointer
from rune.core.storage.sqlite.materialize import connect_for_read

_VISIBLE_CONSTRAINT_STATUSES = {"active", "review_required", "stale"}
_VISIBLE_NOTE_STATUSES = {"active", "stale"}
# ARCHITECTURE.md §7.1: only MUST/SHOULD are ever proactively injected;
# INFO-severity constraints are discoverable via `rune search` but don't
# justify spending context budget on every scope activation.
_INJECTED_CONSTRAINT_SEVERITIES = {"MUST", "SHOULD"}


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
        scope_rows = conn.execute(
            "SELECT s.id, s.name, s.description FROM scope_files sf "
            "JOIN scopes s ON s.id = sf.scope_id WHERE sf.file = ? ORDER BY s.id",
            (path,),
        ).fetchall()

        results: list[ScopeForScope] = []
        for scope_row in scope_rows:
            scope_id = scope_row["id"]

            summary_row = conn.execute(
                "SELECT purpose, status, payload_json FROM semantic_objects WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
            summary_text: str | None = None
            summary_status: str | None = None
            if summary_row is not None:
                summary_status = summary_row["status"]
                if summary_status == "fresh":
                    summary_text = summary_row["purpose"]
                elif summary_status in ("possibly_stale", "stale"):
                    payload = json.loads(summary_row["payload_json"])
                    summary_text = possibly_stale_pointer(list(payload.get("source_files", {})))

            constraint_rows = conn.execute(
                "SELECT r.record_id, v.severity, v.content, v.status "
                "FROM constraint_scopes cs "
                "JOIN constraint_records r ON r.record_id = cs.record_id "
                "JOIN constraint_revisions v "
                "  ON v.record_id = r.record_id AND v.revision = r.current_revision "
                "WHERE cs.scope_id = ? AND cs.revision = r.current_revision",
                (scope_id,),
            ).fetchall()
            constraints = [
                ScopeForConstraint(
                    record_id=row["record_id"], severity=row["severity"], content=row["content"],
                    status=row["status"],
                    warning=row["status"] if row["status"] in ("review_required", "stale") else None,
                )
                for row in constraint_rows
                if row["status"] in _VISIBLE_CONSTRAINT_STATUSES
                and row["severity"] in _INJECTED_CONSTRAINT_SEVERITIES
            ]
            constraints.sort(key=lambda c: (c.severity != "MUST", c.record_id))

            note_rows = conn.execute(
                "SELECT r.id, v.category, v.content, v.status "
                "FROM note_scopes ns "
                "JOIN note_records r ON r.id = ns.id "
                "JOIN note_revisions v ON v.id = r.id AND v.revision = r.current_revision "
                "WHERE ns.scope_id = ? AND ns.revision = r.current_revision",
                (scope_id,),
            ).fetchall()
            notes = [
                ScopeForNote(
                    id=row["id"], category=row["category"], content=row["content"],
                    status=row["status"], warning="[STALE]" if row["status"] == "stale" else None,
                )
                for row in note_rows
                if row["status"] in _VISIBLE_NOTE_STATUSES
            ]
            notes.sort(key=lambda n: n.id)

            results.append(
                ScopeForScope(
                    scope_id=scope_id, name=scope_row["name"], description=scope_row["description"],
                    summary=summary_text, summary_status=summary_status,
                    constraints=constraints, notes=notes,
                )
            )
        return results
    finally:
        conn.close()
