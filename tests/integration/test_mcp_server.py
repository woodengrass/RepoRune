"""End-to-end tests for the MCP tool functions (`rune.mcp.server`) --
calling the plain Python functions directly (the `@_tool` decorator wraps
each function for uniform error-mapping and registers it with `mcp.tool()`,
but `functools.wraps` keeps it directly callable with the original
signature, confirmed by hand), not through the MCP wire protocol, since
the protocol layer itself is the `mcp` SDK's responsibility, not this
project's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rune.core.memory.proposals import approve as core_approve
from rune.core.project import init_project
from rune.core.storage.canonical import write_json_model
from rune.core.storage.models import (
    PersistenceMode,
    Scope,
    ScopeMembers,
    ScopesFile,
    ScopeSource,
)
from rune.core.update import run_update
from rune.mcp import server


def test_rune_status_reports_index_size(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    result = server.rune_status(path=str(python_simple_repo))
    assert result["files_indexed"] > 0
    assert result["working_tree_fresh"] is True


def test_rune_doctor_flags_missing_semantic_model(python_simple_repo: Path) -> None:
    init_project(python_simple_repo)
    result = server.rune_doctor(path=str(python_simple_repo))
    assert result["healthy"] is False
    assert any(c["name"] == "semantic_provider" and c["level"] == "error" for c in result["checks"])


def test_rune_symbol_search_finds_class(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    result = server.rune_symbol_search(query="UserService", path=str(python_simple_repo))
    assert any(r["name"] == "UserService" for r in result["results"])


def test_decision_propose_is_pending_not_searchable_until_approved(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    proposed = server.rune_decision_propose(
        record_id="use-x", content="use X for storage", created_by="agent", path=str(python_simple_repo)
    )
    assert proposed["status"] == "pending"

    before = server.rune_search(query="use X", path=str(python_simple_repo))
    assert before["results"] == []

    core_approve(layout, proposed["proposal_id"], resolved_by="alice")
    run_update(layout, full=True)

    after = server.rune_search(query="use X", path=str(python_simple_repo))
    assert [r["id"] for r in after["results"]] == ["use-x"]


def test_note_add_and_get_round_trip(python_simple_repo: Path) -> None:
    init_project(python_simple_repo)
    added = server.rune_note_add(
        category="pitfall", content="watch retries", why_persist="bit us once", path=str(python_simple_repo)
    )
    fetched = server.rune_note_get(note_id=added["id"], path=str(python_simple_repo))
    assert fetched["current"]["content"] == "watch retries"


def test_note_update_appends_revision(python_simple_repo: Path) -> None:
    init_project(python_simple_repo)
    added = server.rune_note_add(
        category="pitfall", content="watch retries", why_persist="bit us once", path=str(python_simple_repo)
    )
    updated = server.rune_note_update(note_id=added["id"], status="archived", path=str(python_simple_repo))
    assert updated["revision"] == 2
    assert updated["status"] == "archived"


def test_scope_read_raises_value_error_for_unknown_scope(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    with pytest.raises(ValueError):
        server.rune_scope_read(scope_id="nope", path=str(python_simple_repo))


def test_related_context_requires_a_selector(python_simple_repo: Path) -> None:
    init_project(python_simple_repo)
    with pytest.raises(ValueError):
        server.rune_related_context(path=str(python_simple_repo))


def test_rune_bootstrap_rejects_unknown_mode(python_simple_repo: Path) -> None:
    init_project(python_simple_repo)
    with pytest.raises(ValueError):
        server.rune_bootstrap(mode="weird", path=str(python_simple_repo))


def test_corrupt_cache_raises_value_error_not_raw_cache_error(python_simple_repo: Path) -> None:
    """A review pass caught this by hand: `CacheUnusableError` used to
    escape every read tool uncaught. The `@_tool` decorator now maps every
    `_KNOWN_ERRORS` member uniformly -- this reproduces a corrupt-cache
    scenario against several read tools and confirms none of them leaks
    the raw `CacheUnusableError`.
    """
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    layout.memory_db.write_bytes(b"not a real sqlite file")

    for call in (
        lambda: server.rune_search(query="x", path=str(python_simple_repo)),
        lambda: server.rune_symbol_search(query="x", path=str(python_simple_repo)),
        lambda: server.rune_check(path=str(python_simple_repo)),
        lambda: server.rune_changes(path=str(python_simple_repo)),
    ):
        with pytest.raises(ValueError):
            call()


def test_scope_for_resolves_by_symbol(python_simple_repo: Path) -> None:
    """`rune_scope_for`'s `symbol=` resolution now lives in `core.
    retrieval.scope_for.resolve_scope_for` rather than being written
    directly in the MCP protocol layer.
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

    result = server.rune_scope_for(symbol="UserService", path=str(python_simple_repo))
    assert result["path"] == "app/services.py"
    assert [s["scope_id"] for s in result["scopes"]] == ["app"]


def test_scope_for_requires_a_selector(python_simple_repo: Path) -> None:
    init_project(python_simple_repo)
    with pytest.raises(ValueError):
        server.rune_scope_for(path=str(python_simple_repo))


def test_constraint_propose_rejects_invalid_severity(python_simple_repo: Path) -> None:
    init_project(python_simple_repo)
    with pytest.raises(ValueError):
        server.rune_constraint_propose(
            record_id="c1", content="x", severity="NOT_A_SEVERITY",
            persistence_mode=PersistenceMode.persistent.value, path=str(python_simple_repo),
        )
