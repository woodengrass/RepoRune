from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rune.core.project import init_project
from rune.core.scopes.model import load_scopes, save_scopes
from rune.core.scopes.reconcile import (
    InvalidRefError,
    ReconcileClassification,
    reconcile,
)
from rune.core.storage.models import (
    Edge,
    EdgeType,
    IndexedFile,
    Scope,
    ScopeMembers,
    ScopesFile,
    ScopeSource,
)
from rune.core.storage.sqlite.materialize import CodeIndexData, rebuild_cache


def _file(path: str) -> IndexedFile:
    return IndexedFile(
        path=path, language="python", content_hash="h", size=1, mtime=0.0, indexed_at="2026-01-01T00:00:00Z",
    )


def _setup(git_repo: Path, scopes_file: ScopesFile, files: list[str], edges: list[Edge] | None = None):
    layout = init_project(git_repo)
    save_scopes(layout, scopes_file)
    rebuild_cache(
        layout,
        code_index=CodeIndexData(files=[_file(p) for p in files], edges=edges or []),
    )
    return layout


def test_unassigned_file_with_unique_import_evidence_auto_applies(git_repo: Path) -> None:
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["app/services.py"])),
        ]
    )
    edges = [
        Edge(source_file="app/new.py", target_file="app/services.py",
             edge_type=EdgeType.imports, confidence=1.0),
    ]
    layout = _setup(git_repo, scopes_file, ["app/services.py", "app/new.py"], edges)

    result, updated = reconcile(layout, full=False)

    assert result.auto_count == 1
    assert result.applied is True
    assert updated is not None
    assert "app/new.py" in updated.scopes[0].members.files
    auto_entries = [e for e in result.entries if e.classification is ReconcileClassification.auto]
    assert len(auto_entries) == 1
    assert auto_entries[0].applied is True
    assert auto_entries[0].scope_id == "app"

    # The write is actually persisted by the caller (CLI); confirm the
    # returned ScopesFile round-trips through save/load like any other.
    save_scopes(layout, updated)
    assert "app/new.py" in load_scopes(layout).scopes[0].members.files


def test_unassigned_file_with_no_evidence_is_review_not_auto(git_repo: Path) -> None:
    scopes_file = ScopesFile(scopes=[])
    layout = _setup(git_repo, scopes_file, ["orphan.py"])

    result, updated = reconcile(layout, full=False)

    assert result.auto_count == 0
    assert result.review_count == 1
    assert updated is None
    entry = result.entries[0]
    assert entry.classification is ReconcileClassification.review
    assert entry.candidate_scope_ids == ()


def test_unassigned_file_with_ambiguous_import_evidence_is_review(git_repo: Path) -> None:
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="one", name="One", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["one.py"])),
            Scope(id="two", name="Two", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["two.py"])),
        ]
    )
    edges = [
        Edge(source_file="ambiguous.py", target_file="one.py", edge_type=EdgeType.imports, confidence=1.0),
        Edge(source_file="ambiguous.py", target_file="two.py", edge_type=EdgeType.imports, confidence=1.0),
    ]
    layout = _setup(git_repo, scopes_file, ["one.py", "two.py", "ambiguous.py"], edges)

    result, updated = reconcile(layout, full=False)

    assert result.auto_count == 0
    assert updated is None
    entry = next(e for e in result.entries if e.target == "ambiguous.py")
    assert entry.classification is ReconcileClassification.review
    assert entry.candidate_scope_ids == ("one", "two")


def test_reference_only_evidence_is_review_not_auto(git_repo: Path) -> None:
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="one", name="One", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["one.py"])),
        ]
    )
    edges = [
        Edge(source_file="caller.py", target_file="one.py", edge_type=EdgeType.calls, confidence=0.8),
    ]
    layout = _setup(git_repo, scopes_file, ["one.py", "caller.py"], edges)

    result, updated = reconcile(layout, full=False)

    assert result.auto_count == 0
    assert updated is None
    entry = next(e for e in result.entries if e.target == "caller.py")
    assert entry.classification is ReconcileClassification.review


