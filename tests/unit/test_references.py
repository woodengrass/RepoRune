from __future__ import annotations

import sqlite3

from rune.core.index.references import (
    find_referencing_edges,
    group_symbols_by_path,
    resolve_references,
)
from rune.core.index.treesitter import RawReference
from rune.core.storage.models import EdgeType, Symbol, SymbolKind


def _symbol(
    file: str, name: str, qualified_name: str, kind: SymbolKind, start: int, end: int
) -> Symbol:
    return Symbol(
        symbol_id=f"{file}:{qualified_name}:{kind.value}",
        file=file,
        name=name,
        qualified_name=qualified_name,
        kind=kind,
        signature=None,
        start_line=start,
        end_line=end,
    )


def test_resolve_references_matches_local_symbol_first() -> None:
    local_helper = _symbol("a.py", "helper", "helper", SymbolKind.function, 10, 12)
    caller = _symbol("a.py", "run", "run", SymbolKind.function, 1, 5)
    symbols_by_path = {"a.py": [local_helper, caller]}

    edges = resolve_references(
        "a.py",
        [RawReference(name="helper", edge_type=EdgeType.calls, line=3)],
        symbols_by_path,
        imported_files=set(),
    )

    assert len(edges) == 1
    assert edges[0].target_symbol == local_helper.symbol_id
    assert edges[0].target_file == "a.py"
    assert edges[0].source_symbol == caller.symbol_id
    assert edges[0].confidence == 0.8


