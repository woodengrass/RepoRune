from __future__ import annotations

import sqlite3
from typing import cast

import pytest
from pydantic import ValidationError

from rune.core.project import RuneLayout
from rune.core.scopes.clustering import suggest_from_graph
from rune.core.scopes.heuristics import suggest_from_paths
from rune.core.scopes.model import (
    assign_new_files_from_imports,
    create_scope,
    delete_scope,
    load_scopes,
    set_scope_locked,
    update_scope,
)
from rune.core.storage.models import (
    Edge,
    EdgeType,
    Scope,
    ScopeMembers,
    ScopesFile,
    ScopeSource,
)


def test_scope_crud_preserves_sorted_unique_membership(git_repo) -> None:
    layout = RuneLayout(git_repo)
    created = create_scope(
        layout, "auth", "Auth", files=["b.py", "a.py", "a.py"], symbols=["s2", "s1"],
    )
    assert created.locked is True
    assert created.members.files == ["a.py", "b.py"]

    updated = update_scope(
        layout, "auth", name="Authentication", add_files=["c.py"], remove_files=["b.py"]
    )
    assert updated.name == "Authentication"
    assert updated.members.files == ["a.py", "c.py"]
    assert set_scope_locked(layout, "auth", False).locked is False
    delete_scope(layout, "auth")
    assert load_scopes(layout).scopes == []


def test_incremental_assignment_requires_one_unlocked_import_target() -> None:
    scopes = ScopesFile(
        scopes=[
            Scope(
                id="app", name="App", locked=False, source=ScopeSource.model,
                members=ScopeMembers(files=["app/services.py"]),
            ),
            Scope(
                id="locked", name="Locked", locked=True, source=ScopeSource.human,
                members=ScopeMembers(files=["app/locked.py"]),
            ),
        ]
    )
    edges = [
        Edge(
            source_file="app/new.py", target_file="app/services.py",
            edge_type=EdgeType.imports, confidence=1.0,
        ),
        Edge(
            source_file="app/locked-new.py", target_file="app/locked.py",
            edge_type=EdgeType.imports, confidence=1.0,
        ),
    ]
    changed = assign_new_files_from_imports(
        scopes, {"app/new.py", "app/locked-new.py"}, edges, []
    )
    assert changed == ["app"]
    assert scopes.scopes[0].members.files == ["app/new.py", "app/services.py"]
    assert scopes.scopes[1].members.files == ["app/locked.py"]


def test_incremental_assignment_rejects_ambiguous_and_reference_only_matches() -> None:
    scopes = ScopesFile(
        scopes=[
            Scope(id="one", name="One", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["one.py"])),
            Scope(id="two", name="Two", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["two.py"])),
        ]
    )
    edges = [
        Edge(source_file="ambiguous.py", target_file="one.py", edge_type=EdgeType.imports),
        Edge(source_file="ambiguous.py", target_file="two.py", edge_type=EdgeType.imports),
        Edge(source_file="reference.py", target_file="one.py", edge_type=EdgeType.calls, confidence=0.8),
    ]
    assert assign_new_files_from_imports(scopes, {"ambiguous.py", "reference.py"}, edges, []) == []
    assert all("ambiguous.py" not in scope.members.files for scope in scopes.scopes)
    assert all("reference.py" not in scope.members.files for scope in scopes.scopes)


def test_set_scope_locked_rejects_invalid_value_without_poisoning_canonical(git_repo) -> None:
    """Bare `model_copy` skips Pydantic validators, so a bad `locked` value
    would be written straight into scopes.json and break every later load
    (same bug class as notes.py's `validated_copy` fix). Must fail here.
    """
    layout = RuneLayout(git_repo)
    create_scope(layout, "auth", "Auth")
    # cast: deliberately smuggle a bad runtime value past the checker --
    # runtime validation (validated_copy) must reject it.
    with pytest.raises(ValidationError):
        set_scope_locked(layout, "auth", cast(bool, "not-a-bool"))
    # Canonical untouched: still loads, still locked.
    assert load_scopes(layout).scopes[0].locked is True


def test_incremental_assignment_preserves_existing_symbols() -> None:
    scopes = ScopesFile(
        scopes=[
            Scope(
                id="app", name="App", locked=False, source=ScopeSource.model,
                members=ScopeMembers(files=["app/services.py"], symbols=["app/services.py:svc:function"]),
            ),
        ]
    )
    edges = [
        Edge(
            source_file="app/new.py", target_file="app/services.py",
            edge_type=EdgeType.imports, confidence=1.0,
        ),
    ]
    assert assign_new_files_from_imports(scopes, {"app/new.py"}, edges, []) == ["app"]
    assert scopes.scopes[0].members.files == ["app/new.py", "app/services.py"]
    assert scopes.scopes[0].members.symbols == ["app/services.py:svc:function"]


def test_graph_suggestions_assign_unique_ids_without_common_parent() -> None:
    """Two disconnected root-level components share no parent directory, so
    the old constant "connected-component" fallback gave both the same id.
    Fallback labels must derive from each component's own files.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE edges (source_file TEXT, target_file TEXT, edge_type TEXT)")
    conn.executemany(
        "INSERT INTO edges VALUES (?, ?, ?)",
        [("aaa.py", "aab.py", "imports"), ("zzz.py", "zzq.py", "calls")],
    )
    candidates = suggest_from_graph(conn, set())
    assert len(candidates) == 2
    assert candidates[0].id != candidates[1].id
    assert {candidates[0].files, candidates[1].files} == {
        ("aaa.py", "aab.py"), ("zzq.py", "zzz.py"),
    }


def test_path_and_graph_suggestions_exclude_locked_members() -> None:
    assert suggest_from_paths(["app/a.py", "app/b.py", "locked/a.py"], {"locked/a.py"}) == [
        suggest_from_paths(["app/a.py", "app/b.py"])[0]
    ]

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE edges (source_file TEXT, target_file TEXT, edge_type TEXT)")
    conn.executemany(
        "INSERT INTO edges VALUES (?, ?, ?)",
        [("app/a.py", "app/b.py", "imports"), ("locked/a.py", "app/a.py", "calls")],
    )
    candidates = suggest_from_graph(conn, {"locked/a.py"})
    assert len(candidates) == 1
    assert candidates[0].files == ("app/a.py", "app/b.py")
