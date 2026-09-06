from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rune.cli.main import app

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
