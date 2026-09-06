"""Project identity, code index size, and working-tree freshness --
shared by `rune status` and `core.retrieval.context`'s soft bootstrap
(Milestone 7), which needs the exact same freshness computation `rune
status` already does rather than a second, subtly different one.

Extracted from `cli.main.status` (previously inline in the CLI command,
the only caller) when Milestone 7 needed the same computation from core.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from rune.core.config import load_config
from rune.core.hashing import working_tree_fingerprint
from rune.core.index.scanner import diff_against_previous, scan_files
from rune.core.project import RuneLayout
from rune.core.storage.canonical import read_json_model
from rune.core.storage.models import ProjectFile


@dataclass(frozen=True)
class ProjectStatus:
    project_id: str
    name: str
    last_indexed_head: str | None
    last_indexed_tree_hash: str | None
    last_indexed_at: str | None
    cache_exists: bool
    files_indexed: int
    symbols_indexed: int
    working_tree_fresh: bool
    files_modified: int
    files_added: int
    files_deleted: int


def compute_status(layout: RuneLayout) -> ProjectStatus | None:
    """Returns `None` if `project.json` is missing/unreadable (mirrors
    the CLI's own "missing or unreadable" error case -- the caller
    decides how to report that, this function doesn't raise for it).
    Never raises for a scan hiccup either: a failed working-tree scan
    just reports `working_tree_fresh=False` with zero counts, same
    failure-isolation principle used everywhere else in this project.
    """
    project = read_json_model(layout.project_json, ProjectFile)
    if project is None:
        return None

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

    is_fresh = current_tree_hash is not None and current_tree_hash == project.last_indexed_tree_hash

    return ProjectStatus(
        project_id=project.project_id,
        name=project.name,
        last_indexed_head=project.last_indexed_head,
        last_indexed_tree_hash=project.last_indexed_tree_hash,
        last_indexed_at=project.last_indexed_at,
        cache_exists=cache_exists,
        files_indexed=file_count,
        symbols_indexed=symbol_count,
        working_tree_fresh=is_fresh,
        files_modified=modified_count,
        files_added=added_count,
        files_deleted=deleted_count,
    )
