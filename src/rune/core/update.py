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
from rune.core.index.scanner import ScannedFile, diff_against_previous, scan_files
from rune.core.index.treesitter import get_parser_adapter
from rune.core.project import RuneLayout, utc_now_iso
from rune.core.storage.canonical import read_json_model, write_json_model
from rune.core.storage.models import (
    Edge,
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
) -> tuple[list[Symbol], list[Edge], IndexedFileStatus]:
    """Parses one file. A parse failure marks that file `parse_error` and
    yields no symbols/edges for it — it must never abort the whole update
    (spec §62's failure-isolation principle, applied here to indexing).

    Catches `Exception` broadly and deliberately: tree-sitter's grammars
    are error-tolerant for genuinely malformed source (they produce ERROR/
    partial nodes rather than raising), but a file that is unreadable in
    some unexpected way, or an AST shape our walkers don't defensively
    guard against (e.g. a `MISSING` node tree-sitter synthesizes during
    error recovery, which can leave an expected field `None`), must still
    degrade to "this one file didn't index" rather than take the whole
    `rune update` down.
    """
    try:
        source = scanned.absolute_path.read_bytes()
        adapter = get_parser_adapter(scanned.language, scanned.path)
        symbols = adapter.extract_symbols(scanned.path, source)
        raw_imports = adapter.extract_imports(scanned.path, source)
        edges = build_import_edges(repo_root, scanned.path, scanned.language, raw_imports)
    except Exception:  # noqa: BLE001 - intentional: isolate one file's parse failure
        return [], [], IndexedFileStatus.parse_error
    return symbols, edges, IndexedFileStatus.ok


def run_update(layout: RuneLayout, full: bool = False) -> dict[str, int]:
    config = load_config(layout.config_path)
    repo_root = layout.repo_root
    now = utc_now_iso()

    previous = CodeIndexData() if full else read_current_code_index(layout)
    previous_hashes = {f.path: f.content_hash for f in previous.files}
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

    for scanned_file in changeset.unchanged:
        new_files.append(_to_indexed_file(scanned_file, now, IndexedFileStatus.ok))
        new_symbols.extend(symbols_by_path.get(scanned_file.path, []))
        new_edges.extend(edges_by_path.get(scanned_file.path, []))

    for scanned_file in (*changeset.added, *changeset.modified):
        symbols, edges, status = _parse_file(repo_root, scanned_file)
        files_parsed += 1
        new_files.append(_to_indexed_file(scanned_file, now, status))
        new_symbols.extend(symbols)
        new_edges.extend(edges)

    stats = rebuild_cache(
        layout, code_index=CodeIndexData(files=new_files, symbols=new_symbols, edges=new_edges)
    )

    tree_hash = working_tree_fingerprint({f.path: f.content_hash for f in new_files})
    project = read_json_model(layout.project_json, ProjectFile)
    if project is not None:
        updated_project = project.model_copy(
            update={
                "last_indexed_head": _git_head(repo_root),
                "last_indexed_tree_hash": tree_hash,
                "last_indexed_at": now,
            }
        )
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
