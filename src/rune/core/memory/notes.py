"""Note add/update (DATA_MODEL.md §2.6, ARCHITECTURE.md §4.6). Unlike
Decision/Constraint, a Note has no propose/approve gate -- an agent can
write one directly. `source_hashes` is still computed automatically here
(never hand-typed), same principle as `core.memory.proposals.approve`.
"""

from __future__ import annotations

import uuid
from typing import Literal

from rune.core.memory.hashes import compute_source_hashes
from rune.core.memory.records import current_by_note_id
from rune.core.project import RuneLayout, utc_now_iso
from rune.core.semantic.redaction import redact_text
from rune.core.storage.canonical import append_jsonl, read_jsonl
from rune.core.storage.models import Note, NoteCategory, NoteStatus, RevisionAuthor
from rune.core.storage.sqlite.materialize import read_current_code_index


class NoteNotFoundError(Exception):
    pass


def _redact(text: str, *, enabled: bool) -> str:
    return redact_text(text) if enabled else text


def note_add(
    layout: RuneLayout,
    *,
    category: NoteCategory,
    content: str,
    why_persist: str,
    scopes: list[str] = (),
    files: list[str] = (),
    symbols: list[str] = (),
    importance: float = 0.5,
    confidence: float = 0.5,
    evidence: list[str] = (),
    expires_at: str | None = None,
    source: Literal["agent", "human"] = "agent",
    redact_secrets: bool = True,
) -> Note:
    """Writes a brand-new Note (`revision=1`). `source_hashes` is
    auto-computed from the current code index whenever `files`/`symbols`
    is non-empty -- that's what makes the note "source_bound" for
    staleness purposes (DATA_MODEL.md §6: "只有設了 files/symbols/
    source_hashes 才 source-bound，否則視為 persistent"); a note with
    neither stays persistent regardless of category.
    """
    now = utc_now_iso()
    source_hashes: dict[str, str] = {}
    if files or symbols:
        code_index = read_current_code_index(layout)
        file_hashes = {f.path: f.content_hash for f in code_index.files}
        symbol_owning_file = {s.symbol_id: s.file for s in code_index.symbols}
        source_hashes = compute_source_hashes(list(files), list(symbols), file_hashes, symbol_owning_file)

    note = Note(
        id=str(uuid.uuid4()),
        revision=1,
        category=category,
        content=_redact(content, enabled=redact_secrets),
        why_persist=_redact(why_persist, enabled=redact_secrets),
        scopes=sorted(set(scopes)),
        files=sorted(set(files)),
        symbols=sorted(set(symbols)),
        importance=importance,
        confidence=confidence,
        source=RevisionAuthor.agent if source == "agent" else RevisionAuthor.human,
        evidence=[_redact(e, enabled=redact_secrets) for e in evidence],
        created_at=now,
        last_verified_at=now,
        expires_at=expires_at,
        source_hashes=source_hashes,
        status=NoteStatus.active,
    )
    append_jsonl(layout.notes_jsonl, note)
    return note


def get_current_note(layout: RuneLayout, note_id: str) -> Note:
    current = current_by_note_id(read_jsonl(layout.notes_jsonl, Note))
    note = current.get(note_id)
    if note is None:
        raise NoteNotFoundError(f"note {note_id!r} does not exist")
    return note


def note_update(
    layout: RuneLayout,
    note_id: str,
    *,
    content: str | None = None,
    why_persist: str | None = None,
    status: NoteStatus | None = None,
    evidence: list[str] | None = None,
    source: Literal["agent", "human"] = "agent",
    redact_secrets: bool = True,
    recompute_source_hashes: bool = False,
) -> Note:
    """"Updating" a note = appending `revision = current + 1`. Every field
    not explicitly overridden is carried forward unchanged from the
    current revision -- the same "complete snapshot, only the touched
    fields change" rule every other system/human-appended revision in
    this project follows (DATA_MODEL.md §2.5's completeness rule, applied
    here even though this is an agent/human action rather than a system
    one, because the underlying reason -- never losing metadata a later
    reader needs -- is the same).

    `recompute_source_hashes=True` re-snapshots `files`/`symbols` against
    the current code index (e.g. after verifying a `stale` note's issue
    was actually fixed and it's being un-staled) -- default False leaves
    the existing snapshot untouched, since most updates (re-verifying,
    archiving) aren't about the source content itself.
    """
    current = get_current_note(layout, note_id)
    now = utc_now_iso()
    source_hashes = current.source_hashes
    if recompute_source_hashes and (current.files or current.symbols):
        code_index = read_current_code_index(layout)
        file_hashes = {f.path: f.content_hash for f in code_index.files}
        symbol_owning_file = {s.symbol_id: s.file for s in code_index.symbols}
        source_hashes = compute_source_hashes(
            current.files, current.symbols, file_hashes, symbol_owning_file
        )

    updates: dict = {
        "revision": current.revision + 1,
        "source": RevisionAuthor.agent if source == "agent" else RevisionAuthor.human,
        "last_verified_at": now,
        "created_at": now,
        "source_hashes": source_hashes,
    }
    if content is not None:
        updates["content"] = _redact(content, enabled=redact_secrets)
    if why_persist is not None:
        updates["why_persist"] = _redact(why_persist, enabled=redact_secrets)
    if status is not None:
        updates["status"] = status
    if evidence is not None:
        updates["evidence"] = [_redact(e, enabled=redact_secrets) for e in evidence]

    updated = current.model_copy(update=updates)
    append_jsonl(layout.notes_jsonl, updated)
    return updated
