"""Snapshot-hash helpers shared by Decision/Constraint approval and Note
add/update (DATA_MODEL.md §2.5, §2.6). Two distinct kinds of snapshot exist
and must not be confused (ARCHITECTURE.md §4.6):

- `source_hashes` (`persistence_mode=source_bound`, or any Note with
  `files`/`symbols` set): per-file `content_hash`, detects the referenced
  *content* changing.
- `scope_hashes` (`persistence_mode=scope_bound`): a hash of a scope's own
  *membership list* (which files/symbols belong to it), detects the scope's
  membership changing -- unrelated to file content.
"""

from __future__ import annotations

import hashlib

from rune.core.storage.models import Scope


def compute_source_hashes(
    files: list[str],
    symbols: list[str],
    file_hashes: dict[str, str],
    symbol_owning_file: dict[str, str],
) -> dict[str, str]:
    """Snapshot of `content_hash` for every referenced file, unioned with
    the owning file of every referenced symbol -- same union rule as
    `core.semantic.worker.compute_source_files` (DATA_MODEL.md §2.4's
    invariant, reused here for §2.5's `source_hashes`). A path that no
    longer resolves to a currently-indexed file (deleted, unparseable) is
    silently dropped -- there's no content left to hash for it, and its
    disappearance is instead what the existence-based `review_required`/
    `orphaned` staleness check (`core.memory.staleness`) reacts to, not
    this hash comparison.
    """
    paths = set(files)
    for symbol_id in symbols:
        owning_file = symbol_owning_file.get(symbol_id)
        if owning_file is not None:
            paths.add(owning_file)
    return {path: file_hashes[path] for path in paths if path in file_hashes}


def compute_scope_membership_hash(scope: Scope) -> str:
    """A scope's membership fingerprint: sha256 over its own sorted
    `files`/`symbols` lists. Deliberately independent of file *content*
    (that's `compute_source_hashes`'s job) -- this only changes when
    something is added to or removed from the scope, e.g. `rune scope
    edit` or the import-only incremental auto-assignment (Milestone 4).
    """
    hasher = hashlib.sha256()
    for path in sorted(scope.members.files):
        hasher.update(b"f\0")
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\n")
    for symbol_id in sorted(scope.members.symbols):
        hasher.update(b"s\0")
        hasher.update(symbol_id.encode("utf-8"))
        hasher.update(b"\n")
    return "sha256:" + hasher.hexdigest()