def test_deleted_target_in_locked_scope_is_broken_not_removed(git_repo: Path) -> None:
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="locked", name="Locked", locked=True, source=ScopeSource.human,
                  members=ScopeMembers(files=["gone.py"])),
        ]
    )
    layout = _setup(git_repo, scopes_file, [])  # gone.py no longer exists in the index

    result, updated = reconcile(layout, full=False)

    assert result.broken_count == 1
    assert updated is None  # never silently removed
    entry = next(e for e in result.entries if e.target == "gone.py")
    assert entry.classification is ReconcileClassification.broken
    assert entry.scope_id == "locked"
    # Canonical membership must still list it -- BROKEN never mutates.
    assert "gone.py" in load_scopes(layout).scopes[0].members.files


def test_deleted_target_in_unlocked_auto_scope_is_review_not_broken(git_repo: Path) -> None:
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="auto", name="Auto", locked=False, source=ScopeSource.auto,
                  members=ScopeMembers(files=["gone.py"])),
        ]
    )
    layout = _setup(git_repo, scopes_file, [])

    result, updated = reconcile(layout, full=False)

    assert result.broken_count == 0
    entry = next(e for e in result.entries if e.target == "gone.py")
    assert entry.classification is ReconcileClassification.review
    assert updated is None


def test_deleted_target_in_unlocked_human_scope_is_still_broken(git_repo: Path) -> None:
    """A scope can be human-authored and later unlocked (`rune scope
    unlock`) without ceasing to be human-authoritative -- `locked` alone
    isn't the only protection signal, `source == human` also counts (see
    reconcile.py's `_is_protected`)."""
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="human-unlocked", name="H", locked=False, source=ScopeSource.human,
                  members=ScopeMembers(files=["gone.py"])),
        ]
    )
    layout = _setup(git_repo, scopes_file, [])

    result, _ = reconcile(layout, full=False)

    assert result.broken_count == 1
    assert result.entries[0].classification is ReconcileClassification.broken


def test_untouched_membership_is_invisible_by_default_and_keep_under_full(git_repo: Path) -> None:
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["app/a.py"])),
        ]
    )
    layout = _setup(git_repo, scopes_file, ["app/a.py"])

    default_result, default_updated = reconcile(layout, full=False)
    assert default_result.entries == []
    assert default_result.keep_count == 1
    assert default_updated is None

    full_result, full_updated = reconcile(layout, full=True)
    assert full_result.keep_count == 1
    assert full_updated is None  # --full never writes
    assert len(full_result.entries) == 1
    assert full_result.entries[0].classification is ReconcileClassification.keep


def test_full_mode_never_applies_even_high_confidence_auto(git_repo: Path) -> None:
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["app/services.py"])),
        ]
    )
    edges = [
        Edge(source_file="app/new.py", target_file="app/services.py",
             edge_type=EdgeType.imports, confidence=1.0),
    ]
    layout = _setup(git_repo, scopes_file, ["app/services.py", "app/new.py"], edges)

    result, updated = reconcile(layout, full=True)

    assert result.auto_count == 1
    assert result.applied is False
    assert updated is None
    entry = next(e for e in result.entries if e.classification is ReconcileClassification.auto)
    assert entry.applied is False


def test_large_churn_blocks_every_auto_write(git_repo: Path) -> None:
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["app/services.py"])),
        ]
    )
    new_files = [f"app/new{i}.py" for i in range(5)]
    edges = [
        Edge(source_file=f, target_file="app/services.py", edge_type=EdgeType.imports, confidence=1.0)
        for f in new_files
    ]
    layout = _setup(git_repo, scopes_file, ["app/services.py", *new_files], edges)

    result, updated = reconcile(layout, full=False, large_churn_threshold=3)

    assert result.auto_count == 5
    assert result.suspicious_churn is True
    assert result.applied is False
    assert updated is None
    # canonical untouched
    assert load_scopes(layout).scopes[0].members.files == ["app/services.py"]


def test_churn_threshold_boundary_is_inclusive(git_repo: Path) -> None:
    """`auto_count > threshold` (not `>=`): exactly at the threshold is
    still allowed to auto-apply, only strictly exceeding it aborts."""
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["app/services.py"])),
        ]
    )
    new_files = [f"app/new{i}.py" for i in range(3)]
    edges = [
        Edge(source_file=f, target_file="app/services.py", edge_type=EdgeType.imports, confidence=1.0)
        for f in new_files
    ]
    layout = _setup(git_repo, scopes_file, ["app/services.py", *new_files], edges)

    result, updated = reconcile(layout, full=False, large_churn_threshold=3)

    assert result.suspicious_churn is False
    assert result.applied is True
    assert updated is not None


