"""Canonical Scope CRUD and safe incremental membership assignment."""

from __future__ import annotations

from collections.abc import Iterable

from rune.core.project import RuneLayout
from rune.core.storage.canonical import read_json_model, write_json_model
from rune.core.storage.models import (
    Edge,
    EdgeType,
    Scope,
    ScopeMembers,
    ScopesFile,
    ScopeSource,
    Symbol,
)


class ScopeNotFoundError(Exception):
    pass


class ScopeAlreadyExistsError(Exception):
    pass


def load_scopes(layout: RuneLayout) -> ScopesFile:
    return read_json_model(layout.scopes_json, ScopesFile) or ScopesFile()


def save_scopes(layout: RuneLayout, scopes_file: ScopesFile) -> None:
    write_json_model(layout.scopes_json, scopes_file)


def get_scope(scopes_file: ScopesFile, scope_id: str) -> Scope:
    for scope in scopes_file.scopes:
        if scope.id == scope_id:
            return scope
    raise ScopeNotFoundError(f"Scope {scope_id!r} does not exist.")


def create_scope(
    layout: RuneLayout,
    scope_id: str,
    name: str,
    description: str = "",
    files: Iterable[str] = (),
    symbols: Iterable[str] = (),
    source: ScopeSource = ScopeSource.human,
    locked: bool = True,
) -> Scope:
    scopes_file = load_scopes(layout)
    if any(scope.id == scope_id for scope in scopes_file.scopes):
        raise ScopeAlreadyExistsError(f"Scope {scope_id!r} already exists.")
    scope = Scope(
        id=scope_id,
        name=name,
        description=description,
        locked=locked,
        source=source,
        members=ScopeMembers(files=sorted(set(files)), symbols=sorted(set(symbols))),
    )
    scopes_file.scopes.append(scope)
    save_scopes(layout, scopes_file)
    return scope


def update_scope(
    layout: RuneLayout,
    scope_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    add_files: Iterable[str] = (),
    remove_files: Iterable[str] = (),
    add_symbols: Iterable[str] = (),
    remove_symbols: Iterable[str] = (),
) -> Scope:
    scopes_file = load_scopes(layout)
    scope = get_scope(scopes_file, scope_id)
    files = (set(scope.members.files) | set(add_files)) - set(remove_files)
    symbols = (set(scope.members.symbols) | set(add_symbols)) - set(remove_symbols)
    updated = scope.model_copy(
        update={
            "name": name if name is not None else scope.name,
            "description": description if description is not None else scope.description,
            "members": ScopeMembers(files=sorted(files), symbols=sorted(symbols)),
        }
    )
    scopes_file.scopes[scopes_file.scopes.index(scope)] = updated
    save_scopes(layout, scopes_file)
    return updated


def set_scope_locked(layout: RuneLayout, scope_id: str, locked: bool) -> Scope:
    scopes_file = load_scopes(layout)
    scope = get_scope(scopes_file, scope_id)
    updated = scope.model_copy(update={"locked": locked})
    scopes_file.scopes[scopes_file.scopes.index(scope)] = updated
    save_scopes(layout, scopes_file)
    return updated


def delete_scope(layout: RuneLayout, scope_id: str) -> None:
    scopes_file = load_scopes(layout)
    get_scope(scopes_file, scope_id)
    scopes_file.scopes = [scope for scope in scopes_file.scopes if scope.id != scope_id]
    save_scopes(layout, scopes_file)


def assign_new_files_from_imports(
    scopes_file: ScopesFile,
    added_paths: set[str],
    edges: Iterable[Edge],
    symbols: Iterable[Symbol],
) -> list[str]:
    """Adds a new file only when high-confidence imports identify one unlocked scope.

    This is intentionally stricter than suggestions: it is the only scope write
    path without human confirmation, so reference edges and ambiguous targets are
    excluded (ARCHITECTURE.md §4.4).
    """
    symbol_files = {symbol.symbol_id: symbol.file for symbol in symbols}
    member_files_by_scope = {
        scope.id: set(scope.members.files)
        | {symbol_files[symbol] for symbol in scope.members.symbols if symbol in symbol_files}
        for scope in scopes_file.scopes
        if not scope.locked
    }
    changed_scope_ids: list[str] = []
    for path in sorted(added_paths):
        candidate_ids = {
            scope_id
            for edge in edges
            if edge.source_file == path
            and edge.edge_type is EdgeType.imports
            and edge.confidence == 1.0
            and edge.target_file is not None
            for scope_id, member_files in member_files_by_scope.items()
            if edge.target_file in member_files
        }
        if len(candidate_ids) != 1:
            continue
        scope_id = candidate_ids.pop()
        scope = get_scope(scopes_file, scope_id)
        if path in scope.members.files:
            continue
        updated = scope.model_copy(
            update={
                "members": scope.members.model_copy(
                    update={"files": sorted([*scope.members.files, path])}
                )
            }
        )
        scopes_file.scopes[scopes_file.scopes.index(scope)] = updated
        changed_scope_ids.append(scope_id)
    return changed_scope_ids
