"""Shared current/visible revision logic for Decision/Constraint/Note
(DATA_MODEL.md §3, IMPLEMENTATION_PLAN.md Milestone 6).

This is deliberately the first piece of `core.memory` implemented: DATA_MODEL
§3 calls the current/visible split "the most easily reintroduced bug" in this
milestone, and every other Milestone 6 piece (staleness, proposals, search
ranking) builds on top of `current_revision` being correct.
"""

from __future__ import annotations

from typing import Protocol

from rune.core.storage.models import NoteStatus, RecordStatus


class _Revisioned(Protocol):
    revision: int


def current_revision[T: _Revisioned](revisions: list[T]) -> T | None:
    """current = `max(revision)`, unconditionally -- never filtered by
    `status`. This is DATA_MODEL.md §3's core bug fix: an earlier design
    picked "the highest revision whose status is in {active,
    review_required}" as current, which meant deactivating a Decision
    (appending rev2 with status=inactive) left rev1 (status=active)
    showing as current -- the opposite of what "I turned this off" is
    supposed to mean. Whether a current revision actually shows up in
    retrieval is an entirely separate, later-applied question -- see
    `is_decision_constraint_visible`/`is_note_visible` below. Returns
    `None` for an empty list (a `record_id`/`id` with zero revisions
    shouldn't normally occur, but this must not raise on it).
    """
    if not revisions:
        return None
    return max(revisions, key=lambda r: r.revision)


_DECISION_CONSTRAINT_VISIBLE_STATUSES = frozenset(
    {RecordStatus.active, RecordStatus.review_required, RecordStatus.stale}
)


def is_decision_constraint_visible(status: RecordStatus) -> bool:
    """DATA_MODEL.md §3's visibility table for a Decision/Constraint
    current revision: `active`/`review_required`/`stale` all show in
    default retrieval (the latter two carry a warning marker, applied by
    the retrieval layer -- this function only decides show/hide);
    `inactive`/`orphaned` are excluded. `stale` only actually occurs for
    Constraint (Decision has no `stale` status transition), but the
    lookup table itself is shared -- there's nothing Decision-specific to
    special-case here.
    """
    return status in _DECISION_CONSTRAINT_VISIBLE_STATUSES


_NOTE_VISIBLE_STATUSES = frozenset({NoteStatus.active, NoteStatus.stale})


def is_note_visible(status: NoteStatus) -> bool:
    """DATA_MODEL.md §2.6/§6's visibility table for a Note current
    revision: `active`/`stale` show (`stale` marked `[STALE]`, not
    hidden); `expired`/`orphaned` are excluded from default retrieval;
    `archived` is excluded from default retrieval too and only appears in
    an explicit history/audit read.
    """
    return status in _NOTE_VISIBLE_STATUSES
