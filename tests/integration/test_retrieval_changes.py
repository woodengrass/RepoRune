from __future__ import annotations

import sqlite3
from pathlib import Path

from rune.core.project import init_project
from rune.core.retrieval.changes import StaleSemanticSummary, changes
from rune.core.storage.canonical import write_json_model
from rune.core.storage.models import Scope, ScopeMembers, ScopesFile, ScopeSource
from rune.core.update import run_update


def test_changes_reports_no_changes_on_fresh_index(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    result = changes(layout)
    assert result.changed_files == []
    assert result.affected_scope_ids == []
    assert result.stale_semantic == []


def test_changes_detects_modified_file(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    (python_simple_repo / "app" / "services.py").write_text(
        (python_simple_repo / "app" / "services.py").read_text(encoding="utf-8") + "\n# changed\n",
        encoding="utf-8",
    )
    result = changes(layout)
    assert "app/services.py" in result.changed_files


def test_changes_lists_stale_semantic_summaries_project_wide(python_simple_repo: Path) -> None:
    """`stale_semantic` must report every possibly_stale/stale scope, not
    only ones touched by the current working-tree diff -- confirmed here
    by manually marking a scope stale in the cache directly (no working
    tree change at all) and checking it still shows up.
    """
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", source=ScopeSource.human, members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    run_update(layout, full=True)

    conn = sqlite3.connect(str(layout.memory_db))
    conn.execute(
        "INSERT INTO semantic_objects (scope_id, current_revision, purpose, payload_json, generated_at, "
        "model, source_hash, status) VALUES ('app', 1, 'old purpose', '{}', '2020-01-01T00:00:00Z', 'm', 'h', 'stale')"
    )
    conn.commit()
    conn.close()

    result = changes(layout)
    assert result.stale_semantic == [StaleSemanticSummary(scope_id="app", status="stale")]
