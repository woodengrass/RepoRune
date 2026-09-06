from __future__ import annotations

import sqlite3
from pathlib import Path

from rune.core.project import RuneLayout, init_project
from rune.core.update import run_update


def test_python_simple_fixture_indexes_expected_symbols(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    stats = run_update(layout, full=True)

    assert stats["files"] == 3  # __init__.py, main.py, services.py
    assert stats["files_parsed"] == 3
    assert stats["files_reused"] == 0

    conn = sqlite3.connect(str(layout.memory_db))
    qnames = {row[0] for row in conn.execute("SELECT qualified_name FROM symbols")}
    assert "UserService" in qnames
    assert "UserService.get_user" in qnames
    assert "run" in qnames
    assert "DEFAULT_TIMEOUT" in qnames

    # `.services` relative import from app/main.py must resolve to the real file
    edge = conn.execute(
        "SELECT target_file FROM edges WHERE source_file = ? AND target_file IS NOT NULL",
        ("app/main.py",),
    ).fetchone()
    assert edge == ("app/services.py",)

    # the unresolvable stdlib import must still be recorded, just unresolved
    stdlib_edges = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE source_file = ? AND edge_type = 'imports' "
        "AND target_file IS NULL",
        ("app/main.py",),
    ).fetchone()[0]
    assert stdlib_edges == 1


def test_python_simple_fixture_resolves_calls_across_files(python_simple_repo: Path) -> None:
    """Hand-verified reference query (Milestone 3's explicitly-permitted
    exception to "no hand-checked fixtures", since best-effort resolution
    can't be judged any other way): app/main.py's `run()` calls
    `UserService(...)` and `service.get_user(1)`, both of which should
    resolve into app/services.py via the import edge between the two
    files. `self.db.fetch(...)` inside get_user has no matching symbol
    anywhere and must be recorded unresolved, not dropped.
    """
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)

    conn = sqlite3.connect(str(layout.memory_db))
    run_id = conn.execute(
        "SELECT symbol_id FROM symbols WHERE qualified_name = 'run'"
    ).fetchone()[0]
    user_service_id = conn.execute(
        "SELECT symbol_id FROM symbols WHERE qualified_name = 'UserService'"
    ).fetchone()[0]
    get_user_id = conn.execute(
        "SELECT symbol_id FROM symbols WHERE qualified_name = 'UserService.get_user'"
    ).fetchone()[0]

    calls_from_run = {
        row[0]
        for row in conn.execute(
            "SELECT target_symbol FROM edges WHERE edge_type = 'calls' AND source_symbol = ?",
            (run_id,),
        )
    }
    assert calls_from_run == {user_service_id, get_user_id}

    unresolved_calls_in_get_user = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE edge_type = 'calls' AND source_symbol = ? "
        "AND target_symbol IS NULL",
        (get_user_id,),
    ).fetchone()[0]
    assert unresolved_calls_in_get_user == 1  # self.db.fetch(...)


def test_extends_and_implements_resolve_end_to_end(tmp_path: Path) -> None:
    """A small hand-built repo (not the shared fixtures, to avoid coupling
    every other fixture-based test's symbol/edge counts to this one) that
    exercises `extends`/`implements` through the full init -> update ->
    materialize pipeline, not just the unit-level extraction tests.
    """
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "shapes.ts").write_text(
        "export interface Shape { area(): number; }\n"
        "export class Base {}\n"
        "export class Circle extends Base implements Shape {\n"
        "  area(): number { return 0; }\n"
        "}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        cwd=repo, check=True,
    )

    layout = init_project(repo)
    run_update(layout, full=True)

    conn = sqlite3.connect(str(layout.memory_db))
    circle_id = conn.execute(
        "SELECT symbol_id FROM symbols WHERE qualified_name = 'Circle'"
    ).fetchone()[0]
    base_id = conn.execute(
        "SELECT symbol_id FROM symbols WHERE qualified_name = 'Base'"
    ).fetchone()[0]
    shape_id = conn.execute(
        "SELECT symbol_id FROM symbols WHERE qualified_name = 'Shape'"
    ).fetchone()[0]

    extends_target = conn.execute(
        "SELECT target_symbol FROM edges WHERE edge_type = 'extends' AND source_symbol = ?",
        (circle_id,),
    ).fetchone()[0]
    implements_target = conn.execute(
        "SELECT target_symbol FROM edges WHERE edge_type = 'implements' AND source_symbol = ?",
        (circle_id,),
    ).fetchone()[0]
    assert extends_target == base_id
    assert implements_target == shape_id


