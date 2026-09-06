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


def test_unchanged_callers_reference_edge_is_re_resolved_after_target_gains_the_symbol(
    python_simple_repo: Path,
) -> None:
    """Regression test: `app/main.py` calls `compute_total()`, which
    doesn't exist anywhere yet, so it's recorded unresolved. Later,
    `compute_total` is added to a file `app/main.py` already imports --
    but `app/main.py` itself is never touched, so it stays in
    `changeset.unchanged` on the next incremental `rune update`. Before
    this fix, an unchanged file's edges were reused verbatim from last
    run's SQLite state, so this reference stayed unresolved forever until
    a full rebuild -- silently diverging from what a full rebuild would
    produce for the exact same source, which is the equivalence guarantee
    `rebuild-cache` depends on. Only `imports` edges are safe to reuse
    verbatim for an unchanged file (resolution depends solely on that
    file's own unchanged import statements); `calls`/`extends`/
    `implements` depend on the *target's* symbol table too, which can
    change on a run where the caller itself doesn't.
    """
    (python_simple_repo / "app" / "main.py").write_text(
        (python_simple_repo / "app" / "main.py").read_text(encoding="utf-8")
        + "\n\ndef run2():\n    compute_total()\n",
        encoding="utf-8",
    )
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)

    conn = sqlite3.connect(str(layout.memory_db))
    before = conn.execute(
        "SELECT target_symbol, confidence FROM edges "
        "WHERE source_file = 'app/main.py' AND edge_type = 'calls' "
        "AND target_symbol IS NULL"
    ).fetchall()
    assert len(before) == 1  # compute_total() recorded unresolved, not dropped

    (python_simple_repo / "app" / "services.py").write_text(
        (python_simple_repo / "app" / "services.py").read_text(encoding="utf-8")
        + "\n\ndef compute_total():\n    return 42\n",
        encoding="utf-8",
    )
    stats = run_update(layout, full=False)
    assert stats["files_parsed"] == 1  # only services.py was re-parsed
    assert stats["files_reused"] == 2  # main.py (the caller) was NOT re-parsed

    conn2 = sqlite3.connect(str(layout.memory_db))
    target_id = conn2.execute(
        "SELECT symbol_id FROM symbols WHERE qualified_name = 'compute_total'"
    ).fetchone()[0]
    resolved = conn2.execute(
        "SELECT target_symbol, target_file, confidence FROM edges "
        "WHERE source_file = 'app/main.py' AND edge_type = 'calls' "
        "AND target_symbol = ?",
        (target_id,),
    ).fetchone()
    assert resolved == (target_id, "app/services.py", 0.6)


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


