"""System-triggered lifecycle transitions for Decision/Constraint/Note
(ARCHITECTURE.md §4.6, DATA_MODEL.md §6). Runs as part of every `rune
update`/`rune rebuild-cache` -- pure structural/hash comparison, never
calls an LLM, so unlike `core.semantic.worker` it always runs regardless
of provider availability or `full`/incremental mode.

Deliberately a *separate* module from `core.semantic.worker`, not a
shared abstraction over both: ScopeSummary staleness is "recompute the
content hash every time, no snapshot" (the description must always
reflect current code); Decision/Constraint/Note staleness is "compare the
current state against a snapshot taken at approval time" (a Decision
represents a point-in-time judgment call, not something that should
silently drift just because the referenced code changed). Conflating the
two would be a correctness bug, not a simplification -- see
DATA_MODEL.md §6's "關鍵區別" note.
"""

from __future__ import annotations

from datetime import datetime

from rune.core.memory.hashes import compute_scope_membership_hash, compute_source_hashes
from rune.core.storage.canonical import validated_copy
from rune.core.storage.models import (
    MemoryRevision,
    Note,
    NoteStatus,
    PersistenceMode,
    RecordStatus,
    RevisionAuthor,
    Scope,
)

# A revision already in one of these statuses is left alone: these are
# terminal from the system's point of view (only a human proposing new
# content, or explicit `deactivate`, moves a record out of them) -- this
# is what keeps every `rune update` from re-appending an identical
# "still missing" revision forever once a record has already been
# flagged once.
_DECISION_CONSTRAINT_TERMINAL = frozenset(
    {RecordStatus.orphaned, RecordStatus.review_required, RecordStatus.inactive}
)
_NOTE_TERMINAL = frozenset({NoteStatus.expired, NoteStatus.orphaned, NoteStatus.archived})


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _system_revision(
    current: MemoryRevision,
    new_status: RecordStatus,
    now: str,
    author: RevisionAuthor,
    extra: dict | None = None,
) -> MemoryRevision:
    """DATA_MODEL.md §2.5's completeness rule: a system-appended revision
    is a full snapshot of the previous current revision, only status/
    lifecycle metadata changed -- never a partial `{record_id, revision,
    status}` row, or the next staleness check would lose its comparison
    baseline (e.g. a source_bound constraint's `source_hashes`).

    `extra`, when given, is merged into the same update -- used by the
    source_bound/scope_bound branches to also refresh the snapshot itself
    to the *current* value (same rationale as `core.semantic.worker`'s
    failure-revision rule: without this, a constraint that just went
    `stale` would keep comparing against the same now-stale snapshot on
    every subsequent `rune update`, re-appending an identical `stale`
    revision forever instead of only once per actual change).
    """
    updates = {
        "revision": current.revision + 1,
        "status": new_status,
        "created_by": author,
        "approved_by": None,
        "created_at": now,
    }
    if extra:
        updates.update(extra)
    return validated_copy(current, updates)


def _existence_status(
    rev: MemoryRevision, known_scope_ids: set[str], known_files: set[str], known_symbol_ids: set[str]
) -> RecordStatus | None:
    """ARCHITECTURE.md §4.6's existence-based reaction, shared by
    Decision and (non-`persistent`) Constraint: a referenced scope that no
    longer exists in scopes.json outranks a merely-deleted file/symbol
    (orphaned, not just review_required) -- losing the whole scope means
    losing the context the record was scoped to, not just one supporting
    reference.
    """
    if rev.scopes and any(scope_id not in known_scope_ids for scope_id in rev.scopes):
        return RecordStatus.orphaned
    if any(f not in known_files for f in rev.files) or any(
        s not in known_symbol_ids for s in rev.symbols
    ):
        return RecordStatus.review_required
    return None


def detect_decision_transitions(
    current_decisions: dict[str, MemoryRevision],
    known_scope_ids: set[str],
    known_files: set[str],
    known_symbol_ids: set[str],
    now: str,
) -> list[MemoryRevision]:
    """Decision staleness is purely existence-based (ARCHITECTURE.md
    §4.6): content changing never moves a Decision, only a referenced
    file/symbol/scope disappearing does.
    """
    new_revisions: list[MemoryRevision] = []
    for rev in current_decisions.values():
        if rev.status in _DECISION_CONSTRAINT_TERMINAL:
            continue
        new_status = _existence_status(rev, known_scope_ids, known_files, known_symbol_ids)
        if new_status is not None:
            new_revisions.append(_system_revision(rev, new_status, now, RevisionAuthor.system_staleness))
    return new_revisions


