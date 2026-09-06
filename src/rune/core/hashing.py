"""Content hashing helpers: file content hash, git blob hash, working-tree
fingerprint (see ARCHITECTURE.md §9, DATA_MODEL.md §2.1).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def content_hash(data: bytes) -> str:
    """sha256 of file bytes, as used for IndexedFile.content_hash."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def content_hash_of_file(path: Path) -> str:
    return content_hash(path.read_bytes())


def git_blob_hash(repo_root: Path, path: Path) -> str | None:
    """`git hash-object` for a tracked file. Returns None if git is
    unavailable or the file isn't tracked/readable by git.
    """
    try:
        result = subprocess.run(
            ["git", "hash-object", str(path)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return "sha1:" + result.stdout.strip()


def working_tree_fingerprint(file_hashes: dict[str, str]) -> str:
    """rune-computed tree hash: sha256 over sorted "path\\0hash\\n" pairs.

    This is independent of git HEAD so it can detect dirty working trees
    (uncommitted changes) even when HEAD hasn't moved. See ARCHITECTURE.md §9.
    """
    hasher = hashlib.sha256()
    for path in sorted(file_hashes):
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(file_hashes[path].encode("utf-8"))
        hasher.update(b"\n")
    return "sha256:" + hasher.hexdigest()
