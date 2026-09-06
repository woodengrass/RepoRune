from __future__ import annotations

from rune.core.memory.revisions import (
    current_revision,
    is_decision_constraint_visible,
    is_note_visible,
)
from rune.core.storage.models import (
    MemoryRevision,
    Note,
    NoteCategory,
    NoteStatus,
    RecordStatus,
    RecordType,
    RevisionAuthor,
)


def _decision(revision: int, status: RecordStatus) -> MemoryRevision:
    return MemoryRevision(
        record_id="d1", revision=revision, type=RecordType.decision, status=status,
        content="use postgres", created_by=RevisionAuthor.human, created_at="2026-01-01T00:00:00Z",
    )


def _note(revision: int, status: NoteStatus) -> Note:
    return Note(
        id="n1", revision=revision, category=NoteCategory.observation, content="c",
        why_persist="w", source=RevisionAuthor.agent, created_at="2026-01-01T00:00:00Z",
        last_verified_at="2026-01-01T00:00:00Z", expires_at=None, status=status,
    )


def test_current_revision_is_rev2_inactive_not_rev1_active() -> None:
    """DATA_MODEL.md §3's core bug fix, locked down: `current` must be
    `max(revision)` regardless of status. A naive "highest revision whose
    status is in the visible set" implementation would wrongly pick rev1
    (`active`) here instead of rev2 (`inactive`) -- meaning a Decision the
    user explicitly deactivated would keep showing up as if it were still
    active. This is the single most important invariant in Milestone 6.
    """
    rev1 = _decision(1, RecordStatus.active)
    rev2 = _decision(2, RecordStatus.inactive)
    current = current_revision([rev1, rev2])
    assert current is rev2
    assert current.revision == 2
    assert current.status is RecordStatus.inactive


def test_current_revision_is_rev2_even_when_rev1_appears_after_it_in_the_list() -> None:
    """current_revision must not assume input order reflects revision
    order -- canonical JSONL is read in file order, and a caller could
    hand revisions in whatever order they were collected.
    """
    rev1 = _decision(1, RecordStatus.active)
    rev2 = _decision(2, RecordStatus.inactive)
    assert current_revision([rev2, rev1]) is rev2


def test_current_revision_none_for_empty_list() -> None:
    assert current_revision([]) is None


def test_current_revision_single_revision() -> None:
    rev1 = _decision(1, RecordStatus.active)
    assert current_revision([rev1]) is rev1


def test_decision_constraint_visibility_table() -> None:
    assert is_decision_constraint_visible(RecordStatus.active) is True
    assert is_decision_constraint_visible(RecordStatus.review_required) is True
    assert is_decision_constraint_visible(RecordStatus.stale) is True
    assert is_decision_constraint_visible(RecordStatus.inactive) is False
    assert is_decision_constraint_visible(RecordStatus.orphaned) is False


def test_note_visibility_table() -> None:
    assert is_note_visible(NoteStatus.active) is True
    assert is_note_visible(NoteStatus.stale) is True
    assert is_note_visible(NoteStatus.expired) is False
    assert is_note_visible(NoteStatus.orphaned) is False
    assert is_note_visible(NoteStatus.archived) is False


def test_current_revision_works_for_notes_too() -> None:
    """The same generic current/visible split applies to Note, not just
    Decision/Constraint -- DATA_MODEL.md §2.6 explicitly says Note reuses
    "the same rule as §1", just with its own status enum/visibility table.
    """
    rev1 = _note(1, NoteStatus.active)
    rev2 = _note(2, NoteStatus.archived)
    current = current_revision([rev1, rev2])
    assert current is rev2
    assert is_note_visible(current.status) is False