def detect_constraint_transitions(
    current_constraints: dict[str, MemoryRevision],
    known_scope_ids: set[str],
    known_files: set[str],
    known_symbol_ids: set[str],
    file_hashes: dict[str, str],
    symbol_owning_file: dict[str, str],
    scope_by_id: dict[str, Scope],
    now: str,
) -> list[MemoryRevision]:
    """DATA_MODEL.md §6's per-`persistence_mode` table. `persistent`
    constraints never auto-transition at all (existence checks included --
    a human choosing `persistent` is explicitly opting the constraint out
    of any automatic reaction to its `scopes`/`files`/`symbols`
    disappearing, per ARCHITECTURE.md §4.6's table). Every other mode gets
    the same existence check as Decision first; only if nothing is
    missing does the mode-specific snapshot comparison run.
    """
    new_revisions: list[MemoryRevision] = []
    now_dt = _parse(now)
    for rev in current_constraints.values():
        if rev.status in _DECISION_CONSTRAINT_TERMINAL:
            continue
        if rev.persistence_mode is PersistenceMode.persistent:
            continue
        existence_status = _existence_status(rev, known_scope_ids, known_files, known_symbol_ids)
        if existence_status is not None:
            new_revisions.append(
                _system_revision(rev, existence_status, now, RevisionAuthor.system_staleness)
            )
            continue

        if rev.persistence_mode is PersistenceMode.source_bound:
            current_hashes = compute_source_hashes(rev.files, rev.symbols, file_hashes, symbol_owning_file)
            if current_hashes != rev.source_hashes:
                new_revisions.append(
                    _system_revision(
                        rev, RecordStatus.stale, now, RevisionAuthor.system_staleness,
                        extra={"source_hashes": current_hashes},
                    )
                )
        elif rev.persistence_mode is PersistenceMode.scope_bound:
            current_scope_hashes = {
                scope_id: compute_scope_membership_hash(scope_by_id[scope_id])
                for scope_id in rev.scopes
                if scope_id in scope_by_id
            }
            if current_scope_hashes != rev.scope_hashes:
                new_revisions.append(
                    _system_revision(
                        rev, RecordStatus.review_required, now, RevisionAuthor.system_staleness,
                        extra={"scope_hashes": current_scope_hashes},
                    )
                )
        elif rev.persistence_mode is PersistenceMode.temporary:
            # `rev.status is not stale` guards idempotency here specifically
            # (unlike source_bound above, `expires_at` never changes once
            # set, so without this check "now >= expires_at" would stay
            # true forever and re-append an identical stale revision on
            # every subsequent `rune update`).
            if (
                rev.status is not RecordStatus.stale
                and rev.expires_at is not None
                and now_dt >= _parse(rev.expires_at)
            ):
                new_revisions.append(
                    _system_revision(rev, RecordStatus.stale, now, RevisionAuthor.system_lifecycle)
                )
    return new_revisions


def _note_system_revision(
    current: Note, new_status: NoteStatus, now: str, author: RevisionAuthor, extra: dict | None = None
) -> Note:
    # `created_at` is deliberately left untouched -- DATA_MODEL.md §2.6
    # lists only status/source/last_verified_at as the fields a system
    # transition changes; created_at is the note's original creation
    # time and must survive across every subsequent revision.
    updates = {
        "revision": current.revision + 1,
        "status": new_status,
        "source": author,
        "last_verified_at": now,
    }
    if extra:
        updates.update(extra)
    return validated_copy(current, updates)


def detect_note_transitions(
    current_notes: dict[str, Note],
    known_scope_ids: set[str],
    file_hashes: dict[str, str],
    symbol_owning_file: dict[str, str],
    now: str,
) -> list[Note]:
    """DATA_MODEL.md §6: orphan (referenced scope gone) takes priority
    over TTL expiry, which takes priority over source-hash staleness.
    Only a note whose `source_hashes` is actually non-empty is
    source-bound at all -- otherwise it's treated as persistent
    regardless of category, matching every other category
    (`pitfall`/`observation`/`workaround`/`known_issue`) that wasn't
    explicitly bound to a file/symbol when written.
    """
    new_revisions: list[Note] = []
    now_dt = _parse(now)
    for note in current_notes.values():
        if note.status in _NOTE_TERMINAL:
            continue
        if note.scopes and any(scope_id not in known_scope_ids for scope_id in note.scopes):
            new_revisions.append(
                _note_system_revision(note, NoteStatus.orphaned, now, RevisionAuthor.system_staleness)
            )
            continue
        if note.expires_at is not None and now_dt >= _parse(note.expires_at):
            new_revisions.append(
                _note_system_revision(note, NoteStatus.expired, now, RevisionAuthor.system_lifecycle)
            )
            continue
        if note.source_hashes:
            current_hashes = compute_source_hashes(note.files, note.symbols, file_hashes, symbol_owning_file)
            if current_hashes != note.source_hashes:
                new_revisions.append(
                    _note_system_revision(
                        note, NoteStatus.stale, now, RevisionAuthor.system_staleness,
                        extra={"source_hashes": current_hashes},
                    )
                )
    return new_revisions
