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
from rune.core.storage.canonical import read_json_model
from rune.core.storage.models import ProjectFile
from rune.core.storage.sqlite.materialize import CanonicalConflictError
from rune.core.update import run_update

app = typer.Typer(add_completion=False, no_args_is_help=True)


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


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
