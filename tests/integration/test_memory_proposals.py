from __future__ import annotations

from pathlib import Path

import pytest

from rune.core.memory.proposals import (
    ProposalAlreadyResolvedError,
    ProposalNotFoundError,
    ProposalValidationError,
    approve,
    deactivate,
    list_pending_proposals,
    propose,
    reject,
)
from rune.core.project import init_project
from rune.core.storage.canonical import read_jsonl
from rune.core.storage.models import (
    MemoryRevision,
    PersistenceMode,
    ProposalStatus,
    RecordStatus,
    RecordType,
    RevisionAuthor,
    Scope,
    ScopeMembers,
    ScopesFile,
    ScopeSource,
    Severity,
)
from rune.core.storage.sqlite.materialize import rebuild_cache
from rune.core.update import run_update


def _init_with_index(repo: Path):
    layout = init_project(repo)
    run_update(layout, full=True)
    return layout


def test_propose_and_list_pending(git_repo: Path) -> None:
    layout = init_project(git_repo)
    proposal = propose(
        layout, type=RecordType.decision, record_id="use-postgres",
        content="Use PostgreSQL for the primary datastore.",
        rationale="Team already knows it well.", created_by="agent",
    )
    assert proposal.status is ProposalStatus.pending
    assert proposal.revision == 1

    pending = list_pending_proposals(layout)
    assert len(pending) == 1
    assert pending[0].proposal_id == proposal.proposal_id


def test_propose_decision_with_severity_is_rejected() -> None:
    class _FakeLayout:
        proposals_jsonl = None

    with pytest.raises(ProposalValidationError):
        propose(
            _FakeLayout(), type=RecordType.decision, record_id="d1", content="c",
            severity=Severity.must,
        )


def test_propose_constraint_without_severity_is_rejected() -> None:
    class _FakeLayout:
        proposals_jsonl = None

    with pytest.raises(ProposalValidationError):
        propose(_FakeLayout(), type=RecordType.constraint, record_id="c1", content="c")


def test_propose_temporary_constraint_without_expires_at_is_rejected() -> None:
    class _FakeLayout:
        proposals_jsonl = None

    with pytest.raises(ProposalValidationError):
        propose(
            _FakeLayout(), type=RecordType.constraint, record_id="c1", content="c",
            severity=Severity.must, persistence_mode=PersistenceMode.temporary,
        )


def test_approve_decision_writes_active_revision_and_resolves_proposal(git_repo: Path) -> None:
    layout = init_project(git_repo)
    proposal = propose(
        layout, type=RecordType.decision, record_id="use-postgres",
        content="Use PostgreSQL.", created_by="agent",
    )
    resolved, memory_rev = approve(layout, proposal.proposal_id, resolved_by="alice")

    assert resolved.status is ProposalStatus.approved
    assert resolved.resolved_by == "alice"
    assert memory_rev.status is RecordStatus.active
    assert memory_rev.revision == 1
    assert memory_rev.approved_by == "alice"
    assert memory_rev.created_by is RevisionAuthor.agent  # who *proposed* it, not who approved

    decisions = read_jsonl(layout.decisions_jsonl, MemoryRevision)
    assert len(decisions) == 1
    assert decisions[0].record_id == "use-postgres"

    # can no longer be approved/rejected again
    with pytest.raises(ProposalAlreadyResolvedError):
        approve(layout, proposal.proposal_id, resolved_by="bob")
    with pytest.raises(ProposalAlreadyResolvedError):
        reject(layout, proposal.proposal_id, resolved_by="bob")


def test_approve_second_proposal_for_same_record_id_bumps_revision(git_repo: Path) -> None:
    layout = init_project(git_repo)
    p1 = propose(layout, type=RecordType.decision, record_id="d1", content="v1")
    approve(layout, p1.proposal_id, resolved_by="alice")

    p2 = propose(layout, type=RecordType.decision, record_id="d1", content="v2")
    _, memory_rev = approve(layout, p2.proposal_id, resolved_by="alice")
    assert memory_rev.revision == 2


