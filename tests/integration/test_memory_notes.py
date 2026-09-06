from __future__ import annotations

from pathlib import Path

import pytest

from rune.core.memory.notes import (
    NoteNotFoundError,
    get_current_note,
    note_add,
    note_update,
)
from rune.core.project import init_project
from rune.core.storage.canonical import read_jsonl
from rune.core.storage.models import Note, NoteCategory, NoteStatus, RevisionAuthor
from rune.core.update import run_update


def test_note_add_writes_first_revision(git_repo: Path) -> None:
    layout = init_project(git_repo)
    note = note_add(
        layout, category=NoteCategory.pitfall, content="watch out for X",
        why_persist="bit us once already",
    )
    assert note.revision == 1
    assert note.status is NoteStatus.active
    assert note.source_hashes == {}  # no files/symbols given -> persistent

    notes = read_jsonl(layout.notes_jsonl, Note)
    assert len(notes) == 1


def test_note_add_with_files_computes_source_hashes(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    note = note_add(
        layout, category=NoteCategory.implementation_detail, content="uses a raw cursor",
        why_persist="not obvious from the signature", files=["app/services.py"],
    )
    assert "app/services.py" in note.source_hashes


def test_note_add_redacts_secrets_by_default(git_repo: Path) -> None:
    layout = init_project(git_repo)
    note = note_add(
        layout, category=NoteCategory.observation,
        content="found key sk-abcdefghijklmnopqrstuvwxyz0123456789 in a log",
        why_persist="for the record",
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in note.content
    assert "[REDACTED]" in note.content


def test_note_update_appends_revision_carrying_forward_untouched_fields(git_repo: Path) -> None:
    layout = init_project(git_repo)
    note = note_add(
        layout, category=NoteCategory.pitfall, content="original", why_persist="why",
        importance=0.8,
    )
    updated = note_update(layout, note.id, content="revised", source="human")
    assert updated.revision == 2
    assert updated.content == "revised"
    assert updated.why_persist == "why"  # untouched, carried forward
    assert updated.importance == 0.8  # untouched, carried forward
    assert updated.source is RevisionAuthor.human

    current = get_current_note(layout, note.id)
    assert current.revision == 2
    assert current.content == "revised"


def test_note_update_can_transition_status(git_repo: Path) -> None:
    layout = init_project(git_repo)
    note = note_add(layout, category=NoteCategory.pitfall, content="c", why_persist="w")
    updated = note_update(layout, note.id, status=NoteStatus.archived)
    assert updated.status is NoteStatus.archived


def test_note_update_nonexistent_id_raises(git_repo: Path) -> None:
    layout = init_project(git_repo)
    with pytest.raises(NoteNotFoundError):
        note_update(layout, "nope", content="x")
