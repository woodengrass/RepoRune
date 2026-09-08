"""rune CLI (Typer app). Milestone 1-2 subset: init, status, update,
rebuild-cache.

Every subcommand is a thin argument-parsing shim over `rune.core` — no
business logic lives here (ARCHITECTURE.md §5).
"""

from __future__ import annotations

import json as json_module
import os
import sys
from pathlib import Path

import typer

from rune.core.config import ConfigError, load_config
from rune.core.doctor import run_doctor
from rune.core.memory.notes import NoteNotFoundError, NoteValidationError
from rune.core.memory.notes import note_add as core_note_add
from rune.core.memory.notes import note_update as core_note_update
from rune.core.memory.proposals import (
    ProposalAlreadyResolvedError,
    ProposalNotFoundError,
    RecordNotFoundError,
    get_current_proposal,
    list_pending_proposals,
)
from rune.core.memory.proposals import (
    ProposalValidationError as _ProposalValidationError,
)
from rune.core.memory.proposals import approve as core_approve
from rune.core.memory.proposals import deactivate as core_deactivate
from rune.core.memory.proposals import propose as core_propose
from rune.core.memory.proposals import reject as core_reject
from rune.core.memory.records import (
    load_current_constraints,
    load_current_decisions,
    load_current_notes,
    refresh_cache,
)
from rune.core.project import (
    AlreadyInitializedError,
    NotAGitRepoError,
    RuneLayout,
    find_repo_root,
    init_project,
)
from rune.core.protocol import PROTOCOL_VERSION
from rune.core.retrieval.check import check as core_check
from rune.core.retrieval.context import build_hard_bootstrap, build_soft_bootstrap
from rune.core.retrieval.related_context import RelatedContextValidationError
from rune.core.retrieval.related_context import related_context as core_related_context
from rune.core.retrieval.scope_for import scope_for as core_scope_for
from rune.core.retrieval.search import search as core_search
from rune.core.retrieval.symbol_search import symbol_search as core_symbol_search
from rune.core.scopes.clustering import suggest_from_graph
from rune.core.scopes.heuristics import ScopeCandidate, suggest_from_paths
from rune.core.scopes.model import (
    ScopeAlreadyExistsError,
    ScopeNotFoundError,
    ScopeValidationError,
    create_scope,
    delete_scope,
    load_scopes,
    save_scopes,
    set_scope_locked,
    update_scope,
)
from rune.core.scopes.reconcile import InvalidRefError
from rune.core.scopes.reconcile import reconcile as core_reconcile
from rune.core.status import compute_status
from rune.core.storage.models import (
    NoteCategory,
    NoteStatus,
    PersistenceMode,
    RecordType,
    Scope,
    ScopeSource,
    Severity,
)
from rune.core.storage.sqlite.materialize import (
    CacheUnusableError,
    CanonicalConflictError,
    connect_for_read,
)
from rune.core.update import run_update

app = typer.Typer(add_completion=False, no_args_is_help=True)
scope_app = typer.Typer(help="Create, maintain, and review scope suggestions.")
app.add_typer(scope_app, name="scope")
decision_app = typer.Typer(help="Propose, list, and deactivate Decisions.")
app.add_typer(decision_app, name="decision")
constraint_app = typer.Typer(help="Propose, list, and deactivate Constraints.")
app.add_typer(constraint_app, name="constraint")
note_app = typer.Typer(help="Add, update, and list Notes.")
app.add_typer(note_app, name="note")
proposal_app = typer.Typer(help="Review pending Decision/Constraint proposals.")
app.add_typer(proposal_app, name="proposal")


