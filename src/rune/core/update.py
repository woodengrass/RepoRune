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
from rune.core.index.scanner import (
    ScannedFile,
    diff_against_previous,
    scan_files_with_issues,
)
from rune.core.index.treesitter import RawReference, get_parser_adapter
from rune.core.memory.records import current_by_note_id, current_by_record_id
from rune.core.memory.staleness import (
    detect_constraint_transitions,
    detect_decision_transitions,
    detect_note_transitions,
)
from rune.core.project import RuneLayout, utc_now_iso
from rune.core.scopes.model import (
    assign_new_files_from_imports,
    load_scopes,
    save_scopes,
)
from rune.core.semantic.provider import (
    ModelProvider,
    SemanticHealthCheck,
    SemanticHealthStatus,
    check_semantic_health,
)
from rune.core.semantic.worker import (
    aggregate_metrics,
    detect_orphaned_scopes,
    mark_possibly_stale,
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
    MemoryRevision,
    Note,
    ProjectFile,
    RecordType,
    RuneConfig,
    ScopeSummary,
    Symbol,
)
from rune.core.storage.sqlite.materialize import (
    CodeIndexData,
    SemanticRunMetricsRecord,
    current_scope_summaries,
    discard_cache,
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


def _build_semantic_providers(
    config: RuneConfig,
) -> tuple[ModelProvider | None, ModelProvider | None, SemanticHealthCheck]:
    """Runs the three-tier provider health check (ARCHITECTURE.md §4.5,
    `check_semantic_health`) and returns the (primary, fallback) providers
    to use this run alongside the check's own result. `primary` is only
    ever non-None when `health.status is SemanticHealthStatus.ok`; every
    other status means semantic refresh can't run this update at all —
    disabled in config, a Step 1 config error (empty model / missing API
    key), or a Step 2 startup probe that failed (rate-limited or, after
    one retry, some other failure). Never raises: a semantic-provider
    problem must never abort the deterministic index update
    (ARCHITECTURE.md §4.9's failure-isolation principle applies here too);
    the caller uses `health` to decide whether to fall back to
    `mark_possibly_stale` and what to tell the user.
    """
    health, primary, fallback = check_semantic_health(config.semantic)
    return primary, fallback, health


def _append_semantic_log(layout: RuneLayout, lines: list[str]) -> None:
    if not lines:
        return
    layout.logs_dir.mkdir(parents=True, exist_ok=True)
    with layout.semantic_log.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(f"{utc_now_iso()} {line}\n")


def _close_provider(provider: ModelProvider | None) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        close()


def run_update(layout: RuneLayout, full: bool = False) -> dict[str, int | float | str | None]:
    config = load_config(layout.config_path)
    repo_root = layout.repo_root
    now = utc_now_iso()

    # A full update parses every readable file from scratch, but still needs
    # the prior facts for a path the scanner could not read this run.
    indexed_before_scan = read_current_code_index(layout)
    previous = CodeIndexData() if full else indexed_before_scan
    previous_hashes = {f.path: f.content_hash for f in previous.files}
    previous_status_by_path = {f.path: f.status for f in previous.files}
    symbols_by_path: dict[str, list[Symbol]] = defaultdict(list)
    for s in previous.symbols:
        symbols_by_path[s.file].append(s)
    edges_by_path: dict[str, list[Edge]] = defaultdict(list)
    for e in previous.edges:
        edges_by_path[e.source_file].append(e)

    scan_result = scan_files_with_issues(repo_root, config.index)
    scanned = scan_result.files
    changeset = diff_against_previous(scanned, previous_hashes)
    unreadable_paths = set(scan_result.unreadable_paths)
    deleted_paths = [path for path in changeset.deleted_paths if path not in unreadable_paths]

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
        if previous_status is IndexedFileStatus.scan_error:
            # A transient scanner failure retains the last good index only
            # until this path becomes readable again. Reparse even when its
            # content hash matches so the status can recover to `ok`.
            symbols, import_edges, raw_references, status = _parse_file(repo_root, scanned_file)
            files_parsed += 1
            new_files.append(_to_indexed_file(scanned_file, now, status))
            new_symbols.extend(symbols)
            new_edges.extend(import_edges)
            pending_references[scanned_file.path] = raw_references
            continue
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

    unreadable_existing_paths = unreadable_paths & {
        file.path for file in indexed_before_scan.files
    }
    for path in sorted(unreadable_existing_paths):
        previous_file = next(file for file in indexed_before_scan.files if file.path == path)
        new_files.append(previous_file.model_copy(update={"status": IndexedFileStatus.scan_error}))
        new_symbols.extend(
            symbol for symbol in indexed_before_scan.symbols if symbol.file == path
        )
        # This source file cannot be reread, so preserve every last-known
        # edge rather than discarding reference facts solely due to a
        # transient filesystem failure.
        new_edges.extend(
            edge for edge in indexed_before_scan.edges if edge.source_file == path
        )

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
    semantic_health: SemanticHealthCheck | None = None
    possibly_stale_revisions: list[ScopeSummary] = []
    if not full:
        primary_provider, fallback_provider, semantic_health = _build_semantic_providers(config)
        try:
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
            else:
                # No provider usable this run at all -- disabled, a Step 1
                # config error, or a failed Step 2 startup probe
                # (ARCHITECTURE.md §4.5). DATA_MODEL.md §2.4: a scope whose
                # member files changed since its last real summary must not
                # keep silently reporting fresh/stale against content that no
                # longer matches, so it gets a possibly_stale marker instead.
                possibly_stale_revisions = mark_possibly_stale(
                    scopes_for_semantic,
                    current_summaries,
                    {f.path: f.content_hash for f in new_files},
                    new_symbols,
                    now,
                )
                new_semantic_revisions.extend(possibly_stale_revisions)
        finally:
            _close_provider(primary_provider)
            _close_provider(fallback_provider)

    if new_semantic_revisions:
        semantic_override = [*existing_semantic, *new_semantic_revisions]

    # Decision/Constraint/Note lifecycle transitions (Milestone 6,
    # ARCHITECTURE.md §4.6, DATA_MODEL.md §6): a pure structural/hash
    # comparison, never an LLM call, so unlike the semantic refresh above
    # this always runs — including on `rune rebuild-cache` (full=True).
    # Same in-memory-then-defer-canonical-write pattern as scopes/semantic
    # above: the new revisions must land in this run's rebuild_cache
    # transaction, but decisions.jsonl/constraints.jsonl/notes.jsonl are
    # only appended to after rebuild_cache actually succeeds.
    known_scope_ids = {scope.id for scope in scopes_for_semantic}
    known_files = {f.path for f in new_files}
    known_symbol_ids = {s.symbol_id for s in new_symbols}
    file_hashes = {f.path: f.content_hash for f in new_files}
    symbol_owning_file = {s.symbol_id: s.file for s in new_symbols}
    scope_by_id = {scope.id: scope for scope in scopes_for_semantic}

    existing_decisions = read_jsonl(layout.decisions_jsonl, MemoryRevision)
    existing_constraints = read_jsonl(layout.constraints_jsonl, MemoryRevision)
    existing_notes = read_jsonl(layout.notes_jsonl, Note)
    current_decisions = current_by_record_id(
        [r for r in existing_decisions if r.type is RecordType.decision]
    )
    current_constraints = current_by_record_id(
        [r for r in existing_constraints if r.type is RecordType.constraint]
    )
    current_notes = current_by_note_id(existing_notes)

    new_decision_revisions = detect_decision_transitions(
        current_decisions, known_scope_ids, known_files, known_symbol_ids, now
    )
    new_constraint_revisions = detect_constraint_transitions(
        current_constraints, known_scope_ids, known_files, known_symbol_ids,
        file_hashes, symbol_owning_file, scope_by_id, now,
    )
    new_note_revisions = detect_note_transitions(
        current_notes, known_scope_ids, file_hashes, symbol_owning_file, now
    )

    decisions_override = (
        [*existing_decisions, *new_decision_revisions] if new_decision_revisions else None
    )
    constraints_override = (
        [*existing_constraints, *new_constraint_revisions] if new_constraint_revisions else None
    )
    notes_override = [*existing_notes, *new_note_revisions] if new_note_revisions else None

    stats: dict[str, int | float | str | None] = rebuild_cache(
        layout,
        code_index=CodeIndexData(files=new_files, symbols=new_symbols, edges=new_edges),
        scopes_override=updated_scopes_file,
        semantic_override=semantic_override,
        semantic_run_metrics=semantic_run_metrics_record,
        decisions_override=decisions_override,
        constraints_override=constraints_override,
        notes_override=notes_override,
    )
    try:
        if updated_scopes_file is not None:
            save_scopes(layout, updated_scopes_file)
        append_jsonl_many(layout.semantic_jsonl, new_semantic_revisions)
        _append_semantic_log(layout, semantic_local_log)
        append_jsonl_many(layout.decisions_jsonl, new_decision_revisions)
        append_jsonl_many(layout.constraints_jsonl, new_constraint_revisions)
        append_jsonl_many(layout.notes_jsonl, new_note_revisions)
        if updated_project is not None:
            write_json_model(layout.project_json, updated_project)
    except BaseException:
        # Canonical is authoritative. If any deferred canonical write fails
        # after SQLite committed, remove the now-untrustworthy derived cache
        # rather than letting readers observe data canonical does not contain.
        try:
            discard_cache(layout)
        except OSError:
            pass
        raise

    stats.update(
        {
            "files_scanned": len(scanned),
            "files_parsed": files_parsed,
            "files_reused": len(changeset.unchanged),
            "files_deleted": len(deleted_paths),
            "files_scan_errors": len(scan_result.unreadable_paths),
            "scope_files_auto_assigned": len(auto_assigned_scope_ids),
            "semantic_scopes_refreshed": len(new_semantic_revisions),
            "semantic_scopes_possibly_stale": len(possibly_stale_revisions),
            "decisions_transitioned": len(new_decision_revisions),
            "constraints_transitioned": len(new_constraint_revisions),
            "notes_transitioned": len(new_note_revisions),
            **{f"semantic_{key}": value for key, value in semantic_metrics_summary.items()},
        }
    )
    # `disabled`/`ok` are the expected, silent-by-design outcomes (see
    # SemanticHealthStatus) and deliberately don't surface here at all --
    # everything else must reach the CLI as a distinct, clearly-labeled
    # field rather than blending into the generic k=v stats line, so it
    # can't be missed the way a silently-skipped semantic refresh could be
    # before this health check existed.
    if semantic_health is not None and semantic_health.status not in (
        SemanticHealthStatus.ok,
        SemanticHealthStatus.disabled,
    ):
        stats["semantic_health_status"] = semantic_health.status.value
        stats["semantic_health_message"] = semantic_health.message or ""
    return stats
