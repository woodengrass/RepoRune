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
    for component in nx.connected_components(graph):
        if len(component) < 2:
            continue
        files = tuple(sorted(component))
        common_parent = _common_parent(files)
        label = common_parent or "connected-component"
        candidates.append(
            ScopeCandidate(
                id=scope_id_for_path(label),
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