def test_resolve_references_ambiguous_imported_match_is_deterministic_across_processes() -> None:
    """Regression test: when a name matches symbols in *two or more*
    imported files, the choice must not depend on Python's `set`
    iteration order for strings, which is randomized per-*process*
    (PYTHONHASHSEED) — a within-process loop would not catch this, since
    CPython's hash caching keeps iteration order stable for the life of
    one process. Confirmed empirically before fixing: running the
    resolution below in separate `python -c` invocations with different
    hash seeds produced two different answers for the exact same input.
    This test reproduces that directly by forcing several different seeds
    via subprocess and asserting they all agree.
    """
    import subprocess
    import sys

    script = (
        "from rune.core.index.references import resolve_references\n"
        "from rune.core.index.treesitter import RawReference\n"
        "from rune.core.storage.models import EdgeType, Symbol, SymbolKind\n"
        "def sym(f, n):\n"
        "    return Symbol(symbol_id=f+n, file=f, name=n, qualified_name=n, "
        "kind=SymbolKind.function, signature=None, start_line=1, end_line=2)\n"
        "symbols_by_path = {'a.py': [], 'b.py': [sym('b.py','helper')], "
        "'z.py': [sym('z.py','helper')]}\n"
        "edges = resolve_references('a.py', "
        "[RawReference(name='helper', edge_type=EdgeType.calls, line=1)], "
        "symbols_by_path, imported_files={'z.py', 'b.py'})\n"
        "print(edges[0].target_file)\n"
    )

    import os

    results = set()
    for seed in ("0", "1", "2", "3", "4"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        results.add(result.stdout.strip())

    assert results == {"b.py"}, (
        f"resolution must be identical across hash seeds, got: {results}"
    )


def test_resolve_references_falls_back_to_imported_file() -> None:
    caller = _symbol("a.py", "run", "run", SymbolKind.function, 1, 5)
    remote_helper = _symbol("b.py", "helper", "helper", SymbolKind.function, 1, 2)
    symbols_by_path = {"a.py": [caller], "b.py": [remote_helper]}

    edges = resolve_references(
        "a.py",
        [RawReference(name="helper", edge_type=EdgeType.calls, line=3)],
        symbols_by_path,
        imported_files={"b.py"},
    )

    assert edges[0].target_symbol == remote_helper.symbol_id
    assert edges[0].target_file == "b.py"
    assert edges[0].confidence == 0.6


def test_resolve_references_unresolved_is_recorded_not_dropped() -> None:
    caller = _symbol("a.py", "run", "run", SymbolKind.function, 1, 5)
    symbols_by_path = {"a.py": [caller]}

    edges = resolve_references(
        "a.py",
        [RawReference(name="totally_unknown", edge_type=EdgeType.calls, line=3)],
        symbols_by_path,
        imported_files=set(),
    )

    assert len(edges) == 1
    assert edges[0].target_symbol is None
    assert edges[0].target_file is None
    assert edges[0].confidence == 0.3
    assert edges[0].source_symbol == caller.symbol_id


def test_resolve_references_extends_source_symbol_is_the_class_itself() -> None:
    cls = _symbol("a.py", "Foo", "Foo", SymbolKind.class_, 5, 10)
    base = _symbol("a.py", "Base", "Base", SymbolKind.class_, 1, 3)
    symbols_by_path = {"a.py": [cls, base]}

    edges = resolve_references(
        "a.py",
        [RawReference(name="Base", edge_type=EdgeType.extends, line=5)],
        symbols_by_path,
        imported_files=set(),
    )

    assert edges[0].source_symbol == cls.symbol_id
    assert edges[0].target_symbol == base.symbol_id


def test_resolve_references_extends_does_not_match_function_symbols() -> None:
    """An `extends` reference's source_symbol must come from class/
    interface symbols only, never a function/method whose range happens
    to overlap the same line -- classes and functions use disjoint kind
    sets when searching for the enclosing symbol.
    """
    cls = _symbol("a.py", "Foo", "Foo", SymbolKind.class_, 1, 10)
    edges = resolve_references(
        "a.py",
        [RawReference(name="Base", edge_type=EdgeType.extends, line=1)],
        {"a.py": [cls]},
        imported_files=set(),
    )
    assert edges[0].source_symbol == cls.symbol_id


def test_resolve_references_extends_target_ignores_same_named_function() -> None:
    """Regression test, distinct from
    `test_resolve_references_extends_does_not_match_function_symbols`
    above (that one only constrains which symbol counts as the *source*
    of an extends/implements edge -- it never actually put a same-named
    function into `symbols_by_path`, so it could not have caught this).
    This one covers the *target* side: `class Foo(Base):` where `Base` is
    coincidentally also the name of a function or variable in scope must
    not resolve `target_symbol` to that function/variable -- a same-named
    non-type symbol is not the base class, and confidently matching it
    would be worse than leaving the reference unresolved.
    """
    base_fn = _symbol("a.py", "Base", "Base", SymbolKind.function, 1, 2)
    cls = _symbol("a.py", "Foo", "Foo", SymbolKind.class_, 4, 5)
    edges = resolve_references(
        "a.py",
        [RawReference(name="Base", edge_type=EdgeType.extends, line=4)],
        {"a.py": [base_fn, cls]},
        imported_files=set(),
    )
    assert edges[0].target_symbol is None
    assert edges[0].target_file is None
    assert edges[0].confidence == 0.3


def test_resolve_references_implements_target_ignores_same_named_variable_in_imported_file() -> None:
    """Same as above but for the imported-file match path (confidence 0.6)
    and `implements` rather than `extends`: a same-named constant in an
    imported file must not satisfy an `implements` reference either.
    """
    fake_shape = _symbol("b.py", "Shape", "Shape", SymbolKind.constant, 1, 1)
    edges = resolve_references(
        "a.py",
        [RawReference(name="Shape", edge_type=EdgeType.implements, line=1)],
        {"a.py": [], "b.py": [fake_shape]},
        imported_files={"b.py"},
    )
    assert edges[0].target_symbol is None
    assert edges[0].target_file is None
    assert edges[0].confidence == 0.3


def test_resolve_references_nested_class_picks_innermost_as_source() -> None:
    outer = _symbol("a.py", "Outer", "Outer", SymbolKind.class_, 1, 20)
    inner = _symbol("a.py", "Inner", "Outer.Inner", SymbolKind.class_, 5, 8)
    symbols_by_path = {"a.py": [outer, inner]}

    edges = resolve_references(
        "a.py",
        [RawReference(name="Base", edge_type=EdgeType.extends, line=5)],
        symbols_by_path,
        imported_files=set(),
    )
    assert edges[0].source_symbol == inner.symbol_id


def test_group_symbols_by_path() -> None:
    a = _symbol("a.py", "x", "x", SymbolKind.function, 1, 2)
    b = _symbol("b.py", "y", "y", SymbolKind.function, 1, 2)
    grouped = group_symbols_by_path([a, b])
    assert grouped == {"a.py": [a], "b.py": [b]}


def test_find_referencing_edges_queries_by_target_symbol() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE edges (source_symbol TEXT, source_file TEXT, "
        "target_symbol TEXT, target_file TEXT, edge_type TEXT, confidence REAL)"
    )
    conn.execute(
        "INSERT INTO edges VALUES ('s1', 'a.py', 't1', 'b.py', 'calls', 0.8)"
    )
    conn.execute(
        "INSERT INTO edges VALUES ('s2', 'a.py', 'other', 'b.py', 'calls', 0.8)"
    )
    rows = find_referencing_edges(conn, "t1")
    assert len(rows) == 1
    assert rows[0]["source_symbol"] == "s1"