def test_reject_does_not_write_to_decisions_jsonl(git_repo: Path) -> None:
    layout = init_project(git_repo)
    proposal = propose(layout, type=RecordType.decision, record_id="d1", content="c")
    resolved = reject(layout, proposal.proposal_id, resolved_by="alice")
    assert resolved.status is ProposalStatus.rejected
    assert read_jsonl(layout.decisions_jsonl, MemoryRevision) == []


def test_approve_nonexistent_proposal_raises() -> None:
    class _FakeLayout:
        proposals_jsonl = Path("/nonexistent/proposals.jsonl")

    with pytest.raises(ProposalNotFoundError):
        approve(_FakeLayout(), "nope", resolved_by="alice")


def test_approve_source_bound_constraint_computes_source_hashes_from_index(
    python_simple_repo: Path,
) -> None:
    layout = _init_with_index(python_simple_repo)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="no-bare-except",
        content="never use a bare except", severity=Severity.must,
        persistence_mode=PersistenceMode.source_bound, files=["app/services.py"],
    )
    _, memory_rev = approve(layout, proposal.proposal_id, resolved_by="alice")
    assert memory_rev.source_hashes  # non-empty
    assert "app/services.py" in memory_rev.source_hashes


def test_approve_source_bound_constraint_with_no_indexed_files_is_rejected(
    python_simple_repo: Path,
) -> None:
    layout = _init_with_index(python_simple_repo)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="c",
        severity=Severity.must, persistence_mode=PersistenceMode.source_bound,
        files=["does/not/exist.py"],
    )
    with pytest.raises(ProposalValidationError):
        approve(layout, proposal.proposal_id, resolved_by="alice")


def test_approve_source_bound_constraint_with_one_missing_file_is_rejected(
    python_simple_repo: Path,
) -> None:
    """DATA_MODEL.md §2.5: the source_hashes key set must equal `files ∪
    owning_file(s)` exactly -- "at least one resolves" is not enough.
    Confirmed by hand this used to approve successfully with only the
    resolving file recorded, silently dropping the missing one from what
    the constraint actually tracks.
    """
    layout = _init_with_index(python_simple_repo)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="c",
        severity=Severity.must, persistence_mode=PersistenceMode.source_bound,
        files=["app/services.py", "GONE.py"],
    )
    with pytest.raises(ProposalValidationError):
        approve(layout, proposal.proposal_id, resolved_by="alice")

    # confirm it really was rejected, not partially approved
    from rune.core.storage.canonical import read_jsonl
    from rune.core.storage.models import MemoryRevision

    assert read_jsonl(layout.constraints_jsonl, MemoryRevision) == []


def test_approve_source_bound_constraint_with_unresolved_symbol_is_rejected(
    python_simple_repo: Path,
) -> None:
    layout = _init_with_index(python_simple_repo)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="c",
        severity=Severity.must, persistence_mode=PersistenceMode.source_bound,
        symbols=["does-not-exist::symbol"],
    )
    with pytest.raises(ProposalValidationError):
        approve(layout, proposal.proposal_id, resolved_by="alice")


def test_approve_scope_bound_constraint_computes_scope_hashes(python_simple_repo: Path) -> None:
    layout = _init_with_index(python_simple_repo)
    from rune.core.storage.canonical import write_json_model

    write_json_model(
        layout.scopes_json,
        ScopesFile(scopes=[
            Scope(id="app", name="App", source=ScopeSource.human,
                  members=ScopeMembers(files=["app/services.py"])),
        ]),
    )
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="c",
        severity=Severity.must, persistence_mode=PersistenceMode.scope_bound, scopes=["app"],
    )
    _, memory_rev = approve(layout, proposal.proposal_id, resolved_by="alice")
    assert "app" in memory_rev.scope_hashes


def test_approve_scope_bound_constraint_with_unknown_scope_is_rejected(python_simple_repo: Path) -> None:
    layout = _init_with_index(python_simple_repo)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="c",
        severity=Severity.must, persistence_mode=PersistenceMode.scope_bound, scopes=["nope"],
    )
    with pytest.raises(ProposalValidationError):
        approve(layout, proposal.proposal_id, resolved_by="alice")


