"""Decision/Constraint propose -> human approve/reject/edit flow
(DATA_MODEL.md §2.5, §2.5a; ARCHITECTURE.md §4.6). Note has no equivalent
gate -- see `core.memory.notes` for its direct-write flow.

Proposals are revision-based (`proposal_id` + `revision`, current =
max(revision)) for the same reason every other canonical record here is:
`proposals.jsonl` is append-only, so "pending -> approved" cannot be an
in-place edit.
"""

from __future__ import annotations

import uuid
from typing import Literal

from rune.core.memory.hashes import compute_scope_membership_hash, compute_source_hashes
from rune.core.memory.records import current_by, current_by_record_id
from rune.core.memory.records import refresh_cache as _refresh_cache
from rune.core.project import RuneLayout, utc_now_iso
from rune.core.scopes.model import load_scopes
from rune.core.storage.canonical import append_jsonl, read_jsonl
from rune.core.storage.models import (
    MemoryRevision,
    PersistenceMode,
    Proposal,
    ProposalStatus,
    RecordStatus,
    RecordType,
    RevisionAuthor,
    Severity,
)
from rune.core.storage.sqlite.materialize import read_current_code_index


class ProposalNotFoundError(Exception):
    pass


class ProposalAlreadyResolvedError(Exception):
    pass


class ProposalValidationError(Exception):
    """Raised for a proposal/edit whose shape can never be approved as-is
    -- a constraint missing severity/persistence_mode, a non-temporary
    constraint carrying `expires_at`, etc. (DATA_MODEL.md §2.5's write-time
    validation rules). Distinct from `ApprovalValidationError` only in
    name for readability at call sites; both are raised from the same
    `_validate_payload_shape`.
    """


ApprovalValidationError = ProposalValidationError


def _validate_payload_shape(
    record_type: RecordType,
    severity: Severity | None,
    persistence_mode: PersistenceMode | None,
    expires_at: str | None,
) -> None:
    if record_type is RecordType.decision:
        if severity is not None or persistence_mode is not None:
            raise ProposalValidationError(
                "a decision must not set severity/persistence_mode -- those are constraint-only fields"
            )
        if expires_at is not None:
            raise ProposalValidationError("a decision must not set expires_at")
        return
    if severity is None or persistence_mode is None:
        raise ProposalValidationError(
            "a constraint requires both severity and persistence_mode"
        )
    if persistence_mode is PersistenceMode.temporary:
        if expires_at is None:
            raise ProposalValidationError("persistence_mode=temporary requires expires_at")
    elif expires_at is not None:
        raise ProposalValidationError(
            "expires_at is only valid for persistence_mode=temporary "
            f"(got persistence_mode={persistence_mode.value})"
        )


def propose(
    layout: RuneLayout,
    *,
    type: RecordType,
    record_id: str,
    content: str,
    rationale: str = "",
    scopes: list[str] = (),
    files: list[str] = (),
    symbols: list[str] = (),
    severity: Severity | None = None,
    persistence_mode: PersistenceMode | None = None,
    expires_at: str | None = None,
    critical: bool = False,
    source_document: str | None = None,
    source_section: str | None = None,
    machine_check_hint: str | None = None,
    created_by: Literal["agent", "human"] = "agent",
) -> Proposal:
    """Writes a `revision=1`, `status=pending` Proposal. Validated against
    the same shape rules `approve()` will re-check -- failing fast here
    means a proposal that can never be approved is rejected immediately
    instead of silently sitting in the pending queue until someone tries
    (and fails) to approve it.
    """
    _validate_payload_shape(type, severity, persistence_mode, expires_at)
    now = utc_now_iso()
    payload = MemoryRevision(
        record_id=record_id,
        revision=1,  # placeholder -- the real decisions/constraints.jsonl
        # revision number is only known at approval time (it depends on
        # how many revisions record_id already has), so this field is
        # meaningless until then and gets overwritten by approve().
        type=type,
        status=RecordStatus.active,  # placeholder, same reason
        content=content,
        rationale=rationale,
        scopes=sorted(set(scopes)),
        files=sorted(set(files)),
        symbols=sorted(set(symbols)),
        severity=severity,
        persistence_mode=persistence_mode,
        expires_at=expires_at,
        critical=critical,
        source_document=source_document,
        source_section=source_section,
        machine_check_hint=machine_check_hint,
        created_by=RevisionAuthor.agent if created_by == "agent" else RevisionAuthor.human,
        created_at=now,
    )
    proposal = Proposal(
        proposal_id=str(uuid.uuid4()),
        revision=1,
        type=type,
        record_id=record_id,
        payload=payload,
        status=ProposalStatus.pending,
        created_by=created_by,
        created_at=now,
    )
    append_jsonl(layout.proposals_jsonl, proposal)
    return proposal