def test_symbol_membership_broken_and_keep(git_repo: Path) -> None:
    from rune.core.storage.models import Symbol, SymbolKind

    scopes_file = ScopesFile(
        scopes=[
            Scope(id="locked", name="Locked", locked=True, source=ScopeSource.human,
                  members=ScopeMembers(symbols=["a.py:f:function", "a.py:gone:function"])),
        ]
    )
    layout = init_project(git_repo)
    save_scopes(layout, scopes_file)
    rebuild_cache(
        layout,
        code_index=CodeIndexData(
            files=[_file("a.py")],
            symbols=[
                Symbol(symbol_id="a.py:f:function", file="a.py", name="f", qualified_name="f",
                       kind=SymbolKind.function, start_line=1, end_line=2),
            ],
        ),
    )

    result, updated = reconcile(layout, full=False)

    assert result.keep_count == 1
    assert result.broken_count == 1
    assert updated is None
    broken = next(e for e in result.entries if e.target == "a.py:gone:function")
    assert broken.classification is ReconcileClassification.broken
    assert broken.target_type == "symbol"


def test_reconcile_result_deterministic_across_hash_seeds(git_repo: Path) -> None:
    """Same regression shape as test_references.py's cross-process
    determinism test: candidate-scope selection here uses a Python `set`
    internally (`high_confidence_import_candidates`), which is subject to
    the same PYTHONHASHSEED-dependent iteration order Milestone 3 was
    bitten by. Reproduced here directly via subprocess across several
    seeds rather than assumed safe because the code happens to call
    `sorted()` in the obvious places.
    """
    import json
    import os
    import subprocess
    import sys

    scopes_file = ScopesFile(
        scopes=[
            Scope(id="one", name="One", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["one.py"])),
            Scope(id="two", name="Two", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["two.py"])),
            Scope(id="three", name="Three", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["three.py"])),
        ]
    )
    edges = [
        Edge(source_file="ambiguous.py", target_file="one.py", edge_type=EdgeType.imports, confidence=1.0),
        Edge(source_file="ambiguous.py", target_file="two.py", edge_type=EdgeType.imports, confidence=1.0),
        Edge(source_file="ambiguous.py", target_file="three.py", edge_type=EdgeType.imports, confidence=1.0),
    ]
    _setup(git_repo, scopes_file, ["one.py", "two.py", "three.py", "ambiguous.py"], edges)

    script = (
        "import json\n"
        "from pathlib import Path\n"
        "from rune.core.project import RuneLayout\n"
        "from rune.core.scopes.reconcile import reconcile\n"
        f"layout = RuneLayout(repo_root=Path({str(git_repo)!r}))\n"
        "result, _ = reconcile(layout, full=False)\n"
        "entry = next(e for e in result.entries if e.target == 'ambiguous.py')\n"
        "print(json.dumps(list(entry.candidate_scope_ids)))\n"
    )

    results = set()
    for seed in ("0", "1", "2", "3", "4"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        proc = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        results.add(proc.stdout.strip())

    assert results == {json.dumps(["one", "three", "two"])}, (
        f"reconcile candidate ordering must be identical across hash seeds, got: {results}"
    )


def _git_commit_all(repo: Path, message: str) -> str:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", message],
        cwd=repo, check=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def test_since_flags_merge_affected_membership_with_changed_evidence(git_repo: Path) -> None:
    """ARCHITECTURE.md §16.6: a membership introduced between `since` and
    HEAD (simulating "branch-local incremental auto-assign, then merged")
    must be re-checked against the current import graph -- if it no
    longer uniquely resolves to the same scope, it becomes REVIEW, never
    silently kept or reassigned.
    """
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["app/services.py"])),
            Scope(id="other", name="Other", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["other/thing.py"])),
        ]
    )
    layout = _setup(
        git_repo, scopes_file, ["app/services.py", "other/thing.py"],
        edges=[
            Edge(source_file="app/services.py", target_file="app/services.py",
                 edge_type=EdgeType.imports, confidence=1.0),
        ],
    )
    since_ref = _git_commit_all(git_repo, "pre-merge base")

    # Simulate what a branch-local `rune update` incremental auto-assign
    # would have done before the merge: app/new.py got auto-written into
    # "app" because at the time it uniquely imported app/services.py.
    scopes_file.scopes[0].members.files.append("app/new.py")
    save_scopes(layout, scopes_file)
    # Simulate the merge changing the import graph: app/new.py now
    # imports other/thing.py instead (e.g. the other branch refactored
    # it) -- no longer unique evidence for "app".
    rebuild_cache(
        layout,
        code_index=CodeIndexData(
            files=[_file(p) for p in ("app/services.py", "other/thing.py", "app/new.py")],
            edges=[
                Edge(source_file="app/new.py", target_file="other/thing.py",
                     edge_type=EdgeType.imports, confidence=1.0),
            ],
        ),
    )

    result, updated = reconcile(layout, full=False, since=since_ref)
    entry = next(e for e in result.entries if e.target == "app/new.py")
    assert entry.classification is ReconcileClassification.review
    assert entry.scope_id == "app"
    assert entry.candidate_scope_ids == ("other",)
    assert "merge-affected" in entry.reason
    # Never silently reassigned: still a member of "app" in canonical, and
    # this function never writes on its own (reconcile()'s AUTO-only write
    # path is untouched by since= entries).
    assert updated is None or "app/new.py" not in next(
        s for s in updated.scopes if s.id == "other"
    ).members.files


