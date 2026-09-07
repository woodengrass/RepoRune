"""`rune related-context` / MCP `rune_related_context`: one call that
returns the most relevant Rune context for a task, instead of a pull-style
agent chaining `scope_for` -> `search` (decisions) -> `search` (constraints)
-> `search` (notes) itself.

Deliberately a *composition*, not new authoritative knowledge: every item
returned is produced by an existing, already-tested core function
(`scope_for`, `scope_decisions`, `core.retrieval.search.search`,
`symbol_search`, `core.memory.records.get_constraint`/`get_note`). Ranking
only re-orders/merges what those calls already returned; it does not
summarize, infer, or generate new text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rune.core.memory.records import RecordNotFoundError, get_note
from rune.core.memory.records import get_constraint as _get_constraint_revision
from rune.core.project import RuneLayout
from rune.core.retrieval.scope_for import (
    INJECTED_CONSTRAINT_SEVERITIES,
    ScopeForConstraint,
    ScopeForDecision,
    ScopeForNote,
    scope_decisions,
    scope_for,
)
from rune.core.retrieval.search import SearchResult, search
from rune.core.retrieval.symbol_search import SymbolSearchResult, symbol_search
from rune.core.storage.sqlite.materialize import connect_for_read

_ALL_BUCKETS = frozenset({"scopes", "constraints", "decisions", "notes", "semantic", "symbols"})


class RelatedContextValidationError(Exception):
    pass


@dataclass(frozen=True)
class RelatedScope:
    scope_id: str
    name: str
    description: str
    summary: str | None
    summary_status: str | None


@dataclass(frozen=True)
class RelatedContextResult:
    scopes: list[RelatedScope] = field(default_factory=list)
    constraints: list[ScopeForConstraint] = field(default_factory=list)
    decisions: list[ScopeForDecision] = field(default_factory=list)
    notes: list[ScopeForNote] = field(default_factory=list)
    semantic: list[RelatedScope] = field(default_factory=list)
    # `semantic` duplicates `scopes` shape (a scope *is* its semantic
    # summary here) -- kept as a separate field because the caller asked
    # for scopes/constraints/decisions/notes/semantic/symbols as distinct
    # buckets; every fresh-or-pointer summary in `scopes` also appears
    # here so a caller that only wants "what does this code do" doesn't
    # have to filter `scopes` itself.
    symbols: list[SymbolSearchResult] = field(default_factory=list)


def related_context(
    layout: RuneLayout,
    *,
    path: str | None = None,
    symbol: str | None = None,
    query: str | None = None,
    include: set[str] | None = None,
    max_items: int = 20,
) -> RelatedContextResult:
    """`include`, when given, restricts the buckets computed to a subset of
    `{"scopes", "constraints", "decisions", "notes", "semantic", "symbols"}`
    -- useful for a caller that only wants e.g. constraints, without paying
    for (or receiving) the rest. `max_items` caps each bucket
    independently (not a combined total): a caller asking for scopes AND
    symbols isn't penalized on one because the other happened to have many
    matches.

    Raises `RelatedContextValidationError` if none of `path`/`symbol`/
    `query` is given -- there's no way to decide what's "related" to
    nothing, and returning an arbitrary/empty result would silently hide
    that mistake from the caller.
    """
    if not path and not symbol and not query:
        raise RelatedContextValidationError("at least one of path, symbol, or query is required")
    wanted = include if include is not None else _ALL_BUCKETS

    scopes: dict[str, RelatedScope] = {}
    constraints: dict[str, ScopeForConstraint] = {}
    decisions: dict[str, ScopeForDecision] = {}
    notes: dict[str, ScopeForNote] = {}
    symbols: dict[str, SymbolSearchResult] = {}

    # One connection for every per-scope Decision lookup this call makes,
    # not one per scope encountered -- an earlier version opened a fresh
    # `connect_for_read` inside the per-scope loop below, which meant a
    # `symbol=` query touching N scopes across up to 5 symbol matches could
    # open N+ separate SQLite connections in one call. `scope_for`/
    # `symbol_search` still each manage their own connection internally
    # (bounded by the number of path/symbol lookups, not by scope count),
    # which is a smaller, harder-to-remove cost without changing their
    # public signature.
    decisions_conn = connect_for_read(layout) if layout.memory_db.exists() else None
    try:
        def _absorb_scope_for_path(file_path: str) -> None:
            for s in scope_for(layout, file_path):
                if s.scope_id in scopes:
                    continue
                scopes[s.scope_id] = RelatedScope(
                    scope_id=s.scope_id, name=s.name, description=s.description,
                    summary=s.summary, summary_status=s.summary_status,
                )
                for c in s.constraints:
                    constraints[c.record_id] = c
                for n in s.notes:
                    notes[n.id] = n
                if decisions_conn is not None:
                    for d in scope_decisions(decisions_conn, s.scope_id):
                        decisions[d.record_id] = d

        if path:
            _absorb_scope_for_path(path)
            if "symbols" in wanted:
                for sym in symbol_search(layout, path=path, limit=max_items):
                    symbols[sym.symbol_id] = sym

        if symbol:
            matches = symbol_search(layout, query=symbol, limit=5)
            for sym in matches:
                symbols[sym.symbol_id] = sym
                _absorb_scope_for_path(sym.file)

        if query:
            results = search(layout, query, limit=max_items * 4)
            for r in results:
                _absorb_search_result(layout, r, constraints, decisions, notes, scopes)
            if "symbols" in wanted:
                for sym in symbol_search(layout, query=query, limit=max_items):
                    symbols[sym.symbol_id] = sym
    finally:
        if decisions_conn is not None:
            decisions_conn.close()

    ordered_scopes = sorted(scopes.values(), key=lambda s: s.scope_id)[:max_items]
    ordered_constraints = sorted(
        constraints.values(), key=lambda c: (c.severity != "MUST", c.record_id)
    )[:max_items]
    ordered_decisions = sorted(decisions.values(), key=lambda d: d.record_id)[:max_items]
    ordered_notes = sorted(notes.values(), key=lambda n: n.id)[:max_items]
    ordered_symbols = sorted(symbols.values(), key=lambda s: (s.file, s.start_line))[:max_items]
    semantic = [s for s in ordered_scopes if s.summary is not None]

    return RelatedContextResult(
        scopes=ordered_scopes if "scopes" in wanted else [],
        constraints=ordered_constraints if "constraints" in wanted else [],
        decisions=ordered_decisions if "decisions" in wanted else [],
        notes=ordered_notes if "notes" in wanted else [],
        semantic=semantic if "semantic" in wanted else [],
        symbols=ordered_symbols if "symbols" in wanted else [],
    )


def _absorb_search_result(
    layout: RuneLayout,
    result: SearchResult,
    constraints: dict[str, ScopeForConstraint],
    decisions: dict[str, ScopeForDecision],
    notes: dict[str, ScopeForNote],
    scopes: dict[str, RelatedScope],
) -> None:
    """`core.retrieval.search.search`'s uniform `SearchResult` shape
    doesn't carry a constraint's real `severity` or a note's `category` --
    looked up here via the same `core.memory.records.get_constraint`/
    `get_note` the `rune_constraint_get`/`rune_note_get` MCP tools already
    use, rather than reverse-engineered from `SearchResult.rank` (an
    earlier version inferred MUST/SHOULD from the rank constant, which
    silently mislabeled INFO-severity constraints as SHOULD and could
    never have produced a real Note category at all). A constraint whose
    real severity is INFO is dropped here, matching
    `scope_for.INJECTED_CONSTRAINT_SEVERITIES` -- the same MUST/SHOULD-only
    rule the `path`/`symbol` paths already apply via `scope_constraints`,
    so a constraint's presence/absence in this bucket doesn't depend on
    which of `path`/`symbol`/`query` found it.
    """
    if result.kind == "constraint":
        try:
            current, _history = _get_constraint_revision(layout, result.id)
        except RecordNotFoundError:
            return
        severity = current.severity.value if current.severity is not None else None
        if severity not in INJECTED_CONSTRAINT_SEVERITIES:
            return
        constraints.setdefault(
            result.id,
            ScopeForConstraint(
                record_id=result.id, severity=severity, content=result.text,
                status=result.status, warning=result.warning,
            ),
        )
    elif result.kind == "decision":
        decisions.setdefault(
            result.id,
            ScopeForDecision(record_id=result.id, content=result.text, status=result.status, warning=result.warning),
        )
    elif result.kind == "note":
        try:
            note_current, _history = get_note(layout, result.id)
            category = note_current.category.value
        except RecordNotFoundError:
            category = "unknown"
        notes.setdefault(
            result.id,
            ScopeForNote(id=result.id, category=category, content=result.text, status=result.status, warning=result.warning),
        )
    elif result.kind == "semantic":
        scopes.setdefault(
            result.id,
            RelatedScope(scope_id=result.id, name=result.id, description="", summary=result.text, summary_status=result.status),
        )


__all__ = ["RelatedContextResult", "RelatedContextValidationError", "RelatedScope", "related_context"]
