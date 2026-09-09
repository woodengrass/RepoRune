"""Graph-assisted, human-reviewed scope suggestions."""

from __future__ import annotations

import sqlite3

import networkx as nx

from rune.core.scopes.heuristics import ScopeCandidate, scope_id_for_path

_CLUSTER_EDGE_TYPES = ("imports", "calls", "extends", "implements")


def suggest_from_graph(conn: sqlite3.Connection, excluded_files: set[str] | None = None) -> list[ScopeCandidate]:
    """Returns connected components from import and best-effort reference edges.

    Candidates are suggestions only. Callers must obtain human confirmation
    before writing them to scopes.json.
    """
    excluded_files = excluded_files or set()
    graph = nx.Graph()
    rows = conn.execute(
        "SELECT source_file, target_file FROM edges "
        "WHERE target_file IS NOT NULL AND edge_type IN (?, ?, ?, ?)",
        _CLUSTER_EDGE_TYPES,
    )
    for source_file, target_file in rows:
        if source_file not in excluded_files and target_file not in excluded_files:
            graph.add_edge(source_file, target_file)

    candidates: list[ScopeCandidate] = []
    # Sorted up front so the `-2`/`-3` disambiguation below assigns the
    # same ids on every run: `connected_components` iteration order follows
    # graph insertion order, which itself follows unordered SQLite row
    # order (same lessons as references.py's `sorted(imported_files)`).
    components = sorted(
        tuple(sorted(component))
        for component in nx.connected_components(graph)
        if len(component) >= 2
    )
    seen_ids: set[str] = set()
    for files in components:
        common_parent = _common_parent(files)
        # A component with no common parent has no directory-derived name.
        # Derive the fallback from the component's own files (instead of a
        # constant "connected-component") so two unrelated islands never
        # share one id -- callers other than the CLI, which dedups via
        # `_unique_candidate_id`, must not receive duplicates.
        label = common_parent or f"connected-{files[0]}"
        base_id = scope_id_for_path(label) or "connected-component"
        candidate_id = base_id
        suffix = 2
        while candidate_id in seen_ids:
            candidate_id = f"{base_id}-{suffix}"
            suffix += 1
        seen_ids.add(candidate_id)
        candidates.append(
            ScopeCandidate(
                id=candidate_id,
                name=label.replace("-", " ").replace("_", " ").title(),
                files=files,
                reason="Files are connected by import/reference graph edges.",
            )
        )
    return sorted(candidates, key=lambda candidate: (candidate.id, candidate.files))


def _common_parent(files: tuple[str, ...]) -> str:
    directories = [file_path.rsplit("/", 1)[0] if "/" in file_path else "" for file_path in files]
    prefix = directories[0].split("/")
    for directory in directories[1:]:
        parts = directory.split("/")
        prefix = prefix[: min(len(prefix), len(parts))]
        while prefix and prefix != parts[: len(prefix)]:
            prefix.pop()
    return "/".join(prefix)