def _configure_windows_unicode_output() -> None:
    """Keep CLI text/JSON UTF-8 on legacy Windows console code pages."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


_configure_windows_unicode_output()


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
    except (CanonicalConflictError, ConfigError) as exc:
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

    try:
        project_status = compute_status(layout)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    if project_status is None:
        _err(f"{layout.project_json} is missing or unreadable.")
        raise typer.Exit(code=1)

    payload = {
        "project_id": project_status.project_id,
        "name": project_status.name,
        "last_indexed_head": project_status.last_indexed_head,
        "last_indexed_tree_hash": project_status.last_indexed_tree_hash,
        "last_indexed_at": project_status.last_indexed_at,
        "cache_exists": project_status.cache_exists,
        "cache_usable": project_status.cache_usable,
        "files_indexed": project_status.files_indexed,
        "symbols_indexed": project_status.symbols_indexed,
        "working_tree_fresh": project_status.working_tree_fresh,
        "files_modified": project_status.files_modified,
        "files_added": project_status.files_added,
        "files_deleted": project_status.files_deleted,
    }
    cache_exists = project_status.cache_exists
    cache_usable = project_status.cache_usable
    file_count = project_status.files_indexed
    symbol_count = project_status.symbols_indexed
    is_fresh = project_status.working_tree_fresh
    modified_count = project_status.files_modified
    added_count = project_status.files_added
    deleted_count = project_status.files_deleted

    if json_output:
        typer.echo(json_module.dumps({"protocol_version": PROTOCOL_VERSION, **payload}, indent=2))
        return

    typer.echo(f"Project: {payload['name']} ({payload['project_id']})")
    typer.echo(f"Last indexed head: {payload['last_indexed_head'] or '(none)'}")
    typer.echo(f"Last indexed tree hash: {payload['last_indexed_tree_hash'] or '(none)'}")
    cache_state = "usable" if cache_usable else ("unusable" if cache_exists else "missing")
    typer.echo(f"Cache: {cache_state}")
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
    """Incrementally rescan changed files and refresh the cache; only files
    whose content hash changed are re-parsed. If `semantic` is configured
    in config.toml, this is also the command that calls the LLM to refresh
    stale scope summaries -- unlike `rebuild-cache`, which never does."""
    path = path or Path.cwd()
    try:
        layout = _require_layout(path)
    except (NotAGitRepoError, _MissingLayoutError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc

    try:
        stats = run_update(layout, full=False)
    except (CanonicalConflictError, ConfigError) as exc:
        _err(f"canonical conflict detected, cache not rebuilt: {exc}")
        raise typer.Exit(code=1) from exc

    # Pulled out of `stats` before printing the generic k=v line so a
    # semantic-provider problem can't blend into it and get missed --
    # ARCHITECTURE.md §4.5's three-tier health check exists specifically
    # because that used to happen silently. `disabled`/`ok` never reach
    # here at all (run_update only sets these keys for the other three
    # statuses), so this block is a no-op on the common path.
    health_status = stats.pop("semantic_health_status", None)
    health_message = stats.pop("semantic_health_message", None)

    typer.echo("Updated: " + ", ".join(f"{k}={v}" for k, v in stats.items()))

    if health_status == "rate_limited":
        # Expected, not the user's fault -- a heads-up, not a failure.
        typer.echo(f"semantic: {health_message}")
    elif health_status in ("config_error", "probe_failed"):
        # Something needs the user's attention right now (a setup mistake
        # or a genuinely broken provider) -- the deterministic index above
        # already completed and was printed, so failing loudly here can't
        # lose or hide that work.
        _err(f"semantic: {health_message}")
        raise typer.Exit(code=1)


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
    except (CanonicalConflictError, ConfigError) as exc:
        _err(f"canonical conflict detected, cache not rebuilt: {exc}")
        raise typer.Exit(code=1) from exc

    typer.echo("Rebuilt cache: " + ", ".join(f"{k}={v}" for k, v in stats.items()))


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json"),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Read-only environment/health diagnostics: config validity, canonical
    schema versions, cache health, git/tree-sitter availability, semantic
    provider setup (existence-only, no network call), and Global MUST
    governance warnings (token budget / count threshold)."""
    try:
        layout = _require_layout(path or Path.cwd())
    except (NotAGitRepoError, _MissingLayoutError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc

    report = run_doctor(layout)
    if json_output:
        typer.echo(
            json_module.dumps(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "healthy": report.healthy,
                    "checks": [
                        {"name": c.name, "level": c.level.value, "message": c.message} for c in report.checks
                    ],
                },
                indent=2,
            )
        )
    else:
        for c in report.checks:
            marker = {"ok": "[ok]", "warn": "[warn]", "error": "[ERROR]"}[c.level.value]
            typer.echo(f"{marker} {c.name}: {c.message}")
    if not report.healthy:
        raise typer.Exit(code=1)


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
    except (
        NotAGitRepoError, _MissingLayoutError, ScopeAlreadyExistsError, ScopeValidationError
    ) as exc:
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
        conn = connect_for_read(layout)
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
    except (NotAGitRepoError, _MissingLayoutError, CacheUnusableError) as exc:
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


