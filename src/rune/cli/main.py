"""rune CLI (Typer app). Milestone 1 subset: init, status, rebuild-cache.

Every subcommand is a thin argument-parsing shim over `rune.core` — no
business logic lives here (ARCHITECTURE.md §5).
"""

from __future__ import annotations

import json as json_module
import sys
from pathlib import Path

import typer

from rune.core.project import (
    AlreadyInitializedError,
    NotAGitRepoError,
    RuneLayout,
    find_repo_root,
    init_project,
)
from rune.core.storage.canonical import read_json_model
from rune.core.storage.models import ProjectFile
from rune.core.storage.sqlite.materialize import CanonicalConflictError, rebuild_cache

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _err(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)


@app.command()
def init(
    force: bool = typer.Option(
        False, "--force", help="Repair missing structural files; never touches canonical memory."
    ),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Create (or, with --force, repair) .rune/ for the current repo."""
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
        stats = rebuild_cache(layout)
    except CanonicalConflictError as exc:
        _err(f"canonical conflict detected, cache not rebuilt: {exc}")
        raise typer.Exit(code=1) from exc

    typer.echo(
        "Rebuilt cache: "
        + ", ".join(f"{k}={v}" for k, v in stats.items())
    )


@app.command()
def status(
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show project identity and memory freshness."""
    path = path or Path.cwd()
    try:
        repo_root = find_repo_root(path)
    except NotAGitRepoError as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc

    layout = RuneLayout(repo_root=repo_root)
    if not layout.rune_dir.exists():
        _err(f"{layout.rune_dir} does not exist. Run `rune init` first.")
        raise typer.Exit(code=1)

    project = read_json_model(layout.project_json, ProjectFile)
    if project is None:
        _err(f"{layout.project_json} is missing or unreadable.")
        raise typer.Exit(code=1)

    payload = {
        "project_id": project.project_id,
        "name": project.name,
        "last_indexed_head": project.last_indexed_head,
        "last_indexed_tree_hash": project.last_indexed_tree_hash,
        "last_indexed_at": project.last_indexed_at,
        "cache_exists": layout.memory_db.exists(),
    }

    if json_output:
        typer.echo(json_module.dumps(payload, indent=2))
        return

    typer.echo(f"Project: {payload['name']} ({payload['project_id']})")
    typer.echo(f"Last indexed head: {payload['last_indexed_head'] or '(none)'}")
    typer.echo(f"Last indexed tree hash: {payload['last_indexed_tree_hash'] or '(none)'}")
    typer.echo(f"Cache: {'present' if payload['cache_exists'] else 'missing'}")


@app.command(name="rebuild-cache")
def rebuild_cache_cmd(
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Delete and fully rebuild memory.db from canonical files. Zero LLM calls."""
    path = path or Path.cwd()
    try:
        repo_root = find_repo_root(path)
    except NotAGitRepoError as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc

    layout = RuneLayout(repo_root=repo_root)
    if not layout.rune_dir.exists():
        _err(f"{layout.rune_dir} does not exist. Run `rune init` first.")
        raise typer.Exit(code=1)

    try:
        stats = rebuild_cache(layout)
    except CanonicalConflictError as exc:
        _err(f"canonical conflict detected, cache not rebuilt: {exc}")
        raise typer.Exit(code=1) from exc

    typer.echo("Rebuilt cache: " + ", ".join(f"{k}={v}" for k, v in stats.items()))


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
