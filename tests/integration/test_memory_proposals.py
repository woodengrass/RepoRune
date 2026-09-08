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
from rune.core.project import RuneLayout, init_project
from rune.core.storage.canonical import read_jsonl
from rune.core.storage.models import (
    Actor,
    MemoryRevision,
    PersistenceMode,
    Proposal,
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
        rationale="Team already knows it well.", created_by=Actor.agent,
    )
    assert proposal.status is ProposalStatus.pending
    assert proposal.revision == 1

    pending = list_pending_proposals(layout)
    assert len(pending) == 1
    assert pending[0].proposal_id == proposal.proposal_id


def test_proposal_text_is_redacted_before_canonical_write(git_repo: Path) -> None:
    layout = init_project(git_repo)
    proposal = propose(
        layout, type=RecordType.decision, record_id="secret-handling",
        content="Rotate sk-abcdefghijklmnopqrstuvwx1234 immediately.",
    )

    assert "sk-abcdefghijklmnopqrstuvwx1234" not in proposal.payload.content
    assert "[REDACTED]" in proposal.payload.content


def test_propose_decision_with_severity_is_rejected() -> None:
    with pytest.raises(ProposalValidationError):
        propose(
            RuneLayout(repo_root=Path("/nonexistent")),
            type=RecordType.decision, record_id="d1", content="c",
            severity=Severity.must,
        )


def test_propose_constraint_without_severity_is_rejected() -> None:
    with pytest.raises(ProposalValidationError):
        propose(
            RuneLayout(repo_root=Path("/nonexistent")),
            type=RecordType.constraint, record_id="c1", content="c",
        )


def test_propose_temporary_constraint_without_expires_at_is_rejected() -> None:
    with pytest.raises(ProposalValidationError):
        propose(
            RuneLayout(repo_root=Path("/nonexistent")),
            type=RecordType.constraint, record_id="c1", content="c",
            severity=Severity.must, persistence_mode=PersistenceMode.temporary,
        )