@scope_app.command("reconcile")
def scope_reconcile(
    full: bool = typer.Option(
        False, "--full",
        help="Audit every existing membership too (KEEP entries included). Output-only: "
        "never writes, even for high-confidence AUTO candidates -- see ARCHITECTURE.md §4.4.",
    ),
    since: str | None = typer.Option(
        None, "--since",
        help="Pre-merge git ref (e.g. the merge base) to revalidate merge-affected "
        "auto-inferred memberships against the merged import graph -- ARCHITECTURE.md "
        "§16.6. Adds REVIEW-only entries; never auto-reassigns or removes.",
    ),
    json_output: bool = typer.Option(False, "--json"),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Reconcile scope membership against the current materialized code
    index: high-confidence single-candidate imports for unassigned files
    auto-apply (unless the large-churn guardrail trips), everything else
    (ambiguous/no-evidence assignment, a removed membership target, a
    locked/human-authoritative target that no longer exists) is reported
    for human review, never silently written. Run `rune update` first so
    the index reflects the tree you want to reconcile against (the
    intended flow after merging worktrees, ARCHITECTURE.md §16.4). Pass
    `--since <ref>` after a merge to also revalidate memberships the merge
    itself introduced (§16.6)."""
    try:
        layout = _scope_layout(path)
        if not layout.memory_db.exists():
            _err(f"{layout.memory_db} does not exist yet. Run `rune update` first.")
            raise typer.Exit(code=1)
        # Refuse to write canonical membership against a cache that cannot
        # be read; reconcile must never treat an unusable index as empty.
        conn = connect_for_read(layout)
        conn.close()
        config = load_config(layout.config_path)
        result, updated_scopes_file = core_reconcile(
            layout, full=full, since=since,
            large_churn_threshold=config.scopes.reconcile_large_churn_threshold,
        )
        if updated_scopes_file is not None:
            save_scopes(layout, updated_scopes_file)
            refresh_cache(layout)
    except CanonicalConflictError as exc:
        _handle_cache_refresh_failure(exc)
    except (NotAGitRepoError, _MissingLayoutError, CacheUnusableError, ConfigError, InvalidRefError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(
            json_module.dumps(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "full": result.full,
                    "since": since,
                    "applied": result.applied,
                    "suspicious_churn": result.suspicious_churn,
                    "auto_count": result.auto_count,
                    "changed_count": sum(entry.applied for entry in result.entries),
                    "review_count": result.review_count,
                    "keep_count": result.keep_count,
                    "broken_count": result.broken_count,
                    "entries": [
                        {
                            "scope_id": e.scope_id, "target_type": e.target_type, "target": e.target,
                            "classification": e.classification.value, "reason": e.reason,
                            "applied": e.applied, "candidate_scope_ids": list(e.candidate_scope_ids),
                        }
                        for e in result.entries
                    ],
                },
                indent=2,
            )
        )
        return

    if full:
        # `--full` never writes regardless of churn (ARCHITECTURE.md §4.4
        # point 4) -- checked before `suspicious_churn` so a `--full` run
        # over a large-churn tree doesn't misleadingly attribute the
        # (already-guaranteed) no-write to the churn guardrail instead of
        # to `--full` itself. A review pass caught this ordering bug by
        # hand: with the old `if suspicious_churn: ... elif applied: ...
        # elif full: ...` order, `--full` on a tree past the churn
        # threshold printed "No changes were written -- review required"
        # (true, but for the wrong reason) instead of this message.
        typer.echo(f"{result.auto_count} AUTO candidate(s) found (--full, output-only).")
    elif result.suspicious_churn:
        typer.secho(
            f"Suspicious churn: {result.auto_count} AUTO-eligible change(s) exceed the "
            f"configured threshold ({config.scopes.reconcile_large_churn_threshold}). "
            "No changes were written -- review required.",
            fg=typer.colors.YELLOW,
        )
    elif result.applied:
        typer.echo(f"Applied {result.auto_count} AUTO membership change(s).")
    if not result.entries:
        typer.echo("Nothing to reconcile: membership matches the current code index.")
        return
    for e in result.entries:
        scope_part = f" scope={e.scope_id}" if e.scope_id else ""
        candidates_part = (
            f" candidates=[{', '.join(e.candidate_scope_ids)}]" if e.candidate_scope_ids else ""
        )
        applied_part = " (applied)" if e.applied else ""
        typer.echo(
            f"[{e.classification.value}]{scope_part} {e.target_type}:{e.target}"
            f"{candidates_part}{applied_part} -- {e.reason}"
        )
    typer.echo(
        f"auto={result.auto_count} review={result.review_count} "
        f"keep={result.keep_count} broken={result.broken_count}"
    )


# --------------------------------------------------------------------------
# Milestone 6: decision / constraint / note / proposal / search / check
# --------------------------------------------------------------------------

_DECISION_VISIBLE = {"active", "review_required"}
_CONSTRAINT_VISIBLE = {"active", "review_required", "stale"}
_NOTE_VISIBLE = {"active", "stale"}


def _validate_actor(value: str, option: str) -> None:
    if value not in ("agent", "human"):
        _err(f"{option} must be 'agent' or 'human', got {value!r}")
        raise typer.Exit(code=1)


def _handle_cache_refresh_failure(exc: CanonicalConflictError) -> None:
    """`core.memory.records.refresh_cache()` (called after every propose-
    approve/note write) can raise `CanonicalConflictError` if some
    *other* canonical file already has a conflict -- confirmed by hand:
    the write this command was actually asked to do (a Decision/
    Constraint/Note revision) had already succeeded by the time this
    fires, so a caller that only prints a raw traceback and exits leaves
    the user thinking the whole command failed, when what actually
    happened is "your data is safely written, but the search/check cache
    is now stale until you fix the *other* conflict and rebuild it".
    """
    _err(
        f"the write itself succeeded, but the search/check cache could not be refreshed: {exc}\n"
        "Fix the conflict and run `rune rebuild-cache` to bring the cache back in sync."
    )
    raise typer.Exit(code=1) from exc


@decision_app.command("propose")
def decision_propose(
    record_id: str = typer.Argument(..., help="Stable id, e.g. 'use-postgres-for-primary-store'."),
    content: str = typer.Option(..., "--content"),
    rationale: str = typer.Option("", "--rationale"),
    scopes: list[str] = typer.Option([], "--scope"),
    files: list[str] = typer.Option([], "--file"),
    symbols: list[str] = typer.Option([], "--symbol"),
    critical: bool = typer.Option(False, "--critical", help="Eligible for hard bootstrap (Milestone 7)."),
    source_document: str | None = typer.Option(None, "--source-document"),
    source_section: str | None = typer.Option(None, "--source-section"),
    created_by: str = typer.Option("agent", "--created-by", help="'agent' or 'human'."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output (e.g. for an adapter custom tool)."),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Propose a new Decision. Sits pending until `rune proposal approve`."""
    _validate_actor(created_by, "--created-by")
    try:
        layout = _scope_layout(path)
        config = load_config(layout.config_path)
        proposal = core_propose(
            layout, type=RecordType.decision, record_id=record_id, content=content,
            rationale=rationale, scopes=scopes, files=files, symbols=symbols,
            critical=critical, source_document=source_document, source_section=source_section,
            created_by=created_by,
            redact_secrets=config.security.redact_secrets,
        )
    except (NotAGitRepoError, _MissingLayoutError, ConfigError, _ProposalValidationError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(json_module.dumps(
            {"protocol_version": PROTOCOL_VERSION, "proposal_id": proposal.proposal_id,
             "record_id": record_id, "status": "pending"}
        ))
        return
    typer.echo(f"Proposed {proposal.proposal_id} (record_id={record_id}, pending approval)")


@decision_app.command("list")
def decision_list(
    show_all: bool = typer.Option(False, "--all", help="Include inactive/orphaned, not just current+visible."),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """List current Decisions."""
    try:
        current = load_current_decisions(_scope_layout(path))
    except (NotAGitRepoError, _MissingLayoutError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    rows = [r for r in current.values() if show_all or r.status.value in _DECISION_VISIBLE]
    if not rows:
        typer.echo("No decisions.")
        return
    for rev in sorted(rows, key=lambda r: r.record_id):
        typer.echo(f"{rev.record_id} [{rev.status.value}] rev{rev.revision}: {rev.content}")


@decision_app.command("deactivate")
def decision_deactivate(
    record_id: str = typer.Argument(...),
    by: str = typer.Option(..., "--by", help="Human identity/handle."),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    try:
        updated = core_deactivate(_scope_layout(path), RecordType.decision, record_id, by=by)
    except CanonicalConflictError as exc:
        _handle_cache_refresh_failure(exc)
    except (NotAGitRepoError, _MissingLayoutError, RecordNotFoundError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"{record_id} deactivated (rev{updated.revision})")


@constraint_app.command("propose")
def constraint_propose(
    record_id: str = typer.Argument(...),
    content: str = typer.Option(..., "--content"),
    severity: Severity = typer.Option(..., "--severity"),
    persistence_mode: PersistenceMode = typer.Option(..., "--persistence-mode"),
    rationale: str = typer.Option("", "--rationale"),
    scopes: list[str] = typer.Option([], "--scope"),
    files: list[str] = typer.Option([], "--file"),
    symbols: list[str] = typer.Option([], "--symbol"),
    expires_at: str | None = typer.Option(
        None, "--expires-at", help="ISO-8601 UTC; required for --persistence-mode=temporary."
    ),
    source_document: str | None = typer.Option(None, "--source-document"),
    source_section: str | None = typer.Option(None, "--source-section"),
    machine_check_hint: str | None = typer.Option(None, "--machine-check-hint"),
    created_by: str = typer.Option("agent", "--created-by", help="'agent' or 'human'."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output (e.g. for an adapter custom tool)."),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Propose a new Constraint. Sits pending until `rune proposal approve` --
    source_hashes/scope_hashes are computed automatically at approval time,
    never entered by hand."""
    _validate_actor(created_by, "--created-by")
    try:
        layout = _scope_layout(path)
        config = load_config(layout.config_path)
        proposal = core_propose(
            layout, type=RecordType.constraint, record_id=record_id, content=content,
            rationale=rationale, scopes=scopes, files=files, symbols=symbols,
            severity=severity, persistence_mode=persistence_mode, expires_at=expires_at,
            source_document=source_document, source_section=source_section,
            machine_check_hint=machine_check_hint, created_by=created_by,
            redact_secrets=config.security.redact_secrets,
        )
    except (NotAGitRepoError, _MissingLayoutError, ConfigError, _ProposalValidationError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(json_module.dumps(
            {"protocol_version": PROTOCOL_VERSION, "proposal_id": proposal.proposal_id,
             "record_id": record_id, "status": "pending"}
        ))
        return
    typer.echo(f"Proposed {proposal.proposal_id} (record_id={record_id}, pending approval)")


@constraint_app.command("list")
def constraint_list(
    show_all: bool = typer.Option(False, "--all", help="Include inactive/orphaned, not just current+visible."),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """List current Constraints."""
    try:
        current = load_current_constraints(_scope_layout(path))
    except (NotAGitRepoError, _MissingLayoutError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    rows = [r for r in current.values() if show_all or r.status.value in _CONSTRAINT_VISIBLE]
    if not rows:
        typer.echo("No constraints.")
        return
    for rev in sorted(rows, key=lambda r: (r.severity.value if r.severity else "", r.record_id)):
        severity = rev.severity.value if rev.severity else "?"
        typer.echo(f"{rev.record_id} [{severity}/{rev.status.value}] rev{rev.revision}: {rev.content}")


@constraint_app.command("deactivate")
def constraint_deactivate(
    record_id: str = typer.Argument(...),
    by: str = typer.Option(..., "--by", help="Human identity/handle."),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    try:
        updated = core_deactivate(_scope_layout(path), RecordType.constraint, record_id, by=by)
    except CanonicalConflictError as exc:
        _handle_cache_refresh_failure(exc)
    except (NotAGitRepoError, _MissingLayoutError, RecordNotFoundError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"{record_id} deactivated (rev{updated.revision})")


@note_app.command("add")
def note_add_cmd(
    category: NoteCategory = typer.Option(..., "--category"),
    content: str = typer.Option(..., "--content"),
    why_persist: str = typer.Option(..., "--why-persist"),
    scopes: list[str] = typer.Option([], "--scope"),
    files: list[str] = typer.Option([], "--file"),
    symbols: list[str] = typer.Option([], "--symbol"),
    importance: float = typer.Option(0.5, "--importance"),
    confidence: float = typer.Option(0.5, "--confidence"),
    evidence: list[str] = typer.Option([], "--evidence"),
    expires_at: str | None = typer.Option(None, "--expires-at"),
    source: str = typer.Option("agent", "--source", help="'agent' or 'human'."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output (e.g. for an adapter custom tool)."),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Add a new Note. No approval gate -- writes immediately."""
    _validate_actor(source, "--source")
    try:
        layout = _scope_layout(path)
        config = load_config(layout.config_path)
        note = core_note_add(
            layout, category=category, content=content, why_persist=why_persist,
            scopes=scopes, files=files, symbols=symbols, importance=importance,
            confidence=confidence, evidence=evidence, expires_at=expires_at, source=source,
            redact_secrets=config.security.redact_secrets,
        )
    except CanonicalConflictError as exc:
        _handle_cache_refresh_failure(exc)
    except (NotAGitRepoError, _MissingLayoutError, ConfigError, NoteValidationError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(json_module.dumps(
            {"protocol_version": PROTOCOL_VERSION, "id": note.id, "category": note.category.value}
        ))
        return
    typer.echo(f"Added note {note.id} ({note.category.value})")


@note_app.command("update")
def note_update_cmd(
    note_id: str = typer.Argument(...),
    content: str | None = typer.Option(None, "--content"),
    why_persist: str | None = typer.Option(None, "--why-persist"),
    status: NoteStatus | None = typer.Option(None, "--status"),
    evidence: list[str] = typer.Option([], "--evidence", help="Replaces the full evidence list if given."),
    clear_evidence: bool = typer.Option(
        False, "--clear-evidence", help="Replace the evidence list with an empty one."
    ),
    scopes: list[str] = typer.Option([], "--scope", help="Replaces the full scopes list if given."),
    clear_scopes: bool = typer.Option(
        False, "--clear-scopes", help="Replace the scopes list with an empty one."
    ),
    files: list[str] = typer.Option([], "--file", help="Replaces the full files list if given."),
    clear_files: bool = typer.Option(
        False, "--clear-files", help="Replace the files list with an empty one."
    ),
    symbols: list[str] = typer.Option([], "--symbol", help="Replaces the full symbols list if given."),
    clear_symbols: bool = typer.Option(
        False, "--clear-symbols", help="Replace the symbols list with an empty one."
    ),
    importance: float | None = typer.Option(None, "--importance"),
    confidence: float | None = typer.Option(None, "--confidence"),
    expires_at: str | None = typer.Option(None, "--expires-at"),
    clear_expires_at: bool = typer.Option(False, "--clear-expires-at"),
    source: str = typer.Option("agent", "--source", help="'agent' or 'human'."),
    recompute_source_hashes: bool = typer.Option(
        False, "--recompute-source-hashes",
        help="Re-snapshot files/symbols against the current index (e.g. after verifying a fix).",
    ),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Update a Note -- appends a new revision, carrying forward every
    field not explicitly overridden."""
    _validate_actor(source, "--source")
    try:
        layout = _scope_layout(path)
        config = load_config(layout.config_path)
        # `evidence or None` alone can't tell "no --evidence given, leave
        # untouched" apart from "user wants to clear it" -- both produce
        # the CLI's [] default. The --clear-* flags (mirroring the
        # existing --clear-expires-at) make that explicit; without one,
        # an empty list stays None (untouched), matching every existing
        # caller's behavior before these flags existed.
        updated = core_note_update(
            layout, note_id, content=content, why_persist=why_persist, status=status,
            evidence=(evidence or ([] if clear_evidence else None)),
            scopes=(scopes or ([] if clear_scopes else None)),
            files=(files or ([] if clear_files else None)),
            symbols=(symbols or ([] if clear_symbols else None)),
            importance=importance, confidence=confidence,
            expires_at=expires_at, clear_expires_at=clear_expires_at, source=source,
            redact_secrets=config.security.redact_secrets,
            recompute_source_hashes=recompute_source_hashes,
        )
    except CanonicalConflictError as exc:
        _handle_cache_refresh_failure(exc)
    except (NotAGitRepoError, _MissingLayoutError, ConfigError, NoteNotFoundError, NoteValidationError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"{note_id} updated to rev{updated.revision} [{updated.status.value}]")


@note_app.command("list")
def note_list(
    show_all: bool = typer.Option(False, "--all", help="Include expired/orphaned/archived, not just current+visible."),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    try:
        current = load_current_notes(_scope_layout(path))
    except (NotAGitRepoError, _MissingLayoutError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    rows = [n for n in current.values() if show_all or n.status.value in _NOTE_VISIBLE]
    if not rows:
        typer.echo("No notes.")
        return
    for note in sorted(rows, key=lambda n: n.id):
        warn = " [STALE]" if note.status is NoteStatus.stale else ""
        typer.echo(f"{note.id} [{note.category.value}/{note.status.value}] rev{note.revision}{warn}: {note.content}")


@proposal_app.command("list")
def proposal_list(
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """List pending proposals -- what's waiting for `[A]pprove`/`[R]eject`/`[E]dit`."""
    try:
        proposals = list_pending_proposals(_scope_layout(path))
    except (NotAGitRepoError, _MissingLayoutError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    if not proposals:
        typer.echo("No pending proposals.")
        return
    for p in proposals:
        typer.echo(f"{p.proposal_id} [{p.type.value}] record_id={p.record_id} created_by={p.created_by}")
        typer.echo(f"  {p.payload.content}")


@proposal_app.command("approve")
def proposal_approve(
    proposal_id: str = typer.Argument(...),
    by: str = typer.Option(..., "--by", help="Human identity/handle."),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    try:
        layout = _scope_layout(path)
        config = load_config(layout.config_path)
        _, memory_rev = core_approve(
            layout, proposal_id, resolved_by=by, redact_secrets=config.security.redact_secrets
        )
    except CanonicalConflictError as exc:
        _handle_cache_refresh_failure(exc)
    except (
        NotAGitRepoError, _MissingLayoutError, ProposalNotFoundError,
        ProposalAlreadyResolvedError, ConfigError, _ProposalValidationError,
    ) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"Approved {proposal_id}: {memory_rev.record_id} rev{memory_rev.revision} [{memory_rev.status.value}]"
    )


@proposal_app.command("reject")
def proposal_reject(
    proposal_id: str = typer.Argument(...),
    by: str = typer.Option(..., "--by", help="Human identity/handle."),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    try:
        core_reject(_scope_layout(path), proposal_id, resolved_by=by)
    except (NotAGitRepoError, _MissingLayoutError, ProposalNotFoundError, ProposalAlreadyResolvedError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"Rejected {proposal_id}")


@proposal_app.command("edit")
def proposal_edit(
    proposal_id: str = typer.Argument(...),
    by: str = typer.Option(..., "--by", help="Human identity/handle."),
    content: str | None = typer.Option(None, "--content"),
    rationale: str | None = typer.Option(None, "--rationale"),
    scopes: list[str] = typer.Option([], "--scope"),
    files: list[str] = typer.Option([], "--file"),
    symbols: list[str] = typer.Option([], "--symbol"),
    severity: Severity | None = typer.Option(None, "--severity"),
    persistence_mode: PersistenceMode | None = typer.Option(None, "--persistence-mode"),
    expires_at: str | None = typer.Option(None, "--expires-at"),
    critical: bool | None = typer.Option(
        None, "--critical/--not-critical", help="Decision-only: eligible for hard bootstrap."
    ),
    source_document: str | None = typer.Option(None, "--source-document"),
    source_section: str | None = typer.Option(None, "--source-section"),
    machine_check_hint: str | None = typer.Option(None, "--machine-check-hint"),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Edit a pending proposal's content, then approve the edited version
    in one step (DATA_MODEL.md §2.5a's `[E]dit` flow -- proposal status
    becomes `edited`, not `approved`)."""
    try:
        layout = _scope_layout(path)
        config = load_config(layout.config_path)
        proposal = get_current_proposal(layout, proposal_id)
        updates: dict = {}
        if content is not None:
            updates["content"] = content
        if rationale is not None:
            updates["rationale"] = rationale
        if scopes:
            updates["scopes"] = scopes
        if files:
            updates["files"] = files
        if symbols:
            updates["symbols"] = symbols
        if severity is not None:
            updates["severity"] = severity
        if persistence_mode is not None:
            updates["persistence_mode"] = persistence_mode
        if expires_at is not None:
            updates["expires_at"] = expires_at
        if critical is not None:
            updates["critical"] = critical
        if source_document is not None:
            updates["source_document"] = source_document
        if source_section is not None:
            updates["source_section"] = source_section
        if machine_check_hint is not None:
            updates["machine_check_hint"] = machine_check_hint
        edited_payload = proposal.payload.model_copy(update=updates)
        _, memory_rev = core_approve(
            layout, proposal_id, resolved_by=by, edited_payload=edited_payload,
            redact_secrets=config.security.redact_secrets,
        )
    except CanonicalConflictError as exc:
        _handle_cache_refresh_failure(exc)
    except (
        NotAGitRepoError, _MissingLayoutError, ProposalNotFoundError,
        ProposalAlreadyResolvedError, ConfigError, _ProposalValidationError,
    ) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"Edited+approved {proposal_id}: {memory_rev.record_id} rev{memory_rev.revision} [{memory_rev.status.value}]"
    )


@app.command()
def search(
    query: str = typer.Argument(...),
    history: bool = typer.Option(False, "--history", help="Also include non-visible current revisions."),
    limit: int = typer.Option(50, "--limit"),
    json_output: bool = typer.Option(False, "--json"),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Ranked search across scope summaries, Decisions, Constraints, and
    Notes (ARCHITECTURE.md §4.8's eight-layer priority order)."""
    try:
        layout = _require_layout(path or Path.cwd())
        results = core_search(layout, query, history=history, limit=limit)
    except (NotAGitRepoError, _MissingLayoutError, CacheUnusableError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(
            json_module.dumps(
                {"protocol_version": PROTOCOL_VERSION, "results": [
                    {"kind": r.kind, "rank": r.rank, "id": r.id, "text": r.text,
                      "status": r.status, "warning": r.warning, "revision": r.revision}
                    for r in results
                ]},
                indent=2,
            )
        )
        return
    if not results:
        typer.echo("No results.")
        return
    for r in results:
        warn = f" ({r.warning})" if r.warning else ""
        typer.echo(f"[{r.rank}] {r.kind} {r.id} [{r.status}]{warn}: {r.text}")


@app.command()
def check(
    json_output: bool = typer.Option(False, "--json"),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Working-tree changes -> affected scopes -> relevant Constraints. No model calls."""
    try:
        layout = _require_layout(path or Path.cwd())
        result = core_check(layout)
    except (NotAGitRepoError, _MissingLayoutError, CacheUnusableError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(
            json_module.dumps(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "changed_files": result.changed_files,
                    "affected_scope_ids": result.affected_scope_ids,
                    "constraints": [
                        {"record_id": c.record_id, "content": c.content, "severity": c.severity,
                         "status": c.status, "scope_ids": c.scope_ids}
                        for c in result.constraints
                    ],
                },
                indent=2,
            )
        )
        return
    if not result.changed_files:
        typer.echo("No changes detected.")
        return
    typer.echo(f"Changed files: {', '.join(result.changed_files)}")
    typer.echo(f"Affected scopes: {', '.join(result.affected_scope_ids) or '(none)'}")
    if not result.constraints:
        typer.echo("No relevant constraints.")
        return
    for c in result.constraints:
        typer.echo(f"[{c.severity}] {c.record_id} (scopes: {', '.join(c.scope_ids)}): {c.content}")


@app.command(name="scope-for")
def scope_for_cmd(
    file_path: str = typer.Argument(..., help="File path relative to the repo root."),
    json_output: bool = typer.Option(False, "--json"),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Which scope(s) FILE_PATH belongs to, plus each scope's summary,
    MUST/SHOULD constraints, and notes -- the context an adapter injects
    the first time an agent touches a scope in a session
    (ARCHITECTURE.md §6's `tool.execute.before` flow)."""
    try:
        layout = _require_layout(path or Path.cwd())
        results = core_scope_for(layout, file_path)
    except (NotAGitRepoError, _MissingLayoutError, CacheUnusableError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(
            json_module.dumps(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "path": file_path,
                    "scopes": [
                        {
                            "scope_id": s.scope_id, "name": s.name, "description": s.description,
                            "summary": s.summary, "summary_status": s.summary_status,
                            "constraints": [
                                {"record_id": c.record_id, "severity": c.severity, "content": c.content,
                                 "status": c.status, "warning": c.warning}
                                for c in s.constraints
                            ],
                            "notes": [
                                {"id": n.id, "category": n.category, "content": n.content,
                                 "status": n.status, "warning": n.warning}
                                for n in s.notes
                            ],
                        }
                        for s in results
                    ],
                },
                indent=2,
            )
        )
        return
    if not results:
        typer.echo(f"{file_path} is not a member of any scope.")
        return
    for s in results:
        typer.echo(f"{s.scope_id}: {s.name}")
        if s.summary:
            typer.echo(f"  summary [{s.summary_status}]: {s.summary}")
        for c in s.constraints:
            warn = f" ({c.warning})" if c.warning else ""
            typer.echo(f"  [{c.severity}] {c.record_id}{warn}: {c.content}")
        for n in s.notes:
            warn = f" ({n.warning})" if n.warning else ""
            typer.echo(f"  note [{n.category}]{warn}: {n.content}")


@app.command(name="symbol-search")
def symbol_search_cmd(
    query: str | None = typer.Option(None, "--query", help="Full-text match on qualified_name + signature."),
    name: str | None = typer.Option(None, "--name"),
    qualified_name: str | None = typer.Option(None, "--qualified-name"),
    kind: str | None = typer.Option(None, "--kind"),
    symbol_path: str | None = typer.Option(None, "--path-filter", help="Substring match on the owning file path."),
    limit: int = typer.Option(50, "--limit"),
    json_output: bool = typer.Option(False, "--json"),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Structured symbol lookup (name/qualified_name/kind/path filters,
    optionally combined with a full-text --query) -- the counterpart to
    `rune search`'s free-text search over Decision/Constraint/Note/
    semantic-summary prose."""
    try:
        layout = _require_layout(path or Path.cwd())
        results = core_symbol_search(
            layout, query=query, name=name, qualified_name=qualified_name,
            kind=kind, path=symbol_path, limit=limit,
        )
    except (NotAGitRepoError, _MissingLayoutError, CacheUnusableError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(
            json_module.dumps(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "results": [
                        {"symbol_id": s.symbol_id, "file": s.file, "name": s.name,
                         "qualified_name": s.qualified_name, "kind": s.kind, "signature": s.signature,
                         "start_line": s.start_line, "end_line": s.end_line}
                        for s in results
                    ],
                },
                indent=2,
            )
        )
        return
    if not results:
        typer.echo("No symbols found.")
        return
    for s in results:
        typer.echo(f"{s.qualified_name} [{s.kind}] {s.file}:{s.start_line}-{s.end_line}")


@app.command(name="related-context")
def related_context_cmd(
    context_path: str | None = typer.Option(None, "--path-filter", help="File path to gather context for."),
    symbol: str | None = typer.Option(None, "--symbol"),
    query: str | None = typer.Option(None, "--query"),
    include: list[str] = typer.Option(
        [], "--include",
        help="Restrict to these buckets (repeatable): scopes/constraints/decisions/notes/semantic/symbols.",
    ),
    max_items: int = typer.Option(20, "--max-items", help="Cap per bucket, not a combined total."),
    json_output: bool = typer.Option(False, "--json"),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """One call for the most relevant Rune context for a task: scopes,
    constraints, decisions, notes, semantic summaries, and symbols related
    to a path, a symbol, and/or a free-text query. Composes `scope_for`/
    `search`/`symbol_search` -- never generates new authoritative content."""
    try:
        layout = _require_layout(path or Path.cwd())
        result = core_related_context(
            layout, path=context_path, symbol=symbol, query=query,
            include=set(include) or None, max_items=max_items,
        )
    except (NotAGitRepoError, _MissingLayoutError, CacheUnusableError, RelatedContextValidationError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "scopes": [
            {"scope_id": s.scope_id, "name": s.name, "description": s.description,
             "summary": s.summary, "summary_status": s.summary_status}
            for s in result.scopes
        ],
        "constraints": [
            {"record_id": c.record_id, "severity": c.severity, "content": c.content,
             "status": c.status, "warning": c.warning}
            for c in result.constraints
        ],
        "decisions": [
            {"record_id": d.record_id, "content": d.content, "status": d.status, "warning": d.warning}
            for d in result.decisions
        ],
        "notes": [
            {"id": n.id, "category": n.category, "content": n.content, "status": n.status, "warning": n.warning}
            for n in result.notes
        ],
        "semantic": [
            {"scope_id": s.scope_id, "name": s.name, "summary": s.summary, "summary_status": s.summary_status}
            for s in result.semantic
        ],
        "symbols": [
            {"symbol_id": s.symbol_id, "file": s.file, "name": s.name, "qualified_name": s.qualified_name,
             "kind": s.kind, "signature": s.signature, "start_line": s.start_line, "end_line": s.end_line}
            for s in result.symbols
        ],
    }
    if json_output:
        typer.echo(json_module.dumps(payload, indent=2))
        return
    for key in ("scopes", "constraints", "decisions", "notes", "symbols"):
        typer.echo(f"{key}: {len(payload[key])}")
        for item in payload[key]:
            label = item.get("record_id") or item.get("scope_id") or item.get("id") or item.get("qualified_name")
            typer.echo(f"  {label}")


@app.command()
def bootstrap(
    mode: str = typer.Option(..., "--mode", help="'hard' or 'soft'."),
    json_output: bool = typer.Option(False, "--json"),
    path: Path = typer.Option(None, "--path", help="Directory inside the target repo (default: cwd)."),
) -> None:
    """Structured agent-injection payload (ARCHITECTURE.md §7.3): `--mode
    hard` returns current+visible global MUST Constraints plus
    critical=true global Decisions (re-injected on every session.created
    / session.compacted); `--mode soft` returns a project overview, scope
    summaries, memory freshness, and the remaining non-critical global
    Decisions (injected once per session.created). Core returns data
    only -- no OpenCode-specific prompt wording (that's the adapter's
    job)."""
    if mode not in ("hard", "soft"):
        _err("--mode must be 'hard' or 'soft'")
        raise typer.Exit(code=1)
    try:
        layout = _require_layout(path or Path.cwd())
        if mode == "hard":
            hard = build_hard_bootstrap(layout)
            payload = {
                "mode": "hard",
                "constraints": [
                    {"record_id": c.record_id, "severity": c.severity, "content": c.content,
                     "source_document": c.source_document, "source_section": c.source_section}
                    for c in hard.constraints
                ],
                "decisions": [
                    {"record_id": d.record_id, "content": d.content} for d in hard.decisions
                ],
                "estimated_tokens": hard.estimated_tokens,
                "budget_tokens": hard.budget_tokens,
                "overflow": hard.overflow,
            }
        else:
            soft = build_soft_bootstrap(layout)
            payload = {
                "mode": "soft",
                "project_name": soft.project_name,
                "working_tree_fresh": soft.working_tree_fresh,
                "files_indexed": soft.files_indexed,
                "symbols_indexed": soft.symbols_indexed,
                "scopes": [
                    {"scope_id": s.scope_id, "name": s.name, "description": s.description,
                     "summary": s.summary}
                    for s in soft.scopes
                ],
                "decisions": [
                    {"record_id": d.record_id, "content": d.content} for d in soft.decisions
                ],
                "estimated_tokens": soft.estimated_tokens,
                "budget_tokens": soft.budget_tokens,
                "overflow": soft.overflow,
            }
    except (NotAGitRepoError, _MissingLayoutError, CacheUnusableError) as exc:
        _err(str(exc))
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(json_module.dumps({"protocol_version": PROTOCOL_VERSION, **payload}, indent=2))
        return

    if mode == "hard":
        if payload["overflow"]:
            _err(
                f"hard bootstrap overflow: {payload['estimated_tokens']} tokens > "
                f"{payload['budget_tokens']} budget -- no rule was dropped, raise "
                f"bootstrap.hard_budget_tokens or trim the global MUST set"
            )
        for c in payload["constraints"]:
            typer.echo(f"[MUST] {c['record_id']}: {c['content']}")
        for d in payload["decisions"]:
            typer.echo(f"[critical decision] {d['record_id']}: {d['content']}")
    else:
        typer.echo(f"Project: {payload['project_name'] or '(unknown)'}")
        typer.echo(
            f"Working tree: {'fresh' if payload['working_tree_fresh'] else 'modified/unknown'} "
            f"({payload['files_indexed']} files, {payload['symbols_indexed']} symbols indexed)"
        )
        for s in payload["scopes"]:
            summary = f" -- {s['summary']}" if s["summary"] else ""
            typer.echo(f"  scope {s['scope_id']} ({s['name']}){summary}")
        for d in payload["decisions"]:
            typer.echo(f"  decision {d['record_id']}: {d['content']}")


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
