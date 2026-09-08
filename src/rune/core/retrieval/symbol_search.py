"""`rune symbol-search` / MCP `rune_symbol_search`: structured lookup over
the deterministic code index's `symbols` table -- the counterpart to
`core.retrieval.search`'s free-text search over prose (Decision/
Constraint/Note/semantic-summary content), which deliberately does not
also search symbols (a symbol lookup is a structured field match --
name/qualified_name/kind/path -- not a phrase match over prose).

`query`, when given, matches against `fts_symbols` (qualified_name +
signature, populated by `materialize.py`) the same way `core.retrieval.
search` does for prose. The structured filters (`name`/`qualified_name`/
`kind`/`path`) are plain SQL `LIKE`/`=` filters and can be combined with
`query` or used alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from rune.core.project import RuneLayout
from rune.core.retrieval.search import fts_phrase
from rune.core.storage.sqlite.materialize import connect_for_read


@dataclass(frozen=True)
class SymbolSearchResult:
    symbol_id: str
    file: str
    name: str
    qualified_name: str
    kind: str
    signature: str | None
    start_line: int
    end_line: int


def symbol_search(
    layout: RuneLayout,
    *,
    query: str | None = None,
    name: str | None = None,
    qualified_name: str | None = None,
    kind: str | None = None,
    path: str | None = None,
    limit: int = 50,
) -> list[SymbolSearchResult]:
    """Every filter given must match (AND); `query` matches full-text
    against qualified_name+signature, `name`/`qualified_name`/`path` are
    case-sensitive substring (`LIKE '%...%'`) matches, `kind` is exact.
    Returns `[]` (not an error) if `memory.db` doesn't exist yet or no
    filter at all narrows the result -- callers wanting "everything" can
    pass no filters, bounded by `limit`.
    """
    if not layout.memory_db.exists():
        return []
    if limit < 0:
        raise ValueError("limit must be non-negative")
    conn = connect_for_read(layout)
    try:
        clauses: list[str] = []
        params: list[str | int] = []
        base = "SELECT symbol_id, file, name, qualified_name, kind, signature, start_line, end_line FROM symbols"
        if query:
            base = (
                "SELECT s.symbol_id, s.file, s.name, s.qualified_name, s.kind, s.signature, "
                "       s.start_line, s.end_line "
                "FROM fts_symbols f JOIN symbols s ON s.symbol_id = f.symbol_id"
            )
            clauses.append("fts_symbols MATCH ?")
            params.append(fts_phrase(query))
        if name:
            clauses.append("s.name LIKE ? ESCAPE '\\'" if query else "name LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(name)}%")
        if qualified_name:
            clauses.append("s.qualified_name LIKE ? ESCAPE '\\'" if query else "qualified_name LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(qualified_name)}%")
        if kind:
            clauses.append("s.kind = ?" if query else "kind = ?")
            params.append(kind)
        if path:
            clauses.append("s.file LIKE ? ESCAPE '\\'" if query else "file LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(path)}%")

        sql = base
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY " + ("s.file, s.start_line" if query else "file, start_line") + " LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [
            SymbolSearchResult(
                symbol_id=row["symbol_id"], file=row["file"], name=row["name"],
                qualified_name=row["qualified_name"], kind=row["kind"],
                signature=row["signature"], start_line=row["start_line"], end_line=row["end_line"],
            )
            for row in rows
        ]
    finally:
        conn.close()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = ["SymbolSearchResult", "symbol_search"]