def test_approve_decision_writes_active_revision_and_resolves_proposal(git_repo: Path) -> None:
    layout = init_project(git_repo)
    proposal = propose(
        layout, type=RecordType.decision, record_id="use-postgres",
        content="Use PostgreSQL.", created_by=Actor.agent,
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
    layout = RuneLayout(repo_root=Path("/nonexistent"))

    with pytest.raises(ProposalNotFoundError):
        approve(layout, "nope", resolved_by="alice")


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


def test_deactivate_is_idempotent_for_an_inactive_current_revision(git_repo: Path) -> None:
    layout = init_project(git_repo)
    proposal = propose(layout, type=RecordType.decision, record_id="d1", content="use postgres")
    approve(layout, proposal.proposal_id, resolved_by="alice")

    first = deactivate(layout, RecordType.decision, "d1", by="bob")
    second = deactivate(layout, RecordType.decision, "d1", by="carol")

    assert second == first
    assert len(read_jsonl(layout.decisions_jsonl, MemoryRevision)) == 2


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


def test_edited_payload_with_different_record_id_is_rejected(git_repo: Path) -> None:
    """A relayed review confirmed by hand: approve() never checked that
    edited_payload.record_id matched the proposal being resolved, so an
    edit could silently redirect the approval onto a completely different
    (possibly unrelated or nonexistent) record_id while the original
    proposal got marked resolved -- an undetected hijack, not an error.
    """
    layout = init_project(git_repo)
    proposal = propose(layout, type=RecordType.decision, record_id="d1", content="original")
    edited = proposal.payload.model_copy(update={"record_id": "d2-different", "content": "hijacked"})

    with pytest.raises(ProposalValidationError):
        approve(layout, proposal.proposal_id, resolved_by="alice", edited_payload=edited)

    from rune.core.storage.canonical import read_jsonl
    from rune.core.storage.models import MemoryRevision

    assert read_jsonl(layout.decisions_jsonl, MemoryRevision) == []


def test_edited_payload_with_different_type_is_rejected(git_repo: Path) -> None:
    layout = init_project(git_repo)
    proposal = propose(layout, type=RecordType.decision, record_id="d1", content="original")
    edited = proposal.payload.model_copy(
        update={
            "type": RecordType.constraint, "severity": Severity.must,
            "persistence_mode": PersistenceMode.persistent,
        }
    )
    with pytest.raises(ProposalValidationError):
        approve(layout, proposal.proposal_id, resolved_by="alice", edited_payload=edited)


def test_current_by_rejects_duplicate_revision(git_repo: Path) -> None:
    """A relayed review confirmed by hand: `core.memory.records.current_by`
    (used by propose/approve/note_add/staleness, all of which read
    canonical directly rather than through `rebuild_cache`) silently
    picked one of two revisions sharing the same revision number, instead
    of raising the same CanonicalConflictError `materialize.py`'s own
    duplicate check already raises for the exact same situation. Two read
    paths must agree that a canonical conflict is fatal, not just one.
    """
    from rune.core.storage.canonical import append_jsonl
    from rune.core.storage.models import MemoryRevision, RevisionAuthor
    from rune.core.storage.sqlite.materialize import CanonicalConflictError

    layout = init_project(git_repo)
    now = "2026-01-01T00:00:00Z"
    append_jsonl(
        layout.decisions_jsonl,
        MemoryRevision(
            record_id="d1", revision=2, type=RecordType.decision, status=RecordStatus.active,
            content="version A", created_by=RevisionAuthor.human, created_at=now,
        ),
    )
    append_jsonl(
        layout.decisions_jsonl,
        MemoryRevision(
            record_id="d1", revision=2, type=RecordType.decision, status=RecordStatus.inactive,
            content="version B", created_by=RevisionAuthor.human, created_at=now,
        ),
    )
    from rune.core.memory.records import load_current_decisions

    with pytest.raises(CanonicalConflictError):
        load_current_decisions(layout)


def test_deactivate_cache_refresh_failure_gives_written_content_not_raw_traceback(git_repo: Path) -> None:
    """A relayed review confirmed by hand: when refresh_cache() (called
    after every propose-approve/note write) fails because some *other*
    canonical file already has a conflict, the CLI printed a raw
    traceback -- even though the write this command was actually asked to
    do had already succeeded. This tests the core-level building block:
    the canonical write survives a refresh_cache failure.
    """
    from rune.core.storage.canonical import read_jsonl
    from rune.core.storage.sqlite.materialize import CanonicalConflictError
    from rune.core.update import run_update

    layout = init_project(git_repo)
    run_update(layout, full=True)  # memory.db must actually exist for refresh_cache to do anything
    proposal = propose(layout, type=RecordType.decision, record_id="d1", content="c")
    approve(layout, proposal.proposal_id, resolved_by="alice")

    # corrupt decisions.jsonl with a duplicate revision so the next
    # refresh_cache() call (inside deactivate()) raises
    from rune.core.storage.canonical import append_jsonl as raw_append

    raw_append(
        layout.decisions_jsonl,
        MemoryRevision(
            record_id="other", revision=1, type=RecordType.decision, status=RecordStatus.active,
            content="A", created_by=RevisionAuthor.human, created_at="2026-01-01T00:00:00Z",
        ),
    )
    raw_append(
        layout.decisions_jsonl,
        MemoryRevision(
            record_id="other", revision=1, type=RecordType.decision, status=RecordStatus.active,
            content="B", created_by=RevisionAuthor.human, created_at="2026-01-01T00:00:00Z",
        ),
    )

    with pytest.raises(CanonicalConflictError):
        deactivate(layout, RecordType.decision, "d1", by="bob")

    # the deactivation itself was written despite the cache refresh failure
    decisions = [r for r in read_jsonl(layout.decisions_jsonl, MemoryRevision) if r.record_id == "d1"]
    assert len(decisions) == 2
    assert decisions[-1].status is RecordStatus.inactive


def test_reapproving_a_still_pending_proposal_after_a_crash_does_not_duplicate(git_repo: Path) -> None:
    """A relayed review confirmed by hand: approve() writes the memory
    revision before resolving the proposal (so a crash between the two
    leaves the content safely written and the proposal still `pending`,
    rather than the reverse silent-loss risk). But re-running approve()
    on that still-pending proposal used to just append a second, fully
    duplicate revision. Simulates the crash, then re-approves, and checks
    only one revision exists.
    """
    import rune.core.memory.proposals as proposals_module

    layout = init_project(git_repo)
    proposal = propose(layout, type=RecordType.decision, record_id="d1", content="c")

    real_append = proposals_module.append_jsonl
    call_count = {"n": 0}

    def flaky_append(path: Path, model: Proposal) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:  # the proposals.jsonl resolution write
            raise OSError("simulated crash between the two canonical writes")
        return real_append(path, model)

    proposals_module.append_jsonl = flaky_append  # type: ignore[assignment]
    try:
        with pytest.raises(OSError):
            approve(layout, proposal.proposal_id, resolved_by="alice")
    finally:
        proposals_module.append_jsonl = real_append

    # recovery: re-approve the still-pending proposal
    resolved, _memory_rev = approve(layout, proposal.proposal_id, resolved_by="alice")
    assert resolved.status is ProposalStatus.approved

    from rune.core.storage.canonical import read_jsonl

    decisions = read_jsonl(layout.decisions_jsonl, MemoryRevision)
    assert len(decisions) == 1
    assert decisions[0].revision == 1


def test_reapproving_after_a_crash_with_a_different_by_does_not_duplicate(git_repo: Path) -> None:
    """Relayed review, reproduced by hand: the retry-after-crash
    idempotency check (see the test above) compared `approved_by` as
    part of "is this the same content already written" -- so a retry
    completed by a *different* human than whoever hit the crash (a
    realistic scenario: someone else notices the stuck pending proposal
    and finishes it) failed the comparison and appended a second,
    duplicate revision. Fixed by dropping `approved_by` from the
    comparison (it records who ran the call, not what was approved).
    """
    import rune.core.memory.proposals as proposals_module

    layout = init_project(git_repo)
    proposal = propose(layout, type=RecordType.decision, record_id="d1", content="c")

    real_append = proposals_module.append_jsonl
    call_count = {"n": 0}

    def flaky_append(path: Path, model: Proposal) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated crash between the two canonical writes")
        return real_append(path, model)

    proposals_module.append_jsonl = flaky_append  # type: ignore[assignment]
    try:
        with pytest.raises(OSError):
            approve(layout, proposal.proposal_id, resolved_by="alice")
    finally:
        proposals_module.append_jsonl = real_append

    resolved, memory_rev = approve(layout, proposal.proposal_id, resolved_by="bob")
    assert resolved.status is ProposalStatus.approved
    assert resolved.resolved_by == "bob"
    # the already-written revision's own attribution is untouched
    assert memory_rev.approved_by == "alice"
    assert memory_rev.revision == 1

    decisions = read_jsonl(layout.decisions_jsonl, MemoryRevision)
    assert len(decisions) == 1


def test_propose_rejects_invalid_expires_at_with_a_clean_error(git_repo: Path) -> None:
    """`propose()` builds `MemoryRevision(...)` directly -- this already
    failed before writing anything (nothing to poison here), but as a
    raw `pydantic.ValidationError` rather than the domain
    `ProposalValidationError` every other rejection in this module
    raises. Fixed alongside the higher-severity `approve()`/`edited_
    payload` bug below since it's the same class of gap.
    """
    layout = init_project(git_repo)
    with pytest.raises(ProposalValidationError):
        propose(
            layout, type=RecordType.constraint, record_id="c1", content="c",
            severity=Severity.should, persistence_mode=PersistenceMode.temporary,
            expires_at="2026-09-07T12:00:00",  # naive, no tzinfo
        )
    assert read_jsonl(layout.proposals_jsonl, Proposal) == []


def test_approve_rejects_edited_payload_with_naive_expires_at_without_poisoning_canonical(
    git_repo: Path,
) -> None:
    """Relayed review, reproduced by hand: the CLI's `proposal edit`
    builds `edited_payload` via `proposal.payload.model_copy(update=...)`
    -- unvalidated, same class of bug as `note_update`'s. A naive
    `--expires-at` used to be written straight into `constraints.jsonl`,
    breaking every later `constraint`/`decision`/`search` command that
    reads it back. Fixed with a defensive re-validation inside
    `approve()` itself (not just at the CLI layer) so the write path is
    protected regardless of how a caller constructed `edited_payload`.
    """
    layout = init_project(git_repo)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="temp rule",
        severity=Severity.should, persistence_mode=PersistenceMode.temporary,
        expires_at="2026-12-01T00:00:00Z",
    )
    # mimics the CLI's own (unsafe) construction of `edited_payload`
    edited = proposal.payload.model_copy(update={"expires_at": "2026-09-07T12:00:00"})

    with pytest.raises(ProposalValidationError):
        approve(layout, proposal.proposal_id, resolved_by="alice", edited_payload=edited)

    assert read_jsonl(layout.constraints_jsonl, MemoryRevision) == []
    still_pending = [
        p for p in read_jsonl(layout.proposals_jsonl, Proposal) if p.proposal_id == proposal.proposal_id
    ]
    assert still_pending[-1].status is ProposalStatus.pending


def test_edited_payload_critical_true_on_constraint_is_rejected(git_repo: Path) -> None:
    """A relayed review confirmed by hand: `critical` is a decision-only
    convention (constraint_revisions has no `critical` column at all), but
    approve()/propose() never checked this -- critical=True on a
    constraint payload used to be accepted, written to constraints.jsonl,
    and then had nowhere to go once materialized (a canonical/derived-
    cache mismatch with no error anywhere).
    """
    layout = init_project(git_repo)
    proposal = propose(
        layout, type=RecordType.constraint, record_id="c1", content="c",
        severity=Severity.must, persistence_mode=PersistenceMode.persistent,
    )
    edited = proposal.payload.model_copy(update={"critical": True})
    with pytest.raises(ProposalValidationError):
        approve(layout, proposal.proposal_id, resolved_by="alice", edited_payload=edited)


def test_machine_check_hint_on_decision_proposal_is_rejected() -> None:
    with pytest.raises(ProposalValidationError):
        propose(
            RuneLayout(repo_root=Path("/nonexistent")),
            type=RecordType.decision, record_id="d1", content="c",
            machine_check_hint="ruff",
        )
