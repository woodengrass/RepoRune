from __future__ import annotations

from pathlib import Path

from rune.core.project import init_project
from rune.core.retrieval.symbol_search import symbol_search
from rune.core.update import run_update


def test_symbol_search_missing_cache_returns_empty(git_repo: Path) -> None:
    layout = init_project(git_repo)
    assert symbol_search(layout, query="anything") == []


def test_symbol_search_by_query_matches_qualified_name(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    results = symbol_search(layout, query="get_user")
    assert any(r.qualified_name == "UserService.get_user" for r in results)


def test_symbol_search_by_name_exact_field(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    results = symbol_search(layout, name="get_user")
    assert results and all(r.name == "get_user" for r in results)


def test_symbol_search_by_kind(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    results = symbol_search(layout, kind="class")
    assert results and all(r.kind == "class" for r in results)
    assert any(r.name == "UserService" for r in results)


def test_symbol_search_by_path_filter(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    results = symbol_search(layout, path="services.py")
    assert results and all("services.py" in r.file for r in results)


def test_symbol_search_no_filters_returns_everything_up_to_limit(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    all_symbols = symbol_search(layout, limit=1000)
    limited = symbol_search(layout, limit=1)
    assert len(limited) == 1
    assert len(all_symbols) >= len(limited)
