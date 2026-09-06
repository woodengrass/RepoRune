from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rune.cli.main import app
from rune.core.project import RuneLayout
from rune.core.scopes.model import load_scopes

runner = CliRunner()


def test_init_then_status_json_reports_zero_modified(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--path", str(git_repo)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["status", "--path", str(git_repo), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["files_indexed"] == 1
    assert payload["working_tree_fresh"] is True
    assert payload["files_modified"] == 0
    assert payload["files_added"] == 0
    assert payload["files_deleted"] == 0


def test_status_reports_modified_added_and_deleted_counts(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    (git_repo / "b.py").write_text("def bar():\n    pass\n", encoding="utf-8")
    runner.invoke(app, ["init", "--path", str(git_repo)])

    # modify a.py, delete b.py, add c.py
    (git_repo / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    (git_repo / "b.py").unlink()
    (git_repo / "c.py").write_text("def baz():\n    pass\n", encoding="utf-8")

    result = runner.invoke(app, ["status", "--path", str(git_repo), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["working_tree_fresh"] is False
    assert payload["files_modified"] == 1
    assert payload["files_added"] == 1
    assert payload["files_deleted"] == 1


def test_update_then_status_reports_fresh_again(git_repo: Path) -> None:
    (git_repo / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    runner.invoke(app, ["init", "--path", str(git_repo)])
    (git_repo / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")

    result = runner.invoke(app, ["update", "--path", str(git_repo)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["status", "--path", str(git_repo), "--json"])
    payload = json.loads(result.output)
    assert payload["working_tree_fresh"] is True
    assert payload["files_modified"] == 0


def test_scope_suggest_rejection_has_no_canonical_side_effect(git_repo: Path) -> None:
    (git_repo / "app").mkdir()
    (git_repo / "app" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (git_repo / "app" / "b.py").write_text("y = 2\n", encoding="utf-8")
    assert runner.invoke(app, ["init", "--path", str(git_repo)]).exit_code == 0

    result = runner.invoke(app, ["scope", "suggest", "--path", str(git_repo)], input="n\n")

    assert result.exit_code == 0, result.output


def test_scope_suggest_before_cache_exists_fails_cleanly(git_repo: Path) -> None:
    """Regression test: a repo can legitimately have `.rune/scopes.json`
    committed while `.rune/cache/` (gitignored) doesn't exist yet on a
    fresh clone -- e.g. before the first `rune update`. `scope suggest`
    used to call `sqlite3.connect` unconditionally, which either raised an
    unhandled `OperationalError: unable to open database file` (cache
    directory missing) or silently created a stray empty `memory.db` and
    then crashed with `OperationalError: no such table: symbols` (cache
    directory present but the file itself missing) -- both reproduced by
    hand before this test was written. `status` already guarded this with
    `layout.memory_db.exists()`; `scope suggest` now does the same.
    """
    from rune.core.project import init_project
    from rune.core.update import run_update

    layout = init_project(git_repo)
    run_update(layout, full=True)
    layout.memory_db.unlink()

    result = runner.invoke(app, ["scope", "suggest", "--path", str(git_repo)])

    assert result.exit_code == 1
    assert not layout.memory_db.exists()  # no stray file left behind
    assert RuneLayout(git_repo).rune_dir.exists()  # sanity: didn't touch anything else
    assert load_scopes(RuneLayout(git_repo)).scopes == []