def test_reference_resolution_entirely_unresolved_does_not_break_update(
    python_simple_repo: Path, monkeypatch
) -> None:
    """ARCHITECTURE.md §4.3's resilience requirement: Scope/Constraint
    (and everything else) must not depend on the reference graph being
    complete. Simulated here by forcing every single reference to come
    back unresolved (as if resolution matched nothing at all) and
    confirming `rune update` still completes, the cache is still
    consistent, and scope membership (which never depended on references
    in the first place, per Milestone 1's design) is unaffected.
    """
    import rune.core.update as update_module
    from rune.core.storage.canonical import write_json_model
    from rune.core.storage.models import Scope, ScopeMembers, ScopesFile, ScopeSource

    def always_unresolved(file_path, raw_references, symbols_by_path, imported_files):
        from rune.core.storage.models import Edge

        return [
            Edge(
                source_symbol=None, source_file=file_path,
                target_symbol=None, target_file=None,
                edge_type=ref.edge_type, confidence=0.0,
            )
            for ref in raw_references
        ]

    monkeypatch.setattr(update_module, "resolve_references", always_unresolved)

    layout = init_project(python_simple_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(
            scopes=[
                Scope(
                    id="app", name="App", source=ScopeSource.human,
                    members=ScopeMembers(files=["app/main.py"]),
                )
            ]
        ),
    )

    stats = run_update(layout, full=True)  # must not raise

    conn = sqlite3.connect(str(layout.memory_db))
    unresolved_refs = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE edge_type = 'calls' AND target_symbol IS NULL"
    ).fetchone()[0]
    assert unresolved_refs > 0  # the forced-unresolved path actually ran

    # scope membership, which never depends on the reference graph, is
    # completely unaffected by references being 100% unresolved
    scope_row = conn.execute(
        "SELECT file FROM scope_files WHERE scope_id = 'app'"
    ).fetchone()
    assert scope_row == ("app/main.py",)
    assert stats["files"] == 3


def test_ts_simple_fixture_indexes_expected_symbols_and_edge(ts_simple_repo: Path) -> None:
    layout = init_project(ts_simple_repo)
    run_update(layout, full=True)

    conn = sqlite3.connect(str(layout.memory_db))
    rows = {
        row[0]: row[1]
        for row in conn.execute("SELECT qualified_name, kind FROM symbols")
    }
    assert rows["Vector"] == "interface"
    assert rows["add"] == "function"
    assert rows["Calculator"] == "class"
    assert rows["Calculator.sum"] == "method"
    assert rows["origin"] == "function"

    edge = conn.execute(
        "SELECT target_file FROM edges WHERE source_file = ?", ("src/index.ts",)
    ).fetchone()
    assert edge == ("src/utils.ts",)


def test_update_only_reparses_the_changed_file(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)

    # no changes: a second update must parse nothing
    noop_stats = run_update(layout, full=False)
    assert noop_stats["files_parsed"] == 0
    assert noop_stats["files_reused"] == 3

    # modify exactly one file
    (python_simple_repo / "app" / "services.py").write_text(
        (python_simple_repo / "app" / "services.py").read_text(encoding="utf-8") + "\n# comment\n",
        encoding="utf-8",
    )
    stats = run_update(layout, full=False)
    assert stats["files_parsed"] == 1
    assert stats["files_reused"] == 2


def test_symbol_rename_produces_new_id_and_removes_old(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)

    conn = sqlite3.connect(str(layout.memory_db))
    old_id = conn.execute(
        "SELECT symbol_id FROM symbols WHERE qualified_name = ?", ("UserService.get_user",)
    ).fetchone()[0]

    services_path = python_simple_repo / "app" / "services.py"
    services_path.write_text(
        services_path.read_text(encoding="utf-8").replace("get_user", "fetch_user"),
        encoding="utf-8",
    )
    run_update(layout, full=False)

    conn2 = sqlite3.connect(str(layout.memory_db))
    qnames = {row[0] for row in conn2.execute("SELECT qualified_name FROM symbols")}
    assert "UserService.get_user" not in qnames
    assert "UserService.fetch_user" in qnames
    new_id = conn2.execute(
        "SELECT symbol_id FROM symbols WHERE qualified_name = ?", ("UserService.fetch_user",)
    ).fetchone()[0]
    assert new_id != old_id
    # the old id must be gone entirely, not orphaned
    assert conn2.execute(
        "SELECT COUNT(*) FROM symbols WHERE symbol_id = ?", (old_id,)
    ).fetchone()[0] == 0


def test_deleted_file_is_removed_from_index(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)

    (python_simple_repo / "app" / "main.py").unlink()
    stats = run_update(layout, full=False)
    assert stats["files_deleted"] == 1

    conn = sqlite3.connect(str(layout.memory_db))
    paths = {row[0] for row in conn.execute("SELECT path FROM files")}
    assert "app/main.py" not in paths
    qnames = {row[0] for row in conn.execute("SELECT qualified_name FROM symbols")}
    assert "run" not in qnames