def test_new_file_with_one_unlocked_import_scope_is_auto_assigned(python_simple_repo: Path) -> None:
    from rune.core.storage.canonical import write_json_model
    from rune.core.storage.models import Scope, ScopeMembers, ScopesFile, ScopeSource

    layout = init_project(python_simple_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    run_update(layout, full=True)
    (python_simple_repo / "app" / "new.py").write_text(
        "from .services import UserService\n\nservice = UserService()\n", encoding="utf-8"
    )

    stats = run_update(layout, full=False)

    assert stats["scope_files_auto_assigned"] == 1
    conn = sqlite3.connect(str(layout.memory_db))
    assert conn.execute(
        "SELECT scope_id FROM scope_files WHERE file = ?", ("app/new.py",)
    ).fetchone() == ("app",)


def test_new_file_with_only_locked_import_scope_is_not_auto_assigned_end_to_end(
    python_simple_repo: Path,
) -> None:
    """End-to-end counterpart to `test_incremental_assignment_requires_one_
    unlocked_import_target` in tests/unit/test_scopes.py, which only
    exercises `assign_new_files_from_imports` directly. That unit-level
    test can't catch a wiring bug in `core.update.run_update` itself (e.g.
    the locked check being bypassed, or `scopes_override` being threaded
    through incorrectly) -- only a full `rune update` run, asserting
    against the actual SQLite `scope_files` table, can.
    """
    from rune.core.storage.canonical import write_json_model
    from rune.core.storage.models import Scope, ScopeMembers, ScopesFile, ScopeSource

    layout = init_project(python_simple_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", locked=True, source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    run_update(layout, full=True)
    (python_simple_repo / "app" / "new.py").write_text(
        "from .services import UserService\n\nservice = UserService()\n", encoding="utf-8"
    )

    stats = run_update(layout, full=False)

    assert stats["scope_files_auto_assigned"] == 0
    conn = sqlite3.connect(str(layout.memory_db))
    assert conn.execute(
        "SELECT scope_id FROM scope_files WHERE file = ?", ("app/new.py",)
    ).fetchone() is None
    # the locked scope's own membership is untouched too
    assert conn.execute(
        "SELECT file FROM scope_files WHERE scope_id = 'app'"
    ).fetchall() == [("app/services.py",)]


def test_failed_rebuild_cache_does_not_leave_partial_scope_auto_assignment(
    python_simple_repo: Path, monkeypatch
) -> None:
    """Regression test: `assign_new_files_from_imports` used to be applied
    and immediately written to canonical `scopes.json` *before*
    `rebuild_cache` ran, so a `rebuild_cache` failure (a canonical
    conflict, a disk error) left the new membership durably written to
    disk even though the cache that was supposed to reflect it was never
    committed -- violating the "update is all-or-nothing" contract
    ARCHITECTURE.md §4.9 makes for the rest of `rune update`. Reproduced
    here by monkeypatching `rebuild_cache` to raise; canonical
    `scopes.json` must come back byte-for-byte unchanged.
    """
    import rune.core.update as update_module
    from rune.core.storage.canonical import read_json_model, write_json_model
    from rune.core.storage.models import Scope, ScopeMembers, ScopesFile, ScopeSource

    layout = init_project(python_simple_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    run_update(layout, full=True)
    scopes_before = layout.scopes_json.read_text(encoding="utf-8")

    (python_simple_repo / "app" / "new.py").write_text(
        "from .services import UserService\n\nservice = UserService()\n", encoding="utf-8"
    )

    def failing_rebuild_cache(*args, **kwargs):
        raise RuntimeError("simulated rebuild_cache failure")

    monkeypatch.setattr(update_module, "rebuild_cache", failing_rebuild_cache)

    try:
        run_update(layout, full=False)
        raise AssertionError("expected the simulated rebuild_cache failure to propagate")
    except RuntimeError:
        pass

    assert layout.scopes_json.read_text(encoding="utf-8") == scopes_before
    scopes_after = read_json_model(layout.scopes_json, ScopesFile)
    assert scopes_after.scopes[0].members.files == ["app/services.py"]  # not app/new.py


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


def test_unchanged_file_keeps_its_previous_parse_error_status(tmp_path: Path) -> None:
    """Regression test: content_hash-unchanged means "not re-parsed this
    run", not "known good". A file that previously failed to parse
    (status=parse_error) must still show parse_error after a no-op
    `rune update` -- an earlier version hardcoded IndexedFileStatus.ok for
    every file in the `unchanged` bucket, so a single content-unchanged
    `rune update` would silently launder a known-bad file's status back
    to ok without the parser ever running again. Reproduced end to end:
    first update correctly marks a syntactically broken file parse_error,
    a second no-op update was flipping it to ok.
    """
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bad.py").write_text("def foo(:\n    pass\n", encoding="utf-8")
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
    assert conn.execute("SELECT status FROM files").fetchone()[0] == "parse_error"

    # no-op incremental update: bad.py's content hasn't changed, so it's
    # never re-parsed -- its status must still reflect the last real
    # parse attempt, not silently reset
    run_update(layout, full=False)
    conn2 = sqlite3.connect(str(layout.memory_db))
    assert conn2.execute("SELECT status FROM files").fetchone()[0] == "parse_error"


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


class _FakeSemanticProvider:
    """Implements the ModelProvider protocol used by core.semantic without
    touching the network -- injected by monkeypatching
    `update_module._build_semantic_providers`, the same pattern this file
    already uses for `resolve_references`/`get_parser_adapter`.
    """

    def __init__(self, model: str, responses: list) -> None:
        self.model = model
        self._responses = list(responses)
        self.call_count = 0

    def complete(self, *, system_prompt, user_prompt, max_tokens):
        from rune.core.semantic.provider import ProviderResponse

        self.call_count += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return ProviderResponse(content=item, input_tokens=10, output_tokens=5, cost=0.001)


def _good_semantic_json(purpose: str = "does the app thing") -> str:
    import json

    return json.dumps({"purpose": purpose})


def test_semantic_refresh_end_to_end_appends_new_revision(
    python_simple_repo: Path, monkeypatch
) -> None:
    """Full wiring test for Milestone 5: a scope with no existing summary
    gets one generated during `rune update`, materialized into SQLite in
    the same run, and appended to semantic.jsonl afterward.
    """
    import rune.core.update as update_module
    from rune.core.storage.canonical import read_jsonl, write_json_model
    from rune.core.storage.models import (
        Scope,
        ScopeMembers,
        ScopesFile,
        ScopeSource,
        ScopeSummary,
    )

    layout = init_project(python_simple_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    provider = _FakeSemanticProvider("fake-model", [_good_semantic_json("summarizes app services")])
    monkeypatch.setattr(
        update_module, "_build_semantic_providers", lambda config: (provider, None)
    )

    stats = run_update(layout, full=False)

    assert stats["semantic_scopes_refreshed"] == 1
    revisions = read_jsonl(layout.semantic_jsonl, ScopeSummary)
    assert len(revisions) == 1
    assert revisions[0].scope_id == "app"
    assert revisions[0].revision == 1
    assert revisions[0].purpose == "summarizes app services"

    conn = sqlite3.connect(str(layout.memory_db))
    row = conn.execute(
        "SELECT current_revision, purpose, status FROM semantic_objects WHERE scope_id = 'app'"
    ).fetchone()
    assert row == (1, "summarizes app services", "fresh")


def test_semantic_refresh_regenerates_after_member_file_content_changes(
    python_simple_repo: Path, monkeypatch
) -> None:
    """Milestone 5's acceptance criterion, literally: modify a scope's
    member file, and the next `rune update` must produce a new `fresh`
    revision reflecting the new source_hash -- `semantic.jsonl` gains
    exactly one more line, not a rewrite of the first.
    """
    import rune.core.update as update_module
    from rune.core.storage.canonical import read_jsonl, write_json_model
    from rune.core.storage.models import (
        Scope,
        ScopeMembers,
        ScopesFile,
        ScopeSource,
        ScopeSummary,
    )

    layout = init_project(python_simple_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    provider = _FakeSemanticProvider("fake-model", [_good_semantic_json("v1 purpose")])
    monkeypatch.setattr(
        update_module, "_build_semantic_providers", lambda config: (provider, None)
    )
    run_update(layout, full=False)
    first = read_jsonl(layout.semantic_jsonl, ScopeSummary)
    assert len(first) == 1
    assert first[0].status.value == "fresh"

    (python_simple_repo / "app" / "services.py").write_text(
        (python_simple_repo / "app" / "services.py").read_text(encoding="utf-8") + "\n# changed\n",
        encoding="utf-8",
    )
    provider2 = _FakeSemanticProvider("fake-model", [_good_semantic_json("v2 purpose")])
    monkeypatch.setattr(
        update_module, "_build_semantic_providers", lambda config: (provider2, None)
    )
    stats = run_update(layout, full=False)

    assert stats["semantic_scopes_refreshed"] == 1
    revisions = read_jsonl(layout.semantic_jsonl, ScopeSummary)
    assert len(revisions) == 2  # appended, not rewritten
    assert revisions[1].revision == 2
    assert revisions[1].purpose == "v2 purpose"
    assert revisions[1].status.value == "fresh"
    assert revisions[1].source_hash != revisions[0].source_hash

    conn = sqlite3.connect(str(layout.memory_db))
    row = conn.execute(
        "SELECT current_revision, purpose FROM semantic_objects WHERE scope_id = 'app'"
    ).fetchone()
    assert row == (2, "v2 purpose")  # SQLite reflects only the current revision


def test_semantic_refresh_failure_does_not_leave_partial_canonical_state(
    python_simple_repo: Path, monkeypatch
) -> None:
    """Regression test for the same all-or-nothing requirement Milestone 4's
    scope auto-assignment fix established: if rebuild_cache fails, the new
    semantic.jsonl revision computed this run must not have been written --
    the whole point of deferring the canonical append until after
    rebuild_cache succeeds.
    """
    import rune.core.update as update_module
    from rune.core.storage.canonical import read_jsonl, write_json_model
    from rune.core.storage.models import (
        Scope,
        ScopeMembers,
        ScopesFile,
        ScopeSource,
        ScopeSummary,
    )

    layout = init_project(python_simple_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    provider = _FakeSemanticProvider("fake-model", [_good_semantic_json()])
    monkeypatch.setattr(
        update_module, "_build_semantic_providers", lambda config: (provider, None)
    )

    def failing_rebuild_cache(*args, **kwargs):
        raise RuntimeError("simulated rebuild_cache failure")

    monkeypatch.setattr(update_module, "rebuild_cache", failing_rebuild_cache)

    try:
        run_update(layout, full=False)
        raise AssertionError("expected the simulated rebuild_cache failure to propagate")
    except RuntimeError:
        pass

    assert read_jsonl(layout.semantic_jsonl, ScopeSummary) == []


def test_semantic_refresh_failure_writes_sanitized_error_to_canonical_and_full_detail_to_local_log(
    python_simple_repo: Path, monkeypatch
) -> None:
    """Round-10 design decision, end to end: `semantic.jsonl`'s `last_error`
    must be a short sanitized classification even when the underlying
    failure carries a detailed provider message, and the full detail must
    still be recoverable from the local, gitignored `.rune/logs/semantic.log`.
    """
    import rune.core.update as update_module
    from rune.core.semantic.provider import ProviderError
    from rune.core.storage.canonical import read_jsonl, write_json_model
    from rune.core.storage.models import (
        Scope,
        ScopeMembers,
        ScopesFile,
        ScopeSource,
        ScopeSummary,
    )

    layout = init_project(python_simple_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    detailed_error = "upstream said: quota exceeded for customer 12345, retry after 60s"
    provider = _FakeSemanticProvider(
        "fake-model", [ProviderError(detailed_error), ProviderError(detailed_error)]
    )
    monkeypatch.setattr(
        update_module, "_build_semantic_providers", lambda config: (provider, None)
    )

    run_update(layout, full=False)

    revisions = read_jsonl(layout.semantic_jsonl, ScopeSummary)
    assert len(revisions) == 1
    assert revisions[0].status.value == "unavailable"
    assert detailed_error not in (revisions[0].last_error or "")
    assert revisions[0].last_error == "provider_error:ProviderError"

    log_text = layout.semantic_log.read_text(encoding="utf-8")
    assert detailed_error in log_text


def test_rebuild_cache_never_calls_the_semantic_provider(
    python_simple_repo: Path, monkeypatch
) -> None:
    """Regression test: `rune rebuild-cache` (run_update(..., full=True))
    is documented everywhere -- its own --help text, this module's
    docstring -- as "zero LLM calls, zero network". Confirmed by hand this
    was being violated: with a provider configured, a stale scope's
    summary was silently regenerated (a real network call) during what's
    supposed to be a purely local, offline rebuild.
    """
    import rune.core.update as update_module
    from rune.core.storage.canonical import read_jsonl, write_json_model
    from rune.core.storage.models import (
        Scope,
        ScopeMembers,
        ScopesFile,
        ScopeSource,
        ScopeSummary,
    )

    layout = init_project(python_simple_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    provider = _FakeSemanticProvider("fake-model", [_good_semantic_json()])
    monkeypatch.setattr(
        update_module, "_build_semantic_providers", lambda config: (provider, None)
    )

    stats = run_update(layout, full=True)

    assert provider.call_count == 0
    assert stats["semantic_scopes_refreshed"] == 0
    assert read_jsonl(layout.semantic_jsonl, ScopeSummary) == []


def test_deleted_scope_orphans_its_semantic_summary_instead_of_crashing(
    python_simple_repo: Path, monkeypatch
) -> None:
    """Regression test: deleting a scope that has prior semantic history
    used to crash every subsequent `rune update`/`rebuild-cache` with a
    raw sqlite3.IntegrityError -- semantic_objects.scope_id has a real FK
    to scopes(id), and materialize tried to insert a row for a scope_id
    that no longer existed. Confirmed by hand before this fix. Full parity
    with Decision/Constraint's existing orphan handling: the scope's
    summary gets one more revision (status=orphaned, content copied
    forward) in canonical semantic.jsonl, excluded from SQLite.
    """
    import rune.core.update as update_module
    from rune.core.storage.canonical import read_jsonl, write_json_model
    from rune.core.storage.models import (
        Scope,
        ScopeMembers,
        ScopesFile,
        ScopeSource,
        ScopeSummary,
    )

    layout = init_project(python_simple_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    provider = _FakeSemanticProvider("fake-model", [_good_semantic_json("original purpose")])
    monkeypatch.setattr(
        update_module, "_build_semantic_providers", lambda config: (provider, None)
    )
    run_update(layout, full=False)

    write_json_model(layout.scopes_json, ScopesFile(scopes=[]))  # delete the scope

    stats = run_update(layout, full=True)  # must not raise IntegrityError

    revisions = read_jsonl(layout.semantic_jsonl, ScopeSummary)
    assert len(revisions) == 2
    assert revisions[1].revision == 2
    assert revisions[1].status.value == "orphaned"
    assert revisions[1].purpose == "original purpose"  # content copied forward

    conn = sqlite3.connect(str(layout.memory_db))
    assert conn.execute(
        "SELECT COUNT(*) FROM semantic_objects WHERE scope_id = 'app'"
    ).fetchone()[0] == 0
    assert stats["files"] == 3  # the rest of the update still completed normally


def test_multiple_semantic_revisions_append_atomically_in_one_run(
    python_simple_repo: Path, monkeypatch
) -> None:
    """Regression test: two scopes refreshed in the same `rune update` used
    to be appended to semantic.jsonl via two separate append_jsonl calls.
    If the second one failed, SQLite (already committed with both new
    revisions via semantic_override) ended up reporting both scopes as
    current while canonical only had the first -- reproduced by hand
    before this fix (SQLite: ['scope_a', 'scope_b'], canonical:
    ['scope_a']). append_jsonl_many's single atomic write means a failure
    can't land in that in-between state: either both lines are written or
    neither is.
    """
    import rune.core.update as update_module
    from rune.core.storage.canonical import read_jsonl, write_json_model
    from rune.core.storage.models import (
        Scope,
        ScopeMembers,
        ScopesFile,
        ScopeSource,
        ScopeSummary,
    )

    layout = init_project(python_simple_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="scope_a", name="A", locked=False, source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
            Scope(id="scope_b", name="B", locked=False, source=ScopeSource.human,
                  members=ScopeMembers(files=["app/main.py"])),
        ]),
    )
    provider = _FakeSemanticProvider(
        "fake-model", [_good_semantic_json("purpose a"), _good_semantic_json("purpose b")]
    )
    monkeypatch.setattr(
        update_module, "_build_semantic_providers", lambda config: (provider, None)
    )

    def failing_append_jsonl_many(path, models):
        raise OSError("simulated disk-full failure")

    monkeypatch.setattr(update_module, "append_jsonl_many", failing_append_jsonl_many)

    try:
        run_update(layout, full=False)
        raise AssertionError("expected the simulated append_jsonl_many failure to propagate")
    except OSError:
        pass

    # Nothing was written to canonical -- not "the first scope only".
    assert read_jsonl(layout.semantic_jsonl, ScopeSummary) == []
    # But the (unpatched, real) SQLite commit already happened inside
    # rebuild_cache before append_jsonl_many was ever called -- this is
    # the pre-existing, accepted asymmetry (same as project.json), not
    # something this test is trying to fix.
    conn = sqlite3.connect(str(layout.memory_db))
    sqlite_scopes = {row[0] for row in conn.execute("SELECT scope_id FROM semantic_objects")}
    assert sqlite_scopes == {"scope_a", "scope_b"}


def test_semantic_run_metrics_persist_across_runs(python_simple_repo: Path, monkeypatch) -> None:
    """Regression test: the six run-level metrics ARCHITECTURE.md §4.5
    calls for used to exist only in the stats dict `rune update` prints
    and returns -- nothing persisted them, so there was no way to compare
    provider/model performance across runs. `semantic_run_metrics` should
    gain one row per run that actually attempted a refresh, and rows
    should accumulate (not get cleared) across multiple runs.
    """
    import rune.core.update as update_module
    from rune.core.storage.canonical import write_json_model
    from rune.core.storage.models import Scope, ScopeMembers, ScopesFile, ScopeSource

    layout = init_project(python_simple_repo)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    provider = _FakeSemanticProvider("fake-model", [_good_semantic_json()])
    monkeypatch.setattr(
        update_module, "_build_semantic_providers", lambda config: (provider, None)
    )
    run_update(layout, full=False)

    conn = sqlite3.connect(str(layout.memory_db))
    rows = conn.execute(
        "SELECT scopes_attempted, schema_success_rate, fallback_rate, "
        "provider_error_rate FROM semantic_run_metrics"
    ).fetchall()
    assert rows == [(1, 1.0, 0.0, 0.0)]

    # A second run with nothing left to refresh (the scope is now fresh
    # and unchanged) must not add an empty/meaningless row.
    run_update(layout, full=False)
    conn2 = sqlite3.connect(str(layout.memory_db))
    assert conn2.execute("SELECT COUNT(*) FROM semantic_run_metrics").fetchone()[0] == 1

    # A `rune rebuild-cache` (full=True) never even attempts a refresh
    # (see test_rebuild_cache_never_calls_the_semantic_provider), so it
    # must not touch this table either -- but the table must survive
    # rebuild_cache's "clear and repopulate" pass, since it isn't one of
    # the six root content tables that gets cleared.
    run_update(layout, full=True)
    conn3 = sqlite3.connect(str(layout.memory_db))
    assert conn3.execute("SELECT COUNT(*) FROM semantic_run_metrics").fetchone()[0] == 1
