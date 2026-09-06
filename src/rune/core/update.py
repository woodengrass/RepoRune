"""`rune update` / `rune rebuild-cache` orchestrator: scanner -> indexer ->
materialize (ARCHITECTURE.md §4.9, §4.2).

Two entry points share one code path:
- `run_update(layout, full=False)` — the normal incremental path. Only
  files whose content_hash changed since the last run are re-parsed;
  everything else reuses the symbols/edges already sitting in memory.db.
- `run_update(layout, full=True)` — used by `rune rebuild-cache`. Treats
  every matching file as new, so the whole code index is freshly derived
  from source. Still zero LLM calls, zero network — this is local parsing
  only, so "full" is not expensive the way a semantic refresh would be.

Either way, the actual SQLite write goes through
`materialize.rebuild_cache`, which clears and repopulates every table
(including the canonical-memory ones) in one transaction — `core.update`
only decides *what to put in* the code-index portion of that call.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path

from rune.core.config import load_config
from rune.core.hashing import working_tree_fingerprint
from rune.core.index.imports import build_import_edges
from rune.core.index.references import group_symbols_by_path, resolve_references
from rune.core.index.scanner import ScannedFile, diff_against_previous, scan_files
from rune.core.index.treesitter import RawReference, get_parser_adapter
from rune.core.project import RuneLayout, utc_now_iso
from rune.core.scopes.model import (
    assign_new_files_from_imports,
    load_scopes,
    save_scopes,
)
from rune.core.semantic.provider import ModelProvider, ProviderError, build_provider
from rune.core.semantic.worker import (
    aggregate_metrics,
    detect_orphaned_scopes,
    run_semantic_refresh,
)
from rune.core.storage.canonical import (
    append_jsonl_many,
    read_json_model,
    read_jsonl,
    write_json_model,
)
from rune.core.storage.models import (
    Edge,
    EdgeType,
    IndexedFile,
    IndexedFileStatus,
    ProjectFile,
    RuneConfig,
    ScopeSummary,
    Symbol,
)
from rune.core.storage.sqlite.materialize import (
    CodeIndexData,
    SemanticRunMetricsRecord,
    current_scope_summaries,
    read_current_code_index,
    rebuild_cache,
)


def _git_head(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None  # e.g. a repo with no commits yet
    return result.stdout.strip()


def _to_indexed_file(scanned: ScannedFile, indexed_at: str, status: IndexedFileStatus) -> IndexedFile:
    return IndexedFile(
        path=scanned.path,
        language=scanned.language,
        content_hash=scanned.content_hash,
        size=scanned.size,
        mtime=scanned.mtime,
        git_blob_hash=scanned.git_blob_hash,
        indexed_at=indexed_at,
        status=status,
    )


def _parse_file(
    repo_root: Path, scanned: ScannedFile
) -> tuple[list[Symbol], list[Edge], list[RawReference], IndexedFileStatus]:
    """Parses one file. A parse failure marks that file `parse_error` and
    yields no symbols/edges/references for it — it must never abort the
    whole update (spec §62's failure-isolation principle, applied here to
    indexing).

    Two distinct ways a file ends up `parse_error`, both handled here:

    1. An actual exception (unreadable file, or an AST shape our walkers
       don't defensively guard against — e.g. a `MISSING` node tree-sitter
       synthesizes during error recovery, which can leave an expected
       field `None`). Caught broadly and deliberately: no symbols/edges
       are kept for this file.
    2. `adapter.has_syntax_error(source)` is True. tree-sitter's grammars
       are error-tolerant for malformed source — they produce a tree with
       ERROR/partial nodes rather than raising, so extraction can succeed
       and return symbols that are individually well-formed but drawn
       from a file that didn't fully parse. Those symbols are still kept
       (best effort, same principle as unresolved imports), but the file
       is flagged `parse_error` so this is visible rather than silently
       reported as `ok`.

    Reference resolution (calls/extends/implements) is deliberately NOT
    done here: it needs the *complete*, cross-file symbol table (including
    files this one wasn't re-parsed against), which only exists once every
    file in this run has been through this function. `run_update` collects
    the raw (unresolved) references from every file and resolves them in
    one pass afterward — see `resolve_references`.
    """
    try:
        source = scanned.absolute_path.read_bytes()
        adapter = get_parser_adapter(scanned.language, scanned.path)
        symbols = adapter.extract_symbols(scanned.path, source)
        raw_imports = adapter.extract_imports(scanned.path, source)
        import_edges = build_import_edges(repo_root, scanned.path, scanned.language, raw_imports)
        raw_references = adapter.extract_references(scanned.path, source)
        status = (
            IndexedFileStatus.parse_error
            if adapter.has_syntax_error(source)
            else IndexedFileStatus.ok
        )
    except Exception:  # noqa: BLE001 - intentional: isolate one file's parse failure
        return [], [], [], IndexedFileStatus.parse_error
    return symbols, import_edges, raw_references, status


def _extract_references_only(scanned: ScannedFile) -> list[RawReference]:
    """Re-extracts just the raw (unresolved) references for a file whose
    content_hash hasn't changed this run, so its calls/extends/implements
    edges get re-resolved against the *current* run's symbol table rather
    than reused verbatim from last run.

    This file's own symbols/imports are still safely reused as-is from the
    cache: both depend only on this file's own (unchanged) content. But a
    resolved reference edge's correctness also depends on the *target's*
    symbol table, which can change even when this file doesn't — e.g. a
    function this file already calls gets added to an already-imported
    module. `edges_by_path` reuse alone would carry the old, now-stale
    resolution forward and never revisit it, since this file would never
    be re-parsed again unless *its own* content changes. Reusing only the
    `imports` edges for unchanged files and always recomputing references
    from a fresh extraction closes that gap.

    Deliberately not folded into `_parse_file`: this only re-runs
    `extract_references`, not the (already-trusted, unnecessary-to-redo)
    `extract_symbols`/`extract_imports`, and it must never change the
    file's `status` — a reference-extraction hiccup here isn't a full
    parse failure, so it's isolated the same way `_parse_file` isolates
    its own failures (best effort, no symbols/edges rather than aborting
    the whole update), just without touching `IndexedFileStatus`.
    """
    try:
        source = scanned.absolute_path.read_bytes()
        adapter = get_parser_adapter(scanned.language, scanned.path)
        return adapter.extract_references(scanned.path, source)
    except Exception:  # noqa: BLE001 - same isolation principle as _parse_file
        return []


def _build_semantic_providers(config: RuneConfig) -> tuple[ModelProvider | None, ModelProvider | None]:
    """Builds (primary, fallback) providers for the semantic worker, or
    (None, None) if semantic refresh can't run this update — disabled in
    config, no model configured, or the required API key env var is
    missing. Never raises: a semantic-provider problem must never abort
    the deterministic index update (ARCHITECTURE.md §4.9's failure-
    isolation principle applies here too), it just means no scopes get
    refreshed this run.
    """
    if not config.semantic.enabled or not config.semantic.model:
        return None, None
    try:
        primary = build_provider(
            provider_name=config.semantic.provider,
            model=config.semantic.model,
            reasoning=config.semantic.reasoning,
        )
    except ProviderError:
        return None, None
    fallback = None
    if config.semantic.fallback_model:
        try:
            fallback = build_provider(
                provider_name=config.semantic.provider,
                model=config.semantic.fallback_model,
                reasoning=config.semantic.reasoning,
            )
        except ProviderError:
            fallback = None
    return primary, fallback


def _append_semantic_log(layout: RuneLayout, lines: list[str]) -> None:
    if not lines:
        return
    layout.logs_dir.mkdir(parents=True, exist_ok=True)
    with layout.semantic_log.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(f"{utc_now_iso()} {line}\n")


def run_update(layout: RuneLayout, full: bool = False) -> dict[str, int | float]:
    config = load_config(layout.config_path)
    repo_root = layout.repo_root
    now = utc_now_iso()

    previous = CodeIndexData() if full else read_current_code_index(layout)
    previous_hashes = {f.path: f.content_hash for f in previous.files}
    previous_status_by_path = {f.path: f.status for f in previous.files}
    symbols_by_path: dict[str, list[Symbol]] = defaultdict(list)
    for s in previous.symbols:
        symbols_by_path[s.file].append(s)
    edges_by_path: dict[str, list[Edge]] = defaultdict(list)
    for e in previous.edges:
        edges_by_path[e.source_file].append(e)

    scanned = scan_files(repo_root, config.index)
    changeset = diff_against_previous(scanned, previous_hashes)

    new_files: list[IndexedFile] = []
    new_symbols: list[Symbol] = []
    new_edges: list[Edge] = []
    files_parsed = 0
    # path -> not-yet-resolved references, only for files parsed *this*
    # run. Reused (unchanged) files' reference edges are carried over as-
    # is via edges_by_path above, same as import edges — see the
    # module docstring on why re-resolving them isn't done here.
    pending_references: dict[str, list[RawReference]] = {}

    for scanned_file in changeset.unchanged:
        # A file's content_hash is unchanged, but that says nothing about
        # whether it previously parsed cleanly — carry its actual last
        # status forward rather than assuming `ok`. Content-hash-equal
        # means "not re-parsed", not "known good": a file that was
        # `parse_error` last time is *still* `parse_error` until it's
        # actually re-parsed (i.e. until its content changes), otherwise
        # a no-op `rune update` would silently launder a known-bad file
        # back to `ok` without re-running the parser at all.
        previous_status = previous_status_by_path.get(scanned_file.path, IndexedFileStatus.ok)
        new_files.append(_to_indexed_file(scanned_file, now, previous_status))
        new_symbols.extend(symbols_by_path.get(scanned_file.path, []))
        # Only `imports` edges are safe to reuse verbatim here: import
        # resolution depends solely on this file's own (unchanged) import
        # statements. calls/extends/implements depend on the *target's*
        # symbol table too, which can change on a run where this file
        # itself doesn't — so those must be recomputed from a fresh
        # reference extraction below, not carried over from last run.
        new_edges.extend(
            edge
            for edge in edges_by_path.get(scanned_file.path, [])
            if edge.edge_type == EdgeType.imports
        )
        pending_references[scanned_file.path] = _extract_references_only(scanned_file)

    for scanned_file in (*changeset.added, *changeset.modified):
        symbols, import_edges, raw_references, status = _parse_file(repo_root, scanned_file)
        files_parsed += 1
        new_files.append(_to_indexed_file(scanned_file, now, status))
        new_symbols.extend(symbols)
        new_edges.extend(import_edges)
        pending_references[scanned_file.path] = raw_references

    if pending_references:
        # Resolution needs the complete cross-file symbol table, including
        # unchanged files that weren't re-parsed this run, which is only
        # fully assembled once every added/modified file above has
        # contributed its symbols to new_symbols.
        current_symbols_by_path = group_symbols_by_path(new_symbols)
        for file_path, raw_references in pending_references.items():
            imported_files = {
                e.target_file
                for e in new_edges
                if e.source_file == file_path
                and e.edge_type == EdgeType.imports
                and e.target_file is not None
            }
            new_edges.extend(
                resolve_references(file_path, raw_references, current_symbols_by_path, imported_files)
            )

    # Everything that can be computed without touching disk is done before
    # the SQLite commit, so the only work left afterward is the one atomic
    # write to project.json (minimizing what could go wrong in the window
    # between "cache committed" and "freshness metadata recorded").
    tree_hash = working_tree_fingerprint({f.path: f.content_hash for f in new_files})
    project = read_json_model(layout.project_json, ProjectFile)
    updated_project = (
        project.model_copy(
            update={
                "last_indexed_head": _git_head(repo_root),
                "last_indexed_tree_hash": tree_hash,
                "last_indexed_at": now,
            }
        )
        if project is not None
        else None
    )

    # Computed here (in memory only) so it can be materialized into this
    # same rebuild_cache transaction via `scopes_override` — see that
    # parameter's docstring. The canonical `scopes.json` write is
    # deliberately deferred until *after* rebuild_cache succeeds (below):
    # scopes.json is authoritative canonical content, not derived cache, so
    # writing it before a rebuild_cache that then fails (a canonical
    # conflict, a disk error) would leave canonical membership ahead of
    # what the cache — and every other reader relying on `rune update`
    # being all-or-nothing (ARCHITECTURE.md §4.9) — actually reflects.
    auto_assigned_scope_ids: list[str] = []
    updated_scopes_file = None
    if not full and changeset.added:
        updated_scopes_file = load_scopes(layout)
        auto_assigned_scope_ids = assign_new_files_from_imports(
            updated_scopes_file,
            {scanned_file.path for scanned_file in changeset.added},
            new_edges,
            new_symbols,
        )
        if not auto_assigned_scope_ids:
            updated_scopes_file = None

    # Semantic refresh (Milestone 5, ARCHITECTURE.md §4.5): computed here,
    # in memory, for the same reason as the scopes auto-assignment above —
    # the new/updated ScopeSummary revisions must land in this run's
    # rebuild_cache transaction via `semantic_override`, but the canonical
    # `semantic.jsonl` append is deferred until after rebuild_cache
    # actually succeeds, so a failed update never leaves semantic.jsonl
    # ahead of what the cache reflects (same all-or-nothing rationale as
    # scopes.json in Milestone 4).
    new_semantic_revisions: list[ScopeSummary] = []
    semantic_local_log: list[str] = []
    semantic_metrics_summary: dict[str, float] = {}
    semantic_override: list[ScopeSummary] | None = None
    semantic_run_metrics_record: SemanticRunMetricsRecord | None = None

    scopes_for_semantic = (updated_scopes_file or load_scopes(layout)).scopes
    existing_semantic = read_jsonl(layout.semantic_jsonl, ScopeSummary)
    current_summaries = current_scope_summaries(existing_semantic)
    # Orphan detection is a pure structural check (no LLM call), so unlike
    # the refresh below it always runs — including on `rune rebuild-cache`
    # (full=True) and when no provider is configured at all.
    new_semantic_revisions.extend(
        detect_orphaned_scopes(
            current_summaries, {scope.id for scope in scopes_for_semantic}, now
        )
    )

    # The refresh itself is the one part of `rune update` that calls an
    # LLM, so it must never run for `rune rebuild-cache` (full=True) —
    # that command's whole contract, in its own --help text and this
    # module's docstring, is "zero LLM calls, zero network, local parsing
    # only". Confirmed by hand this was being violated: rebuild-cache with
    # a configured provider was silently making real API calls.
    if not full:
        primary_provider, fallback_provider = _build_semantic_providers(config)
        if primary_provider is not None:
            refresh_result = run_semantic_refresh(
                repo_root=repo_root,
                scopes=scopes_for_semantic,
                current_summaries=current_summaries,
                file_hashes={f.path: f.content_hash for f in new_files},
                symbols=new_symbols,
                primary_provider=primary_provider,
                fallback_provider=fallback_provider,
                max_input_tokens_per_run=config.semantic.budget.max_input_tokens_per_run,
                max_tokens_per_call=config.semantic.max_tokens,
                pricing=config.pricing,
                redact_secrets=config.security.redact_secrets,
            )
            new_semantic_revisions.extend(refresh_result.new_revisions)
            semantic_local_log = refresh_result.local_log_lines
            semantic_metrics_summary = aggregate_metrics(refresh_result.metrics)
            if refresh_result.attempted_scope_ids:
                # Only recorded when at least one scope was actually
                # attempted this run — an all-fresh run with nothing to
                # refresh has no meaningful "success rate" to report and
                # would otherwise flood this history table with empty rows
                # on every single `rune update`.
                semantic_run_metrics_record = SemanticRunMetricsRecord(
                    run_at=now,
                    provider=config.semantic.provider,
                    model=config.semantic.model,
                    scopes_attempted=len(refresh_result.attempted_scope_ids),
                    schema_success_rate=semantic_metrics_summary["schema_success_rate"],
                    reference_strip_rate=semantic_metrics_summary["reference_strip_rate"],
                    fallback_rate=semantic_metrics_summary["fallback_rate"],
                    provider_error_rate=semantic_metrics_summary["provider_error_rate"],
                    total_cost=semantic_metrics_summary["cost"],
                    total_latency_seconds=semantic_metrics_summary["latency_seconds"],
                )

    if new_semantic_revisions:
        semantic_override = [*existing_semantic, *new_semantic_revisions]

    stats = rebuild_cache(
        layout,
        code_index=CodeIndexData(files=new_files, symbols=new_symbols, edges=new_edges),
        scopes_override=updated_scopes_file,
        semantic_override=semantic_override,
        semantic_run_metrics=semantic_run_metrics_record,
    )
    if updated_scopes_file is not None:
        save_scopes(layout, updated_scopes_file)
    # A single atomic write for every new semantic revision this run
    # (orphan markers plus any real refreshes) instead of N separate
    # append_jsonl calls — see append_jsonl_many's docstring for why N
    # separate atomic writes isn't good enough here: a failure partway
    # through would leave canonical behind what the already-committed
    # SQLite transaction above reflects for some scopes but not others.
    append_jsonl_many(layout.semantic_jsonl, new_semantic_revisions)
    _append_semantic_log(layout, semantic_local_log)

    # KNOWN LIMITATION (see IMPLEMENTATION_PLAN.md): this write is not
    # atomic with the SQLite commit above — memory.db and project.json are
    # two separate files, and true two-phase-commit across them is not
    # worth the complexity for what it buys. If this write fails (e.g.
    # disk full) after rebuild_cache already succeeded, memory.db is
    # correctly up to date but project.json's last_indexed_* fields go
    # stale, which only affects `rune status`'s freshness display — it
    # does NOT corrupt future updates, because `read_current_code_index`
    # (used for incremental diffing) reads the actual `files` table, never
    # project.json. The next successful `rune update` recomputes and
    # rewrites these fields regardless of what they said before, so this
    # self-heals. The reverse ordering (write project.json first) would be
    # worse: a subsequent rebuild_cache failure would leave project.json
    # confidently reporting a fresh index that was never actually written.
    if updated_project is not None:
        write_json_model(layout.project_json, updated_project)

    stats.update(
        {
            "files_scanned": len(scanned),
            "files_parsed": files_parsed,
            "files_reused": len(changeset.unchanged),
            "files_deleted": len(changeset.deleted_paths),
            "scope_files_auto_assigned": len(auto_assigned_scope_ids),
            "semantic_scopes_refreshed": len(new_semantic_revisions),
            **{f"semantic_{key}": value for key, value in semantic_metrics_summary.items()},
        }
    )
    return stats
