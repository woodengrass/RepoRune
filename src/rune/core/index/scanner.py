"""Include/exclude glob walk + content-hash-based change detection.

See ARCHITECTURE.md §4.1: this module is the only place that decides "what
changed" — content hash comparison against the previously indexed state,
not mtime (mtime is recorded for information but never trusted for
change detection, since checkouts/clones routinely produce fresh mtimes
for unchanged content).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from rune.core.hashing import content_hash_of_file, git_blob_hash
from rune.core.storage.models import IndexConfig

# Directories never worth descending into regardless of config — walking
# them is either pointless (VCS internals) or pathologically expensive
# (dependency trees). Configurable excludes still apply on top of this.
_ALWAYS_PRUNED_DIR_NAMES = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv"})

LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translates a `**`-aware glob into a regex anchored on a posix
    relative path. `**/` matches zero or more path segments, `**` alone
    matches anything (incl. `/`), `*` matches within one segment, `?`
    matches one character within a segment.
    """
    pattern = pattern.replace("\\", "/")
    parts: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern[i : i + 3] == "**/":
            parts.append("(?:.*/)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


@dataclass(frozen=True)
class ScannedFile:
    path: str  # repo-relative, posix separators
    absolute_path: Path
    language: str
    content_hash: str
    git_blob_hash: str | None
    size: int
    mtime: float


def scan_files(repo_root: Path, index_config: IndexConfig) -> list[ScannedFile]:
    """Walks `repo_root`, returns every file that matches `include` and
    not `exclude`, and whose extension maps to a supported language
    (LANGUAGE_BY_EXTENSION) — unsupported files are not part of the code
    index in V1 and are silently skipped here, not an error.
    """
    include_patterns = [_glob_to_regex(p) for p in index_config.include]
    exclude_patterns = [_glob_to_regex(p) for p in index_config.exclude]

    results: list[ScannedFile] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _ALWAYS_PRUNED_DIR_NAMES]
        for filename in filenames:
            absolute_path = Path(dirpath) / filename
            language = LANGUAGE_BY_EXTENSION.get(absolute_path.suffix)
            if language is None:
                continue
            rel_posix = absolute_path.relative_to(repo_root).as_posix()
            if not any(p.match(rel_posix) for p in include_patterns):
                continue
            if any(p.match(rel_posix) for p in exclude_patterns):
                continue
            stat = absolute_path.stat()
            results.append(
                ScannedFile(
                    path=rel_posix,
                    absolute_path=absolute_path,
                    language=language,
                    content_hash=content_hash_of_file(absolute_path),
                    git_blob_hash=git_blob_hash(repo_root, absolute_path),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )
    return results


@dataclass(frozen=True)
class Changeset:
    added: list[ScannedFile]
    modified: list[ScannedFile]
    unchanged: list[ScannedFile]
    deleted_paths: list[str]


def diff_against_previous(
    current: list[ScannedFile], previous_hashes: dict[str, str]
) -> Changeset:
    """Compares freshly scanned files against the previously indexed
    content_hash per path. A path present before but absent now is
    `deleted_paths` — the caller (core.update) simply omits it from the
    next materialization rather than needing an explicit delete step.
    """
    added: list[ScannedFile] = []
    modified: list[ScannedFile] = []
    unchanged: list[ScannedFile] = []
    current_paths = set()

    for scanned in current:
        current_paths.add(scanned.path)
        previous_hash = previous_hashes.get(scanned.path)
        if previous_hash is None:
            added.append(scanned)
        elif previous_hash != scanned.content_hash:
            modified.append(scanned)
        else:
            unchanged.append(scanned)

    deleted_paths = [p for p in previous_hashes if p not in current_paths]
    return Changeset(added=added, modified=modified, unchanged=unchanged, deleted_paths=deleted_paths)