def list_pending_proposals(layout: RuneLayout) -> list[Proposal]:
    """Current revisions whose status is still `pending`, oldest first --
    this is the CLI's "what's waiting for me" queue. A resolved proposal's
    later revisions never show up here, only genuinely-untouched ones."""
    proposals = read_jsonl(layout.proposals_jsonl, Proposal)
    current = current_by(proposals, "proposal_id")
    return sorted(
        (p for p in current.values() if p.status is ProposalStatus.pending),
        key=lambda p: p.created_at,
    )


def get_current_proposal(layout: RuneLayout, proposal_id: str) -> Proposal:
    proposals = read_jsonl(layout.proposals_jsonl, Proposal)
    current = current_by(proposals, "proposal_id")
    proposal = current.get(proposal_id)
    if proposal is None:
        raise ProposalNotFoundError(f"proposal {proposal_id!r} does not exist")
    return proposal


def _next_record_revision(layout: RuneLayout, record_type: RecordType, record_id: str) -> int:
    path = layout.decisions_jsonl if record_type is RecordType.decision else layout.constraints_jsonl
    existing = [r for r in read_jsonl(path, MemoryRevision) if r.record_id == record_id]
    current = current_by_record_id(existing)
    return current[record_id].revision + 1 if record_id in current else 1


def approve(
    layout: RuneLayout,
    proposal_id: str,
    *,
    resolved_by: str,
    edited_payload: MemoryRevision | None = None,
) -> tuple[Proposal, MemoryRevision]:
    """Approves (or, with `edited_payload`, edits-then-approves) a pending
    proposal: appends the proposal's own `resolved`/`edited` revision AND,
    in the same call, the corresponding `decisions.jsonl`/
    `constraints.jsonl` revision (DATA_MODEL.md §2.5a) -- callers that
    need these atomic together in one canonical write should treat this
    function itself as the unit of work, same as `core.update` treats
    `rebuild_cache` as a unit.

    `source_hashes`/`scope_hashes` are computed here, automatically, from
    the current code index/scopes.json -- the human approving only
    confirms the *content*, never types a hash by hand (DATA_MODEL.md
    §2.5's write-time validation rules).
    """
    proposal = get_current_proposal(layout, proposal_id)
    if proposal.status is not ProposalStatus.pending:
        raise ProposalAlreadyResolvedError(
            f"proposal {proposal_id!r} is already {proposal.status.value}, not pending"
        )

    payload = edited_payload if edited_payload is not None else proposal.payload
    if edited_payload is not None:
        # An [E]dit is allowed to change content/rationale/scopes/files/
        # symbols/severity/persistence_mode/expires_at/etc, but never
        # which record it's approving into or what kind of record it is
        # -- record_id/type identify *which proposal this is resolving*,
        # not editable content. Without this check, an edited_payload
        # with a different record_id silently writes the approval under
        # an unrelated (possibly nonexistent) record while the original
        # proposal gets marked resolved, which is effectively a hijacked
        # approval with no error at all (confirmed by hand).
        if edited_payload.record_id != proposal.record_id:
            raise ProposalValidationError(
                f"edited_payload.record_id ({edited_payload.record_id!r}) must match "
                f"the proposal's own record_id ({proposal.record_id!r})"
            )
        if edited_payload.type != proposal.type:
            raise ProposalValidationError(
                f"edited_payload.type ({edited_payload.type.value!r}) must match "
                f"the proposal's own type ({proposal.type.value!r})"
            )
    _validate_payload_shape(payload.type, payload.severity, payload.persistence_mode, payload.expires_at)

    now = utc_now_iso()
    source_hashes: dict[str, str] = {}
    scope_hashes: dict[str, str] = {}

    if payload.type is RecordType.constraint:
        if payload.persistence_mode is PersistenceMode.source_bound:
            if not payload.files and not payload.symbols:
                raise ProposalValidationError(
                    "persistence_mode=source_bound requires at least one file or symbol"
                )
            code_index = read_current_code_index(layout)
            file_hashes = {f.path: f.content_hash for f in code_index.files}
            symbol_owning_file = {s.symbol_id: s.file for s in code_index.symbols}
            # DATA_MODEL.md §2.5: the snapshot's key set must equal
            # `files ∪ owning_file(s) for s in symbols` exactly -- a
            # missing file or an unresolved symbol means the resulting
            # source_hashes couldn't cover every reference, silently
            # binding to a partial subset rather than everything the
            # human actually approved. Reject outright rather than
            # accepting whatever subset happened to resolve (confirmed by
            # hand: files=["a.py", "GONE.py"] used to approve successfully
            # with only "a.py" recorded, silently dropping "GONE.py" from
            # what the constraint actually tracks).
            missing_files = [f for f in payload.files if f not in file_hashes]
            unresolved_symbols = [s for s in payload.symbols if s not in symbol_owning_file]
            if missing_files or unresolved_symbols:
                raise ProposalValidationError(
                    "persistence_mode=source_bound requires every file/symbol to resolve to a "
                    f"currently-indexed file -- unresolved files={missing_files}, "
                    f"symbols={unresolved_symbols}"
                )
            source_hashes = compute_source_hashes(
                payload.files, payload.symbols, file_hashes, symbol_owning_file
            )
        elif payload.persistence_mode is PersistenceMode.scope_bound:
            if not payload.scopes:
                raise ProposalValidationError("persistence_mode=scope_bound requires at least one scope")
            scope_by_id = {s.id: s for s in load_scopes(layout).scopes}
            missing = [sid for sid in payload.scopes if sid not in scope_by_id]
            if missing:
                raise ProposalValidationError(
                    f"persistence_mode=scope_bound references unknown scope(s): {missing}"
                )
            scope_hashes = {
                sid: compute_scope_membership_hash(scope_by_id[sid]) for sid in payload.scopes
            }

    next_revision = _next_record_revision(layout, payload.type, payload.record_id)
    created_by = (
        RevisionAuthor.human
        if edited_payload is not None
        else (RevisionAuthor.agent if proposal.created_by == "agent" else RevisionAuthor.human)
    )
    new_memory_revision = payload.model_copy(
        update={
            "revision": next_revision,
            "status": RecordStatus.active,
            "source_hashes": source_hashes,
            "scope_hashes": scope_hashes,
            "created_by": created_by,
            "approved_by": resolved_by,
            "created_at": now,
        }
    )

    resolved_proposal = proposal.model_copy(
        update={
            "revision": proposal.revision + 1,
            "status": ProposalStatus.edited if edited_payload is not None else ProposalStatus.approved,
            "payload": new_memory_revision,
            "resolved_at": now,
            "resolved_by": resolved_by,
        }
    )
    # Write the authoritative content first, the proposal's resolution
    # second. These are two separate canonical files -- each individual
    # append is atomic (canonical.append_jsonl's temp-file+rename), but
    # nothing makes the *pair* atomic, so a crash between them is
    # possible. This order picks the less-bad failure mode: if the
    # process dies after this line but before the next, decisions.jsonl/
    # constraints.jsonl already has the real content and the proposal
    # merely still shows `pending` -- visible and recoverable (re-running
    # `rune proposal approve` finds it still pending; a human notices the
    # stuck proposal). The reverse order risks the opposite: a proposal
    # that says `approved` while the content it supposedly approved was
    # never actually written anywhere -- a silent loss that looks
    # resolved and gives no signal anything is wrong.
    target_path = (
        layout.decisions_jsonl if payload.type is RecordType.decision else layout.constraints_jsonl
    )
    append_jsonl(target_path, new_memory_revision)
    append_jsonl(layout.proposals_jsonl, resolved_proposal)
    _refresh_cache(layout)
    return resolved_proposal, new_memory_revision


