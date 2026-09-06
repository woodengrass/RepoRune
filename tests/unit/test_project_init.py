from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from rune.core.project import (
    AlreadyInitializedError,
    NotAGitRepoError,
    find_repo_root,
    init_project,
)


def test_find_repo_root_walks_up_to_git_dir(git_repo: Path) -> None:
    nested = git_repo / "a" / "b"
    nested.mkdir(parents=True)
    assert find_repo_root(nested) == git_repo


def test_find_repo_root_raises_outside_git(tmp_path: Path) -> None:
    with pytest.raises(NotAGitRepoError):
        find_repo_root(tmp_path)


def test_init_creates_rune_layout(git_repo: Path) -> None:
    layout = init_project(git_repo)
    assert layout.rune_dir.exists()
    assert layout.project_json.exists()
    assert layout.scopes_json.exists()
    assert layout.config_path.exists()
    for path in layout.canonical_memory_files():
        assert path.exists()


def test_init_twice_without_force_raises(git_repo: Path) -> None:
    init_project(git_repo)
    with pytest.raises(AlreadyInitializedError):
        init_project(git_repo)


def _hash_all(paths: list[Path]) -> list[str]:
    return [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]


def test_force_preserves_all_seven_canonical_memory_files_byte_for_byte(
    git_repo: Path,
) -> None:
    """ARCHITECTURE.md §10 / IMPLEMENTATION_PLAN.md Milestone 1: `--force`
    must never clobber project.json, scopes.json, semantic.jsonl,
    decisions.jsonl, constraints.jsonl, notes.jsonl, proposals.jsonl —
    even when they already contain real content.
    """
    layout = init_project(git_repo)

    # Put real content into the protected files, as a human/agent would
    # over time, before anyone runs `rune init --force`.
    layout.scopes_json.write_text(
        '{"schema_version": 1, "scopes": [{"id": "auth", "name": "Auth", '
        '"description": "", "locked": true, "source": "human", '
        '"members": {"files": ["a.py"], "symbols": []}}]}',
        encoding="utf-8",
    )
    layout.decisions_jsonl.write_text(
        '{"record_id":"r1","revision":1,"type":"decision","status":"active",'
        '"content":"x","rationale":"","scopes":[],"files":[],"symbols":[],'
        '"severity":null,"persistence_mode":null,"source_hashes":{},'
        '"scope_hashes":{},"expires_at":null,"created_by":"human",'
        '"approved_by":"a","created_at":"t","schema_version":1}\n',
        encoding="utf-8",
    )

    protected = layout.canonical_memory_files()
    before = _hash_all(protected)

    init_project(git_repo, force=True)

    after = _hash_all(protected)
    assert before == after, "init --force must not modify any canonical memory file"


def test_force_creates_missing_canonical_file(git_repo: Path) -> None:
    layout = init_project(git_repo)
    layout.notes_jsonl.unlink()
    assert not layout.notes_jsonl.exists()

    init_project(git_repo, force=True)

    assert layout.notes_jsonl.exists()
    assert layout.notes_jsonl.read_text(encoding="utf-8") == ""
