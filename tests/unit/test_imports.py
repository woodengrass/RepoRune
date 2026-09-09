from __future__ import annotations

from pathlib import Path

from rune.core.index.imports import build_import_edges, resolve_import_target
from rune.core.index.treesitter import RawImport
from rune.core.storage.models import EdgeType


def _touch(root: Path, rel_path: str) -> None:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")


def test_resolve_python_relative_import(tmp_path: Path) -> None:
    _touch(tmp_path, "app/__init__.py")
    _touch(tmp_path, "app/services.py")
    target = resolve_import_target(tmp_path, "app/main.py", "python", ".services")
    assert target == "app/services.py"


def test_resolve_python_bare_dots_import_is_unresolved_not_package_init(tmp_path: Path) -> None:
    """Regression test: `from . import services` extracts as the raw
    specifier "." (RawImport only captures the relative_import prefix,
    not the names after `import`). An earlier version resolved this to
    the *package's own* __init__.py -- a confidently wrong answer (the
    real target is normally a sibling submodule, not the current
    package's init file) for what's supposed to be a high-confidence edge
    type. Must resolve to None (honestly unresolved) instead of guessing.
    """
    _touch(tmp_path, "app/__init__.py")
    _touch(tmp_path, "app/services.py")
    target = resolve_import_target(tmp_path, "app/main.py", "python", ".")
    assert target is None

    target = resolve_import_target(tmp_path, "app/sub/main.py", "python", "..")
    assert target is None


def test_resolve_python_relative_import_to_package(tmp_path: Path) -> None:
    _touch(tmp_path, "app/sub/__init__.py")
    target = resolve_import_target(tmp_path, "app/main.py", "python", ".sub")
    assert target == "app/sub/__init__.py"


def test_resolve_python_parent_relative_import(tmp_path: Path) -> None:
    _touch(tmp_path, "pkg/mod.py")
    target = resolve_import_target(tmp_path, "pkg/sub/main.py", "python", "..mod")
    assert target == "pkg/mod.py"


def test_resolve_python_absolute_import_within_repo(tmp_path: Path) -> None:
    _touch(tmp_path, "app/services.py")
    target = resolve_import_target(tmp_path, "app/main.py", "python", "app.services")
    assert target == "app/services.py"


def test_resolve_python_unresolvable_stdlib_import_returns_none(tmp_path: Path) -> None:
    target = resolve_import_target(tmp_path, "app/main.py", "python", "os.path")
    assert target is None


def test_resolve_js_relative_import_with_extension_guess(tmp_path: Path) -> None:
    _touch(tmp_path, "src/utils.ts")
    target = resolve_import_target(tmp_path, "src/index.ts", "typescript", "./utils")
    assert target == "src/utils.ts"


def test_resolve_js_relative_import_to_index_file(tmp_path: Path) -> None:
    _touch(tmp_path, "src/widgets/index.ts")
    target = resolve_import_target(tmp_path, "src/index.ts", "typescript", "./widgets")
    assert target == "src/widgets/index.ts"


def test_resolve_js_bare_package_import_returns_none(tmp_path: Path) -> None:
    target = resolve_import_target(tmp_path, "src/index.ts", "typescript", "react")
    assert target is None


def test_resolve_js_parent_escape_outside_repo_is_unresolved(tmp_path: Path) -> None:
    """Regression test: a relative specifier normalizing to outside the repo
    (`../outside` from a top-level file) must resolve to None. `target_file`
    is documented as a repo-relative path — storing an escape path would
    point `repo_root / target` at a file outside the repo.
    """
    outside = tmp_path.parent / "outside_escape_target.ts"
    outside.write_text("export const x = 1;\n", encoding="utf-8")
    try:
        target = resolve_import_target(tmp_path, "index.ts", "typescript", "../outside_escape_target")
        assert target is None
    finally:
        outside.unlink(missing_ok=True)


def test_resolve_python_escape_outside_repo_is_unresolved(tmp_path: Path) -> None:
    """Locks the same containment invariant for the Python resolver: a
    `..`-leading normalized candidate is honestly unresolved, never a
    repo-external path. (Normal relative specifiers clamp at the repo root
    and can't reach this branch today — this test pins the guard, while the
    JS test above is the live-bug regression.)
    """
    target = resolve_import_target(tmp_path, "a.py", "python", "..outside_escape")
    assert target is None


def test_build_import_edges_always_confidence_one_even_when_unresolved(tmp_path: Path) -> None:
    raw = [RawImport(specifier="some-package", line=1), RawImport(specifier="./missing", line=2)]
    edges = build_import_edges(tmp_path, "src/index.ts", "typescript", raw)
    assert len(edges) == 2
    assert all(e.confidence == 1.0 for e in edges)
    assert all(e.edge_type == EdgeType.imports for e in edges)
    assert all(e.target_file is None for e in edges)