class RecordNotFoundError(Exception):
    pass


def deactivate(
    layout: RuneLayout, record_type: RecordType, record_id: str, *, by: str
) -> MemoryRevision:
    """A human explicitly turning off a Decision/Constraint (ARCHITECTURE.md
    §4.6's "人類明確停用" row) -- appends a new revision with `status=
    inactive`, a full snapshot of the current revision's content per
    DATA_MODEL.md §2.5's system-revision completeness rule (this is
    human-triggered, not system-triggered, but the same "never write a
    partial revision" principle applies: an `inactive` revision missing
    its `severity`/`persistence_mode`/snapshot fields would make a later
    reactivation lose that metadata). `inactive` is reserved for this
    explicit human action -- `core.memory.staleness` never appends it.
    """
    path = layout.decisions_jsonl if record_type is RecordType.decision else layout.constraints_jsonl
    existing = [r for r in read_jsonl(path, MemoryRevision) if r.record_id == record_id]
    current = current_by_record_id(existing)
    if record_id not in current:
        raise RecordNotFoundError(f"{record_type.value} {record_id!r} does not exist")
    current_rev = current[record_id]
    updated = current_rev.model_copy(
        update={
            "revision": current_rev.revision + 1,
            "status": RecordStatus.inactive,
            "created_by": RevisionAuthor.human,
            "approved_by": by,
            "created_at": utc_now_iso(),
        }
    )
    append_jsonl(path, updated)
    _refresh_cache(layout)
    return updated


def reject(layout: RuneLayout, proposal_id: str, *, resolved_by: str) -> Proposal:
    proposal = get_current_proposal(layout, proposal_id)
    if proposal.status is not ProposalStatus.pending:
        raise ProposalAlreadyResolvedError(
            f"proposal {proposal_id!r} is already {proposal.status.value}, not pending"
        )
    now = utc_now_iso()
    resolved_proposal = proposal.model_copy(
        update={
            "revision": proposal.revision + 1,
            "status": ProposalStatus.rejected,
            "resolved_at": now,
            "resolved_by": resolved_by,
        }
    )
    append_jsonl(layout.proposals_jsonl, resolved_proposal)
    return resolved_proposal
