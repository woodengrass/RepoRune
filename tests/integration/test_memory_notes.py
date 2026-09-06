from __future__ import annotations

from pathlib import Path

import pytest

from rune.core.memory.notes import (
    NoteNotFoundError,
    NoteValidationError,
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


def test_note_add_temporary_context_gets_default_ttl_from_config(git_repo: Path) -> None:
    """IMPLEMENTATION_PLAN.md's Milestone 6 deliverable ("依 category 有
    TTL"): a temporary_context Note written without an explicit
    --expires-at must still get one, from `config.notes.
    temporary_context_ttl_days` -- confirmed by hand this was previously
    dead code (NotesConfig's TTL fields existed but nothing read them),
    so this note would otherwise never expire.
    """
    from datetime import UTC, datetime

    layout = init_project(git_repo)
    note = note_add(
        layout, category=NoteCategory.temporary_context, content="c", why_persist="w",
    )
    assert note.expires_at is not None
    expiry = datetime.fromisoformat(note.expires_at)
    days_out = (expiry - datetime.now(UTC)).days
    assert 6 <= days_out <= 7  # default is 7 days


def test_note_add_investigation_result_gets_default_ttl_from_config(git_repo: Path) -> None:
    from datetime import UTC, datetime

    layout = init_project(git_repo)
    note = note_add(
        layout, category=NoteCategory.investigation_result, content="c", why_persist="w",
    )
    assert note.expires_at is not None
    expiry = datetime.fromisoformat(note.expires_at)
    days_out = (expiry - datetime.now(UTC)).days
    assert 29 <= days_out <= 30  # default is 30 days


def test_note_add_explicit_expires_at_overrides_ttl_default(git_repo: Path) -> None:
    layout = init_project(git_repo)
    note = note_add(
        layout, category=NoteCategory.temporary_context, content="c", why_persist="w",
        expires_at="2099-01-01T00:00:00Z",
    )
    assert note.expires_at == "2099-01-01T00:00:00Z"


def test_note_add_non_ttl_category_has_no_default_expiry(git_repo: Path) -> None:
    layout = init_project(git_repo)
    note = note_add(layout, category=NoteCategory.pitfall, content="c", why_persist="w")
    assert note.expires_at is None


def test_note_update_does_not_overwrite_created_at(git_repo: Path) -> None:
    """DATA_MODEL.md §2.6 lists only status/source/last_verified_at as the
    fields a revision transition changes -- created_at is the note's
    original creation time and must survive across every subsequent
    revision, not get reset to "now" on every update.
    """
    layout = init_project(git_repo)
    note = note_add(layout, category=NoteCategory.pitfall, content="c", why_persist="w")
    original_created_at = note.created_at

    import time
    time.sleep(1.1)  # ensure utc_now_iso() (second precision) actually differs

    updated = note_update(layout, note.id, content="revised")
    assert updated.created_at == original_created_at
    assert updated.last_verified_at != original_created_at


def test_note_add_rejects_unresolved_file(python_simple_repo: Path) -> None:
    """A relayed review confirmed by hand: note_add() had no equivalent
    of the "reject a partial snapshot" check core.memory.proposals.
    approve() applies to source_bound Constraints -- files=["app/
    services.py", "GONE.py"] used to succeed, silently recording a
    source_hashes snapshot covering only the file that resolved.
    """
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    with pytest.raises(NoteValidationError):
        note_add(
            layout, category=NoteCategory.observation, content="c", why_persist="w",
            files=["app/services.py", "GONE.py"],
        )


def test_note_add_rejects_unknown_scope(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    with pytest.raises(NoteValidationError):
        note_add(layout, category=NoteCategory.observation, content="c", why_persist="w", scopes=["nope"])


def test_note_update_can_change_files_and_recompute_source_hashes(python_simple_repo: Path) -> None:
    """A relayed review confirmed by hand: note_update() previously had no
    way to change scopes/files/symbols/expires_at/importance/confidence
    at all -- a Note's binding and TTL were frozen at creation.
    """
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    note = note_add(layout, category=NoteCategory.observation, content="c", why_persist="w")
    assert note.files == []

    updated = note_update(layout, note.id, files=["app/services.py"])
    assert updated.files == ["app/services.py"]
    assert "app/services.py" in updated.source_hashes


def test_note_update_rejects_unresolved_new_file(python_simple_repo: Path) -> None:
    layout = init_project(python_simple_repo)
    run_update(layout, full=True)
    note = note_add(layout, category=NoteCategory.observation, content="c", why_persist="w")
    with pytest.raises(NoteValidationError):
        note_update(layout, note.id, files=["GONE.py"])


def test_note_update_can_change_expires_at_and_clear_it(git_repo: Path) -> None:
    layout = init_project(git_repo)
    note = note_add(
        layout, category=NoteCategory.pitfall, content="c", why_persist="w",
        expires_at="2099-01-01T00:00:00Z",
    )
    updated = note_update(layout, note.id, expires_at="2099-06-01T00:00:00Z")
    assert updated.expires_at == "2099-06-01T00:00:00Z"

    cleared = note_update(layout, note.id, clear_expires_at=True)
    assert cleared.expires_at is None


def test_note_update_can_change_importance_and_confidence(git_repo: Path) -> None:
    layout = init_project(git_repo)
    note = note_add(
        layout, category=NoteCategory.pitfall, content="c", why_persist="w",
        importance=0.5, confidence=0.5,
    )
    updated = note_update(layout, note.id, importance=0.9, confidence=0.2)
    assert updated.importance == 0.9
    assert updated.confidence == 0.2


def test_note_update_rejects_naive_expires_at_without_poisoning_canonical(git_repo: Path) -> None:
    """Relayed review, reproduced by hand: `note_update` built the new
    revision via `current.model_copy(update=updates)`, which Pydantic
    does NOT re-validate -- a naive (non-UTC) `expires_at` was written
    straight into `notes.jsonl` as-is, and every subsequent `note`
    command then crashed reading it back (`read_jsonl` DOES validate).
    Fixed via `validated_copy` (re-runs every field's validator before
    the write); this now raises `NoteValidationError` immediately and
    the canonical file is never touched.
    """
    layout = init_project(git_repo)
    note = note_add(layout, category=NoteCategory.pitfall, content="c", why_persist="w")

    with pytest.raises(NoteValidationError):
        note_update(layout, note.id, expires_at="2026-09-07T12:00:00")  # naive, no tzinfo

    notes = read_jsonl(layout.notes_jsonl, Note)
    assert len(notes) == 1  # nothing appended
    # canonical is still readable and the note is unaffected
    still_current = get_current_note(layout, note.id)
    assert still_current.revision == 1
    assert still_current.expires_at is None


def test_note_update_rejects_out_of_range_importance_and_confidence(git_repo: Path) -> None:
    layout = init_project(git_repo)
    note = note_add(layout, category=NoteCategory.pitfall, content="c", why_persist="w")

    with pytest.raises(NoteValidationError):
        note_update(layout, note.id, importance=9.9, confidence=-2.0)

    notes = read_jsonl(layout.notes_jsonl, Note)
    assert len(notes) == 1


def test_note_add_rejects_naive_expires_at_with_a_clean_error(git_repo: Path) -> None:
    """Same underlying validator, but on the `note_add` construction path
    (a plain `Note(...)` call, not `model_copy`) -- this one already
    failed before writing anything, but as a raw `pydantic.
    ValidationError`, not the domain `NoteValidationError` every other
    rejection here raises. Low-severity CLI polish, fixed alongside the
    higher-severity `model_copy` bug since it's the same code path.
    """
    layout = init_project(git_repo)
    with pytest.raises(NoteValidationError):
        note_add(
            layout, category=NoteCategory.pitfall, content="c", why_persist="w",
            expires_at="2026-09-07T12:00:00",
        )
    assert read_jsonl(layout.notes_jsonl, Note) == []


def test_note_add_before_any_rune_update_does_not_create_a_misleading_empty_cache(git_repo: Path) -> None:
    """A relayed review confirmed by hand: refresh_cache() used to call
    rebuild_cache() with an empty CodeIndexData whenever memory.db didn't
    exist yet (nothing had ever been indexed), which *created* a cache
    whose files table was empty -- rune check/status then read "files
    table is empty" as "everything looks changed" instead of "no cache
    exists yet, nothing to compare against".
    """
    from rune.core.retrieval.check import check

    layout = init_project(git_repo)
    assert not layout.memory_db.exists()
    note_add(layout, category=NoteCategory.observation, content="c", why_persist="w")
    assert not layout.memory_db.exists()  # still no cache -- nothing to safely refresh

    result = check(layout)
    assert result.changed_files == []
