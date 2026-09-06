"""rune CLI (Typer app). Milestone 1-2 subset: init, status, update,
rebuild-cache.

Every subcommand is a thin argument-parsing shim over `rune.core` — no
business logic lives here (ARCHITECTURE.md §5).
"""

from __future__ import annotations

import json as json_module
import sqlite3
import sys
from pathlib import Path

import typer

from rune.core.config import load_config
from rune.core.hashing import working_tree_fingerprint
from rune.core.index.scanner import diff_against_previous, scan_files
from rune.core.project import (
    AlreadyInitializedError,
    NotAGitRepoError,
    RuneLayout,
    find_repo_root,
    init_project,
)
from rune.core.scopes.clustering import suggest_from_graph
from rune.core.scopes.heuristics import ScopeCandidate, suggest_from_paths
from rune.core.scopes.model import (
    ScopeAlreadyExistsError,
    ScopeNotFoundError,
    create_scope,
    delete_scope,
    load_scopes,
    save_scopes,
    set_scope_locked,
    update_scope,
)
from rune.core.storage.canonical import read_json_model
from rune.core.storage.models import ProjectFile, Scope, ScopeSource
from rune.core.storage.sqlite.materialize import CanonicalConflictError
from rune.core.update import run_update

app = typer.Typer(add_completion=False, no_args_is_help=True)
scope_app = typer.Typer(help="Create, maintain, and review scope suggestions.")
app.add_typer(scope_app, name="scope")


def _err(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)


def _require_layout(path: Path) -> RuneLayout:
    repo_root = find_repo_root(path)
    layout = RuneLayout(repo_root=repo_root)
    if not layout.rune_dir.exists():
        raise _MissingLayoutError(f"{layout.rune_dir} does not exist. Run `rune init` first.")
    return layout


class _MissingLayoutError(Exception):
    pass