def test_last_indexed_tree_hash_changes_when_content_changes(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    project_after_first = RuneLayout(repo_root=python_simple_repo)
    from rune.core.storage.canonical import read_json_model
    from rune.core.storage.models import ProjectFile

    first = read_json_model(project_after_first.project_json, ProjectFile)
    assert first.last_indexed_tree_hash is not None

    (python_simple_repo / "app" / "main.py").write_text(
        (python_simple_repo / "app" / "main.py").read_text(encoding="utf-8") + "\n# x\n",
        encoding="utf-8",
    )
    run_update(layout, full=False)
    second = read_json_model(project_after_first.project_json, ProjectFile)
    assert second.last_indexed_tree_hash != first.last_indexed_tree_hash


def test_rebuild_cache_full_rescan_matches_incremental_state(python_simple_repo: Path) -> None:
    """rebuild-cache equivalence: a full rescan (`full=True`) must produce
    the same logical code index as the incrementally-updated state, for
    the same on-disk source.
    """
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    (python_simple_repo / "app" / "services.py").write_text(
        (python_simple_repo / "app" / "services.py").read_text(encoding="utf-8") + "\n# c\n",
        encoding="utf-8",
    )
    incremental_stats = run_update(layout, full=False)

    full_stats = run_update(layout, full=True)

    assert incremental_stats["symbols"] == full_stats["symbols"]
    assert incremental_stats["files"] == full_stats["files"]
    assert incremental_stats["edges"] == full_stats["edges"]

    conn = sqlite3.connect(str(layout.memory_db))
    qnames = {row[0] for row in conn.execute("SELECT qualified_name FROM symbols")}
    assert "UserService.get_user" in qnames


def test_one_file_parse_failure_does_not_abort_the_whole_update(
    python_simple_repo: Path, monkeypatch
) -> None:
    """spec §62's failure-isolation principle applied to indexing: a
    single file that can't be parsed must not take down the rest of the
    update. Simulated here by making the parser adapter raise for exactly
    one file, since tree-sitter's own error tolerance makes a *naturally*
    crash-inducing source hard to construct reliably.

    Patches at the `get_parser_adapter` layer (not `_parse_file` itself)
    so the real `_parse_file`'s try/except -- the actual isolation logic
    under test -- stays in the call path. An earlier draft of this test
    replaced `_parse_file` wholesale, which silently discarded the very
    guard it was meant to verify and would have passed even if that guard
    were deleted.
    """
    import rune.core.update as update_module
    from rune.core.index.treesitter import PythonParserAdapter

    layout = init_project(python_simple_repo)
    real_adapter = PythonParserAdapter()

    class FlakyAdapter:
        def extract_symbols(self, path: str, source: bytes) -> list:
            if path.endswith("services.py"):
                raise RuntimeError("simulated parser crash")
            return real_adapter.extract_symbols(path, source)

        def extract_imports(self, path: str, source: bytes) -> list:
            if path.endswith("services.py"):
                raise RuntimeError("simulated parser crash")
            return real_adapter.extract_imports(path, source)

        def extract_references(self, path: str, source: bytes) -> list:
            if path.endswith("services.py"):
                raise RuntimeError("simulated parser crash")
            return real_adapter.extract_references(path, source)

        def has_syntax_error(self, source: bytes) -> bool:
            return real_adapter.has_syntax_error(source)

    monkeypatch.setattr(
        update_module, "get_parser_adapter", lambda language, path="": FlakyAdapter()
    )

    stats = run_update(layout, full=True)

    conn = sqlite3.connect(str(layout.memory_db))
    status = conn.execute(
        "SELECT status FROM files WHERE path = ?", ("app/services.py",)
    ).fetchone()[0]
    assert status == "parse_error"
    # the other two files were still indexed normally
    other_statuses = {
        row[0]
        for row in conn.execute(
            "SELECT status FROM files WHERE path != ?", ("app/services.py",)
        )
    }
    assert other_statuses == {"ok"}
    assert stats["files"] == 3


def test_project_json_write_failure_after_cache_commit_self_heals_next_run(
    python_simple_repo: Path, monkeypatch
) -> None:
    """Known limitation documented in core.update.run_update: the SQLite
    commit and the project.json write are not atomic with each other. If
    project.json's write fails right after a successful cache commit, the
    cache itself must still be correct, and the next `run_update` call
    must still behave correctly (it diffs against the `files` table, not
    against project.json, so a stale project.json cannot corrupt it).
    """
    import rune.core.update as update_module

    layout = init_project(python_simple_repo)
    run_update(layout, full=True)

    def failing_write_json_model(path, model):
        raise OSError("simulated disk-full failure")

    monkeypatch.setattr(update_module, "write_json_model", failing_write_json_model)

    (python_simple_repo / "app" / "services.py").write_text(
        (python_simple_repo / "app" / "services.py").read_text(encoding="utf-8") + "\n# x\n",
        encoding="utf-8",
    )
    try:
        run_update(layout, full=False)
        raise AssertionError("expected the simulated project.json write failure to propagate")
    except OSError:
        pass

    # the cache itself was still correctly committed despite the metadata
    # write failing afterward
    conn = sqlite3.connect(str(layout.memory_db))
    qnames = {row[0] for row in conn.execute("SELECT qualified_name FROM symbols")}
    assert "UserService.get_user" in qnames

    # a subsequent, unpatched update must still work correctly: it diffs
    # against the files table (already up to date), not the stale
    # project.json, so nothing is corrupted by the earlier failure
    monkeypatch.undo()
    stats = run_update(layout, full=False)
    assert stats["files_parsed"] == 0  # nothing changed since the failed attempt
    assert stats["files_reused"] == 3