def test_edited_payload_approval_marks_edited_and_uses_human_author(git_repo: Path) -> None:
    layout = init_project(git_repo)
    proposal = propose(layout, type=RecordType.decision, record_id="d1", content="draft")
    edited = proposal.payload.model_copy(update={"content": "final wording"})
    resolved, memory_rev = approve(
        layout, proposal.proposal_id, resolved_by="alice", edited_payload=edited
    )
    assert resolved.status is ProposalStatus.edited
    assert memory_rev.content == "final wording"
    assert memory_rev.created_by is RevisionAuthor.human


def test_deactivate_appends_inactive_revision_with_full_snapshot(git_repo: Path) -> None:
    layout = init_project(git_repo)
    proposal = propose(
        layout, type=RecordType.decision, record_id="d1", content="use postgres",
        rationale="because",
    )
    approve(layout, proposal.proposal_id, resolved_by="alice")

    updated = deactivate(layout, RecordType.decision, "d1", by="bob")
    assert updated.status is RecordStatus.inactive
    assert updated.revision == 2
    assert updated.content == "use postgres"  # full snapshot preserved
    assert updated.rationale == "because"
    assert updated.approved_by == "bob"


def test_materialize_reflects_current_revision_after_deactivate(git_repo: Path) -> None:
    """End-to-end: propose -> approve -> deactivate -> rebuild_cache must
    show `current_revision=2, status=inactive`, not fall back to rev1 --
    this is the round-15/16-style regression check applied to Milestone 6.
    """
    import sqlite3

    layout = init_project(git_repo)
    proposal = propose(layout, type=RecordType.decision, record_id="d1", content="c")
    approve(layout, proposal.proposal_id, resolved_by="alice")
    deactivate(layout, RecordType.decision, "d1", by="bob")

    rebuild_cache(layout)
    conn = sqlite3.connect(str(layout.memory_db))
    row = conn.execute(
        "SELECT r.current_revision, v.status FROM decision_records r "
        "JOIN decision_revisions v ON v.record_id = r.record_id AND v.revision = r.current_revision "
        "WHERE r.record_id = 'd1'"
    ).fetchone()
    assert row == (2, "inactive")


def test_approve_writes_memory_revision_before_resolving_proposal(git_repo: Path, monkeypatch) -> None:
    """A relayed review flagged approve()'s two canonical writes as
    non-atomic (proposals.jsonl and decisions.jsonl/constraints.jsonl are
    separate files, so a crash between them is possible). Simulates that
    crash by making the *second* write (the proposal's own resolution)
    fail, and confirms the failure mode is the intentionally-safer one:
    the actual Decision content is already durably written, and the
    proposal is left recoverably `pending` (still visible, not silently
    claiming to be resolved) rather than the content being lost.
    """
    import rune.core.memory.proposals as proposals_module
    from rune.core.storage.canonical import read_jsonl
    from rune.core.storage.models import MemoryRevision, Proposal

    layout = init_project(git_repo)
    proposal = propose(layout, type=RecordType.decision, record_id="d1", content="use postgres")

    real_append_jsonl = proposals_module.append_jsonl
    call_count = {"n": 0}

    def flaky_append_jsonl(path, model):
        call_count["n"] += 1
        if call_count["n"] == 2:  # the proposals.jsonl resolution write
            raise OSError("simulated crash between the two canonical writes")
        return real_append_jsonl(path, model)

    monkeypatch.setattr(proposals_module, "append_jsonl", flaky_append_jsonl)

    with pytest.raises(OSError):
        approve(layout, proposal.proposal_id, resolved_by="alice")

    # The Decision content survived -- not lost.
    decisions = read_jsonl(layout.decisions_jsonl, MemoryRevision)
    assert len(decisions) == 1
    assert decisions[0].content == "use postgres"

    # The proposal is still (accurately) pending, not silently
    # claiming to be resolved while its content never landed anywhere.
    proposals = read_jsonl(layout.proposals_jsonl, Proposal)
    assert len(proposals) == 1
    assert proposals[0].status is ProposalStatus.pending
