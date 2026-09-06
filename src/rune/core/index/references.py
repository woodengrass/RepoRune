"""Best-effort resolution of call/inheritance references to symbols.

ARCHITECTURE.md §4.3: `imports` are high confidence (a real import
statement was parsed, regardless of whether the target resolves).
`references`/`calls`/`extends`/`implements` are the opposite: purely
syntactic name matching with no type inference or binding resolution, so
confidence varies and a name that can't be matched to any known symbol is
still recorded (target_symbol=None) rather than dropped or treated as an
error. Downstream consumers (Scope/Constraint in later milestones) must
not depend on this graph being complete — see the resilience test in
tests/integration/test_update_flow.py proving `rune update` still works
when reference resolution is entirely empty.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from rune.core.index.treesitter import RawReference
from rune.core.storage.models import Edge, EdgeType, Symbol, SymbolKind

_CALLABLE_KINDS = {SymbolKind.function, SymbolKind.method}
_TYPE_KINDS = {SymbolKind.class_, SymbolKind.interface}

# Confidence bands (best-effort, not a precise probability): a name that
# matches a symbol declared in the same file is much more likely to be the
# actual target than one matched in an imported file (could collide with
# an unrelated same-named symbol in a third file we didn't check), which
# in turn is far more trustworthy than "recorded but couldn't match
# anything at all" (could be a builtin, a dynamically-created attribute,
# or a name defined somewhere resolution didn't look).
_CONFIDENCE_LOCAL_MATCH = 0.8
_CONFIDENCE_IMPORTED_MATCH = 0.6
_CONFIDENCE_UNRESOLVED = 0.3


def _find_enclosing_symbol(
    symbols_in_file: list[Symbol], line: int, kinds: set[SymbolKind]
) -> Symbol | None:
    """The innermost symbol of one of `kinds` whose line range contains
    `line` — used as the reference edge's source_symbol. Picking the
    smallest range handles nested classes correctly (an inner class's own
    `extends` reference belongs to the inner class, not the outer one).
    """
    candidates = [
        s for s in symbols_in_file if s.kind in kinds and s.start_line <= line <= s.end_line
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: s.end_line - s.start_line)


def resolve_references(
    file_path: str,
    raw_references: list[RawReference],
    symbols_by_path: dict[str, list[Symbol]],
    imported_files: set[str],
) -> list[Edge]:
    """Turns one file's RawReferences into Edge rows. `symbols_by_path`
    must reflect the *current* run's complete symbol set (including
    reused-but-unchanged files) so cross-file resolution sees files that
    weren't re-parsed this time.
    """
    local_symbols = symbols_by_path.get(file_path, [])
    edges: list[Edge] = []

    for ref in raw_references:
        target_symbol: str | None = None
        target_file: str | None = None
        confidence = _CONFIDENCE_UNRESOLVED

        local_matches = [s for s in local_symbols if s.name == ref.name]
        if local_matches:
            target_symbol = local_matches[0].symbol_id
            target_file = file_path
            confidence = _CONFIDENCE_LOCAL_MATCH
        else:
            # Sorted, not iterated directly over the set: CPython's set
            # iteration order for strings depends on the process's hash
            # seed, which is randomized per-process by default. Without
            # this, an ambiguous name matching symbols in two+ imported
            # files could resolve to a *different* target across separate
            # `rune update` runs on the exact same source — breaking the
            # "same state in, same result out" guarantee the rest of this
            # project relies on (verified: it actually did, across 5
            # separate process invocations, before this fix).
            for imported_file in sorted(imported_files):
                candidates = [s for s in symbols_by_path.get(imported_file, []) if s.name == ref.name]
                if candidates:
                    target_symbol = candidates[0].symbol_id
                    target_file = imported_file
                    confidence = _CONFIDENCE_IMPORTED_MATCH
                    break

        source_kinds = (
            _TYPE_KINDS if ref.edge_type in (EdgeType.extends, EdgeType.implements) else _CALLABLE_KINDS
        )
        source_symbol_obj = _find_enclosing_symbol(local_symbols, ref.line, source_kinds)

        edges.append(
            Edge(
                source_symbol=source_symbol_obj.symbol_id if source_symbol_obj else None,
                source_file=file_path,
                target_symbol=target_symbol,
                target_file=target_file,
                edge_type=ref.edge_type,
                confidence=confidence,
            )
        )
    return edges


def group_symbols_by_path(symbols: list[Symbol]) -> dict[str, list[Symbol]]:
    by_path: dict[str, list[Symbol]] = defaultdict(list)
    for s in symbols:
        by_path[s.file].append(s)
    return by_path


def find_referencing_edges(conn: sqlite3.Connection, symbol_id: str) -> list[sqlite3.Row]:
    """"Who references symbol X" query helper (Milestone 3 deliverable),
    built on the plain `edges` table — no dedicated retrieval layer exists
    yet (that's Milestone 6's `core.retrieval`).
    """
    return conn.execute(
        "SELECT * FROM edges WHERE target_symbol = ? ORDER BY source_file, source_symbol",
        (symbol_id,),
    ).fetchall()
