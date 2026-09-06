"""Path-based, non-authoritative scope suggestions."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class ScopeCandidate:
    id: str
    name: str
    files: tuple[str, ...]
    reason: str


def scope_id_for_path(path: str) -> str:
    value = path.lower().replace("/", "-").replace("_", "-")
    return "".join(char if char.isalnum() or char == "-" else "-" for char in value).strip("-")


def suggest_from_paths(files: list[str], excluded_files: set[str] | None = None) -> list[ScopeCandidate]:
    """Groups two or more files sharing a non-root top-level directory."""
    excluded_files = excluded_files or set()
    groups: dict[str, list[str]] = defaultdict(list)
    for file_path in files:
        if file_path in excluded_files:
            continue
        parts = PurePosixPath(file_path).parts
        if len(parts) > 1:
            groups[parts[0]].append(file_path)
    return [
        ScopeCandidate(
            id=scope_id_for_path(directory),
            name=directory.replace("-", " ").replace("_", " ").title(),
            files=tuple(sorted(group)),
            reason=f"{len(group)} files share the {directory}/ directory.",
        )
        for directory, group in sorted(groups.items())
        if len(group) >= 2
    ]
