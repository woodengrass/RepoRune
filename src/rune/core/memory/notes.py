"""Note add/update (DATA_MODEL.md §2.6, ARCHITECTURE.md §4.6). Unlike
Decision/Constraint, a Note has no propose/approve gate -- an agent can
write one directly. `source_hashes` is still computed automatically here
(never hand-typed), same principle as `core.memory.proposals.approve`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from rune.core.config import load_config
from rune.core.memory.hashes import compute_source_hashes
from rune.core.memory.records import current_by_note_id
from rune.core.memory.records import refresh_cache as _refresh_cache
from rune.core.project import RuneLayout, utc_now_iso
from rune.core.semantic.redaction import redact_text
from rune.core.storage.canonical import append_jsonl, read_jsonl
from rune.core.storage.models import (
    Note,
    NoteCategory,
    NotesConfig,
    NoteStatus,
    RevisionAuthor,
)
from rune.core.storage.sqlite.materialize import read_current_code_index


class NoteNotFoundError(Exception):
    pass


def _redact(text: str, *, enabled: bool) -> str:
    return redact_text(text) if enabled else text


def _default_ttl_expiry(category: NoteCategory, notes_config: NotesConfig) -> str | None:
    """IMPLEMENTATION_PLAN.md's Milestone 6 deliverable ("依 category 有
    TTL") for the two TTL categories, using `config.notes`'s
    `temporary_context_ttl_days`/`investigation_result_ttl_days` -- these
    fields existed in `NotesConfig` since Milestone 1 but nothing ever
    read them (confirmed by grep: zero call sites), so a `temporary_
    context`/`investigation_result` Note written without an explicit
    `--expires-at` never actually expired. Only applies when the category
    has a TTL at all; every other category returns `None` (no default --
    those are TTL-exempt, per DATA_MODEL.md §6).
    """
    if category is NoteCategory.temporary_context:
        days = notes_config.temporary_context_ttl_days
    elif category is NoteCategory.investigation_result:
        days = notes_config.investigation_result_ttl_days
    else:
        return None
    return (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")


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

    `expires_at`, when not given explicitly, defaults from `config.notes`
    for the two TTL categories (`temporary_context`/`investigation_
    result`) -- an explicit caller-supplied value always wins.
    """
    now = utc_now_iso()
    source_hashes: dict[str, str] = {}
    if files or symbols:
        code_index = read_current_code_index(layout)
        file_hashes = {f.path: f.content_hash for f in code_index.files}
        symbol_owning_file = {s.symbol_id: s.file for s in code_index.symbols}
        source_hashes = compute_source_hashes(list(files), list(symbols), file_hashes, symbol_owning_file)

    if expires_at is None:
        config = load_config(layout.config_path)
        expires_at = _default_ttl_expiry(category, config.notes)

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
    _refresh_cache(layout)
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

    `created_at` is deliberately NOT touched here -- DATA_MODEL.md §2.6
    lists only `status`/`source`/`last_verified_at` as the fields a
    revision transition changes; `created_at` is the note's original
    creation time (from revision 1) and stays fixed across every
    subsequent revision, distinct from `last_verified_at` which tracks
    this revision's own timestamp.

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
    _refresh_cache(layout)
    return updated
