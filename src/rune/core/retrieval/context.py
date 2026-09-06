"""`rune bootstrap --mode hard|soft`: the two agent-injection payloads
ARCHITECTURE.md §7.3 splits the old single "project bootstrap context"
into. Hard bootstrap is re-injected on every `session.created` and
`session.compacted`; soft bootstrap is injected once per session. Both
return structured data only -- rendering into adapter-specific prompt
text is the adapter's job (ARCHITECTURE.md §6/§7.6).

Token counts here are a `len(text) // 4` heuristic, not a real
tokenizer call -- good enough to decide "did we blow the budget",
not meant to match any specific model's actual tokenization.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rune.core.config import load_config
from rune.core.project import RuneLayout
from rune.core.status import compute_status
from rune.core.storage.sqlite.materialize import connect_for_read

# ARCHITECTURE.md §7.4: only current+visible global MUST constraints go
# into hard bootstrap -- same visible-status set `rune check`'s global
# MUST query already uses.
_GLOBAL_MUST_VISIBLE = ("active", "review_required", "stale")
# ARCHITECTURE.md §7.5/§7.3: only current+visible, non-critical global
# Decisions go into soft bootstrap; critical ones go into hard bootstrap
# instead (mutually exclusive by `critical`, not a duplication).
_DECISION_VISIBLE = ("active", "review_required")


def estimate_tokens(text: str) -> int:
    """`len(text) // 4` heuristic (roughly 4 chars/token for English
    prose) -- there's no real tokenizer dependency in this codebase, and
    hard bootstrap's overflow check only needs to be roughly right, never
    exact (ARCHITECTURE.md §7.7: an exact miscount can't silently drop a
    MUST rule either way, since overflow only ever adds a flag, never a
    truncation).
    """
    return (len(text) + 3) // 4


@dataclass(frozen=True)
class HardBootstrapConstraint:
    record_id: str
    severity: str
    content: str
    source_document: str | None
    source_section: str | None


@dataclass(frozen=True)
class HardBootstrapDecision:
    record_id: str
    content: str


@dataclass(frozen=True)
class HardBootstrapContext:
    constraints: list[HardBootstrapConstraint] = field(default_factory=list)
    decisions: list[HardBootstrapDecision] = field(default_factory=list)
    estimated_tokens: int = 0
    budget_tokens: int = 3000
    overflow: bool = False


def build_hard_bootstrap(layout: RuneLayout) -> HardBootstrapContext:
    config = load_config(layout.config_path)
    budget = config.bootstrap.hard_budget_tokens

    if not layout.memory_db.exists():
        return HardBootstrapContext(budget_tokens=budget)

    conn = connect_for_read(layout)
    try:
        constraint_rows = conn.execute(
            "SELECT r.record_id, v.severity, v.content, v.source_document, v.source_section "
            "FROM constraint_records r "
            "JOIN constraint_revisions v "
            "  ON v.record_id = r.record_id AND v.revision = r.current_revision "
            "WHERE v.severity = 'MUST' AND v.persistence_mode = 'persistent' "
            f"  AND v.status IN {_GLOBAL_MUST_VISIBLE} "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM constraint_scopes cs "
            "    WHERE cs.record_id = r.record_id AND cs.revision = r.current_revision"
            "  )"
        ).fetchall()
        constraints = [
            HardBootstrapConstraint(
                record_id=row["record_id"],
                severity=row["severity"],
                content=row["content"],
                source_document=row["source_document"],
                source_section=row["source_section"],
            )
            for row in constraint_rows
        ]
        constraints.sort(key=lambda c: c.record_id)

        decision_rows = conn.execute(
            "SELECT r.record_id, v.content FROM decision_records r "
            "JOIN decision_revisions v "
            "  ON v.record_id = r.record_id AND v.revision = r.current_revision "
            f"WHERE v.critical = 1 AND v.status IN {_DECISION_VISIBLE} "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM decision_scopes ds "
            "    WHERE ds.record_id = r.record_id AND ds.revision = r.current_revision"
            "  )"
        ).fetchall()
        decisions = [
            HardBootstrapDecision(record_id=row["record_id"], content=row["content"])
            for row in decision_rows
        ]
        decisions.sort(key=lambda d: d.record_id)
    finally:
        conn.close()

    estimated = sum(estimate_tokens(c.content) for c in constraints) + sum(
        estimate_tokens(d.content) for d in decisions
    )
    return HardBootstrapContext(
        constraints=constraints,
        decisions=decisions,
        estimated_tokens=estimated,
        budget_tokens=budget,
        overflow=estimated > budget,
    )


@dataclass(frozen=True)
class SoftBootstrapScope:
    scope_id: str
    name: str
    description: str
    summary: str | None


@dataclass(frozen=True)
class SoftBootstrapDecision:
    record_id: str
    content: str


@dataclass(frozen=True)
class SoftBootstrapContext:
    project_name: str | None = None
    working_tree_fresh: bool | None = None
    files_indexed: int = 0
    symbols_indexed: int = 0
    scopes: list[SoftBootstrapScope] = field(default_factory=list)
    decisions: list[SoftBootstrapDecision] = field(default_factory=list)
    estimated_tokens: int = 0
    budget_tokens: int = 8000
    overflow: bool = False


def build_soft_bootstrap(layout: RuneLayout) -> SoftBootstrapContext:
    """Project overview, main scope list with summaries, memory freshness
    (reuses `compute_status` -- the exact same computation `rune status`
    uses, not a second subtly different one), and the remaining
    (non-critical) active global Decisions.

    Deliberately omits "近期相關變更" (recent changes) from ARCHITECTURE
    §7.3's soft bootstrap description: there's no defined data source for
    it anywhere in the governance docs (no "recent changes" query exists
    in `core.retrieval`), so this is a scope reduction rather than an
    oversight -- `rune check`'s working-tree diff already covers the one
    concrete "what changed" need this project has defined so far.
    """
    config = load_config(layout.config_path)
    budget = config.bootstrap.soft_budget_tokens

    status = compute_status(layout)
    project_name = status.name if status is not None else None
    working_tree_fresh = status.working_tree_fresh if status is not None else None
    files_indexed = status.files_indexed if status is not None else 0
    symbols_indexed = status.symbols_indexed if status is not None else 0

    if not layout.memory_db.exists():
        return SoftBootstrapContext(
            project_name=project_name,
            working_tree_fresh=working_tree_fresh,
            files_indexed=files_indexed,
            symbols_indexed=symbols_indexed,
            budget_tokens=budget,
        )

    conn = connect_for_read(layout)
    try:
        scope_rows = conn.execute(
            "SELECT s.id, s.name, s.description, so.purpose, so.status "
            "FROM scopes s LEFT JOIN semantic_objects so ON so.scope_id = s.id "
            "ORDER BY s.id"
        ).fetchall()
        scopes = [
            SoftBootstrapScope(
                scope_id=row["id"],
                name=row["name"],
                description=row["description"],
                summary=row["purpose"] if row["status"] == "fresh" else None,
            )
            for row in scope_rows
        ]

        decision_rows = conn.execute(
            "SELECT r.record_id, v.content FROM decision_records r "
            "JOIN decision_revisions v "
            "  ON v.record_id = r.record_id AND v.revision = r.current_revision "
            f"WHERE v.critical = 0 AND v.status IN {_DECISION_VISIBLE} "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM decision_scopes ds "
            "    WHERE ds.record_id = r.record_id AND ds.revision = r.current_revision"
            "  )"
        ).fetchall()
        decisions = [
            SoftBootstrapDecision(record_id=row["record_id"], content=row["content"])
            for row in decision_rows
        ]
        decisions.sort(key=lambda d: d.record_id)
    finally:
        conn.close()

    estimated = sum(
        estimate_tokens(s.summary or s.description) for s in scopes
    ) + sum(estimate_tokens(d.content) for d in decisions)
    return SoftBootstrapContext(
        project_name=project_name,
        working_tree_fresh=working_tree_fresh,
        files_indexed=files_indexed,
        symbols_indexed=symbols_indexed,
        scopes=scopes,
        decisions=decisions,
        estimated_tokens=estimated,
        budget_tokens=budget,
        overflow=estimated > budget,
    )