@app.command()
def init(
    force: bool = typer.Option(
        False, "--force", help="Repair missing structural files; never touches canonical memory."
    ),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Create (or, with --force, repair) .rune/ for the current repo, then
    run a full initial code index scan."""
    path = path or Path.cwd()
    try:
        repo_root = find_repo_root(path)
    except NotAGitRepoError as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc

    try:
        layout = init_project(repo_root, force=force)
    except AlreadyInitializedError as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(f"rune initialized at {layout.rune_dir}")

    try:
        stats = run_update(layout, full=True)
    except CanonicalConflictError as exc:
        _err(f"canonical conflict detected, cache not rebuilt: {exc}")
        raise typer.Exit(code=1) from exc

    typer.echo("Indexed: " + ", ".join(f"{k}={v}" for k, v in stats.items()))


@app.command()
def status(
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show project identity, code index size, and memory freshness."""
    path = path or Path.cwd()
    try:
        layout = _require_layout(path)
    except (NotAGitRepoError, _MissingLayoutError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc

    project = read_json_model(layout.project_json, ProjectFile)
    if project is None:
        _err(f"{layout.project_json} is missing or unreadable.")
        raise typer.Exit(code=1)

    file_count = symbol_count = 0
    previous_hashes: dict[str, str] = {}
    cache_exists = layout.memory_db.exists()
    if cache_exists:
        conn = sqlite3.connect(str(layout.memory_db))
        try:
            file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            symbol_count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            previous_hashes = dict(conn.execute("SELECT path, content_hash FROM files"))
        finally:
            conn.close()

    current_tree_hash: str | None = None
    modified_count = added_count = deleted_count = 0
    try:
        config = load_config(layout.config_path)
        scanned = scan_files(layout.repo_root, config.index)
        current_tree_hash = working_tree_fingerprint({f.path: f.content_hash for f in scanned})
        changeset = diff_against_previous(scanned, previous_hashes)
        modified_count = len(changeset.modified)
        added_count = len(changeset.added)
        deleted_count = len(changeset.deleted_paths)
    except Exception:  # noqa: BLE001 - status must never crash on a scan hiccup
        current_tree_hash = None

    is_fresh = (
        current_tree_hash is not None and current_tree_hash == project.last_indexed_tree_hash
    )

    payload = {
        "project_id": project.project_id,
        "name": project.name,
        "last_indexed_head": project.last_indexed_head,
        "last_indexed_tree_hash": project.last_indexed_tree_hash,
        "last_indexed_at": project.last_indexed_at,
        "cache_exists": cache_exists,
        "files_indexed": file_count,
        "symbols_indexed": symbol_count,
        "working_tree_fresh": is_fresh,
        "files_modified": modified_count,
        "files_added": added_count,
        "files_deleted": deleted_count,
    }

    if json_output:
        typer.echo(json_module.dumps(payload, indent=2))
        return

    typer.echo(f"Project: {payload['name']} ({payload['project_id']})")
    typer.echo(f"Last indexed head: {payload['last_indexed_head'] or '(none)'}")
    typer.echo(f"Last indexed tree hash: {payload['last_indexed_tree_hash'] or '(none)'}")
    typer.echo(f"Cache: {'present' if cache_exists else 'missing'}")
    typer.echo(f"Files indexed: {file_count}")
    typer.echo(f"Symbols indexed: {symbol_count}")
    typer.echo(f"Working tree: {'fresh' if is_fresh else 'modified since last index'}")
    if not is_fresh:
        typer.echo(
            f"  {modified_count} modified, {added_count} added, {deleted_count} deleted "
            f"(relative to the last index)"
        )


@app.command()
def update(
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Incrementally rescan changed files and refresh the cache. Zero LLM
    calls; only files whose content hash changed are re-parsed."""
    path = path or Path.cwd()
    try:
        layout = _require_layout(path)
    except (NotAGitRepoError, _MissingLayoutError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc

    try:
        stats = run_update(layout, full=False)
    except CanonicalConflictError as exc:
        _err(f"canonical conflict detected, cache not rebuilt: {exc}")
        raise typer.Exit(code=1) from exc

    typer.echo("Updated: " + ", ".join(f"{k}={v}" for k, v in stats.items()))


@app.command(name="rebuild-cache")
def rebuild_cache_cmd(
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Fully rebuild memory.db from canonical files and a fresh full scan
    of the source tree. Zero LLM calls."""
    path = path or Path.cwd()
    try:
        layout = _require_layout(path)
    except (NotAGitRepoError, _MissingLayoutError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc

    try:
        stats = run_update(layout, full=True)
    except CanonicalConflictError as exc:
        _err(f"canonical conflict detected, cache not rebuilt: {exc}")
        raise typer.Exit(code=1) from exc

    typer.echo("Rebuilt cache: " + ", ".join(f"{k}={v}" for k, v in stats.items()))


def _scope_layout(path: Path | None) -> RuneLayout:
    return _require_layout(path or Path.cwd())


def _print_scope(scope: Scope) -> None:
    typer.echo(f"{scope.id}: {scope.name} ({'locked' if scope.locked else 'unlocked'}, {scope.source.value})")
    if scope.description:
        typer.echo(f"  {scope.description}")
    typer.echo(f"  files: {', '.join(scope.members.files) or '(none)'}")
    typer.echo(f"  symbols: {', '.join(scope.members.symbols) or '(none)'}")


@scope_app.command("list")
def scope_list(
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """List canonical scopes and their membership."""
    try:
        scopes = load_scopes(_scope_layout(path)).scopes
    except (NotAGitRepoError, _MissingLayoutError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    if not scopes:
        typer.echo("No scopes defined.")
        return
    for scope in sorted(scopes, key=lambda item: item.id):
        _print_scope(scope)


@scope_app.command("create")
def scope_create(
    scope_id: str = typer.Argument(..., help="Stable kebab-case scope id."),
    name: str = typer.Option(..., "--name"),
    description: str = typer.Option("", "--description"),
    files: list[str] = typer.Option([], "--file"),
    symbols: list[str] = typer.Option([], "--symbol"),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Create a human scope. Manual scopes start locked."""
    try:
        scope = create_scope(_scope_layout(path), scope_id, name, description, files, symbols)
    except (NotAGitRepoError, _MissingLayoutError, ScopeAlreadyExistsError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    _print_scope(scope)


@scope_app.command("edit")
def scope_edit(
    scope_id: str = typer.Argument(...),
    name: str | None = typer.Option(None, "--name"),
    description: str | None = typer.Option(None, "--description"),
    add_files: list[str] = typer.Option([], "--add-file"),
    remove_files: list[str] = typer.Option([], "--remove-file"),
    add_symbols: list[str] = typer.Option([], "--add-symbol"),
    remove_symbols: list[str] = typer.Option([], "--remove-symbol"),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Edit a scope's human-controlled metadata or membership."""
    try:
        scope = update_scope(
            _scope_layout(path), scope_id, name=name, description=description,
            add_files=add_files, remove_files=remove_files,
            add_symbols=add_symbols, remove_symbols=remove_symbols,
        )
    except (NotAGitRepoError, _MissingLayoutError, ScopeNotFoundError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    _print_scope(scope)


def _set_scope_lock(path: Path | None, scope_id: str, locked: bool) -> None:
    try:
        scope = set_scope_locked(_scope_layout(path), scope_id, locked)
    except (NotAGitRepoError, _MissingLayoutError, ScopeNotFoundError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    _print_scope(scope)


@scope_app.command("lock")
def scope_lock(
    scope_id: str = typer.Argument(...),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Prevent all automatic scope membership changes."""
    _set_scope_lock(path, scope_id, True)


@scope_app.command("unlock")
def scope_unlock(
    scope_id: str = typer.Argument(...),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Allow only high-confidence incremental import assignment."""
    _set_scope_lock(path, scope_id, False)


@scope_app.command("delete")
def scope_delete(
    scope_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="Delete without confirmation."),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Delete a scope and its canonical membership."""
    try:
        layout = _scope_layout(path)
        if not yes and not typer.confirm(f"Delete scope {scope_id!r}?"):
            raise typer.Abort()
        delete_scope(layout, scope_id)
    except (NotAGitRepoError, _MissingLayoutError, ScopeNotFoundError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"Deleted scope {scope_id!r}.")


def _unique_candidate_id(candidate: ScopeCandidate, used_ids: set[str]) -> str:
    base = candidate.id or "suggested-scope"
    candidate_id = base
    suffix = 2
    while candidate_id in used_ids:
        candidate_id = f"{base}-{suffix}"
        suffix += 1
    return candidate_id


@scope_app.command("suggest")
def scope_suggest(
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Interactively review ephemeral path and graph scope suggestions."""
    try:
        layout = _scope_layout(path)
        if not layout.memory_db.exists():
            _err(f"{layout.memory_db} does not exist yet. Run `rune update` first.")
            raise typer.Exit(code=1)
        scopes_file = load_scopes(layout)
        locked_files = {
            file_path for scope in scopes_file.scopes if scope.locked for file_path in scope.members.files
        }
        conn = sqlite3.connect(str(layout.memory_db))
        try:
            symbol_files = dict(conn.execute("SELECT symbol_id, file FROM symbols"))
            locked_files.update(
                symbol_files[symbol_id]
                for scope in scopes_file.scopes
                if scope.locked
                for symbol_id in scope.members.symbols
                if symbol_id in symbol_files
            )
            files = [row[0] for row in conn.execute("SELECT path FROM files ORDER BY path")]
            candidates = suggest_from_paths(files, locked_files) + suggest_from_graph(conn, locked_files)
        finally:
            conn.close()
    except (NotAGitRepoError, _MissingLayoutError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc

    used_ids = {scope.id for scope in scopes_file.scopes}
    accepted = 0
    for candidate in candidates:
        scope_id = _unique_candidate_id(candidate, used_ids)
        typer.echo(f"\n{scope_id}: {candidate.reason}")
        typer.echo("  " + ", ".join(candidate.files))
        if typer.confirm("Create this model-suggested scope?", default=False):
            scopes_file.scopes.append(
                Scope(
                    id=scope_id,
                    name=candidate.name,
                    source=ScopeSource.model,
                    members={"files": list(candidate.files)},
                )
            )
            used_ids.add(scope_id)
            accepted += 1
    if accepted:
        save_scopes(layout, scopes_file)
    typer.echo(f"Accepted {accepted} scope suggestion(s).")


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