def test_since_no_entry_when_merge_affected_evidence_still_matches(git_repo: Path) -> None:
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["app/services.py"])),
        ]
    )
    layout = _setup(
        git_repo, scopes_file, ["app/services.py"],
        edges=[Edge(source_file="app/services.py", target_file="app/services.py",
                     edge_type=EdgeType.imports, confidence=1.0)],
    )
    since_ref = _git_commit_all(git_repo, "pre-merge base")

    scopes_file.scopes[0].members.files.append("app/new.py")
    save_scopes(layout, scopes_file)
    rebuild_cache(
        layout,
        code_index=CodeIndexData(
            files=[_file(p) for p in ("app/services.py", "app/new.py")],
            edges=[Edge(source_file="app/new.py", target_file="app/services.py",
                         edge_type=EdgeType.imports, confidence=1.0)],
        ),
    )

    result, _ = reconcile(layout, full=False, since=since_ref)
    assert not any(e.target == "app/new.py" for e in result.entries)


def test_since_skips_protected_scope(git_repo: Path) -> None:
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="app", name="App", locked=True, source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
            Scope(id="other", name="Other", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["other/thing.py"])),
        ]
    )
    layout = _setup(git_repo, scopes_file, ["app/services.py", "other/thing.py"])
    since_ref = _git_commit_all(git_repo, "pre-merge base")

    scopes_file.scopes[0].members.files.append("app/new.py")
    save_scopes(layout, scopes_file)
    rebuild_cache(
        layout,
        code_index=CodeIndexData(
            files=[_file(p) for p in ("app/services.py", "other/thing.py", "app/new.py")],
            edges=[Edge(source_file="app/new.py", target_file="other/thing.py",
                         edge_type=EdgeType.imports, confidence=1.0)],
        ),
    )

    result, _ = reconcile(layout, full=False, since=since_ref)
    assert not any(e.target == "app/new.py" for e in result.entries)


def test_since_none_skips_revalidation_entirely(git_repo: Path) -> None:
    """Baseline: omitting `since` (the default) must not run merge-affected
    revalidation at all, even where it would otherwise flag something --
    this is strictly opt-in.
    """
    scopes_file = ScopesFile(
        scopes=[
            Scope(id="app", name="App", locked=False, source=ScopeSource.model,
                  members=ScopeMembers(files=["app/services.py", "app/new.py"])),
        ]
    )
    layout = _setup(
        git_repo, scopes_file, ["app/services.py", "app/new.py"],
        edges=[Edge(source_file="app/new.py", target_file="nonexistent.py",
                     edge_type=EdgeType.imports, confidence=1.0)],
    )

    result, _ = reconcile(layout, full=False)
    assert not any(e.target == "app/new.py" for e in result.entries)


def test_since_invalid_ref_raises(git_repo: Path) -> None:
    scopes_file = ScopesFile(
        scopes=[Scope(id="app", name="App", locked=False, source=ScopeSource.model,
                       members=ScopeMembers(files=["app/services.py"]))]
    )
    layout = _setup(git_repo, scopes_file, ["app/services.py"])
    with pytest.raises(InvalidRefError):
        reconcile(layout, full=False, since="not-a-real-ref-at-all")
