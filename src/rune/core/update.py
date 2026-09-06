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
from rune.core.storage.canonical import read_json_model, write_json_model
from rune.core.storage.models import (
    Edge,
    EdgeType,
    IndexedFile,
    IndexedFileStatus,
    ProjectFile,
    Symbol,
)
from rune.core.storage.sqlite.materialize import (
    CodeIndexData,
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


def run_update(layout: RuneLayout, full: bool = False) -> dict[str, int]:
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
        new_edges.extend(edges_by_path.get(scanned_file.path, []))

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

    stats = rebuild_cache(
        layout, code_index=CodeIndexData(files=new_files, symbols=new_symbols, edges=new_edges)
    )

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
        }
    )
    return stats
