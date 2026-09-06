"""Resolves a language's raw import specifiers to repo-relative file paths.

ARCHITECTURE.md §4.3: imports are high confidence as *edges that a real
import statement exists* (confidence=1.0 always), independent of whether
the specifier could be resolved to an actual file in the repo. An
unresolved specifier (a bare package name, a path alias, a namespace
package) is recorded with `target_file=None` — not an error, not a lower
confidence score. V1 deliberately does not chase tsconfig `paths`/
`baseUrl`, Python `PYTHONPATH`, or monorepo package boundaries.
"""

from __future__ import annotations

import posixpath
from pathlib import Path

from rune.core.index.treesitter import RawImport
from rune.core.storage.models import Edge, EdgeType

_JS_TRY_SUFFIXES = (
    "",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    "/index.ts",
    "/index.tsx",
    "/index.js",
    "/index.jsx",
)


def _resolve_python_import(repo_root: Path, source_path: str, specifier: str) -> str | None:
    source_dir = posixpath.dirname(source_path)
    if specifier.startswith("."):
        dots = len(specifier) - len(specifier.lstrip("."))
        remainder = specifier[dots:]
        if not remainder:
            # `from . import sibling` / `from .. import sibling`: our
            # RawImport only captures the relative_import prefix (the
            # dots), never the names after `import` -- we don't know
            # whether "sibling" is a submodule (package/sibling.py) or a
            # name defined in the package's __init__.py, since we don't
            # capture imported-name lists (spec's "best effort" for
            # imports, not full name resolution). Resolving this to the
            # package's own __init__.py anyway would be a confident wrong
            # answer for a "high confidence" edge type -- worse than
            # leaving it unresolved, so: return None instead of guessing.
            return None
        base = source_dir
        for _ in range(dots - 1):
            base = posixpath.dirname(base)
        candidate_base = posixpath.normpath(posixpath.join(base, remainder.replace(".", "/")))
    else:
        candidate_base = specifier.replace(".", "/")

    for suffix_path in (f"{candidate_base}.py", f"{candidate_base}/__init__.py"):
        if (repo_root / suffix_path).is_file():
            return suffix_path
    return None


def _resolve_relative_js_import(repo_root: Path, source_path: str, specifier: str) -> str | None:
    if not specifier.startswith("."):
        return None  # bare package import — not resolved in V1
    source_dir = posixpath.dirname(source_path)
    combined = posixpath.normpath(posixpath.join(source_dir, specifier))
    for suffix in _JS_TRY_SUFFIXES:
        candidate = combined + suffix
        if (repo_root / candidate).is_file():
            return candidate
    return None


def resolve_import_target(
    repo_root: Path, source_path: str, language: str, specifier: str
) -> str | None:
    if language == "python":
        return _resolve_python_import(repo_root, source_path, specifier)
    if language in ("javascript", "typescript"):
        return _resolve_relative_js_import(repo_root, source_path, specifier)
    return None


def build_import_edges(
    repo_root: Path, source_path: str, language: str, raw_imports: list[RawImport]
) -> list[Edge]:
    edges: list[Edge] = []
    for raw in raw_imports:
        target = resolve_import_target(repo_root, source_path, language, raw.specifier)
        edges.append(
            Edge(
                source_symbol=None,
                source_file=source_path,
                target_symbol=None,
                target_file=target,
                edge_type=EdgeType.imports,
                confidence=1.0,
            )
        )
    return edges
