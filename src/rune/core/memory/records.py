"""Canonical read helpers for Decision/Constraint/Note: group raw JSONL
revisions by logical id and pick out each one's current revision
(`rune.core.memory.revisions.current_revision`). Shared by `core.memory.
staleness`, `core.retrieval.search`, `rune check`, and CLI list commands --
none of them re-implement grouping/current-selection themselves.
"""

from __future__ import annotations

from collections import defaultdict

from rune.core.memory.revisions import current_revision
from rune.core.project import RuneLayout
from rune.core.storage.canonical import read_jsonl
from rune.core.storage.models import MemoryRevision, Note, RecordType
from rune.core.storage.sqlite.materialize import (
    CanonicalConflictError,
    read_current_code_index,
    rebuild_cache,
)


def _group_by_id[T](records: list[T], id_field: str) -> dict[str, list[T]]:
    """Groups by logical id, rejecting a duplicate (id, revision) pair
    the same way `materialize._group_current_by_id` already does.

    This matters because `core.memory` reads canonical directly (propose/
    approve/note_add/staleness all call `current_by*` here, not through
    `rebuild_cache`), so without this check a corrupted canonical file
    with two revisions sharing the same number would silently resolve to
    an arbitrary one here -- computing the wrong `next_revision`, showing
    the wrong current content -- while `rune update`/`rebuild-cache`
    would correctly refuse to materialize the very same file. Both read
    paths must treat a canonical conflict as fatal, not just one of them.
    """
    grouped: dict[str, dict[int, T]] = defaultdict(dict)
    for record in records:
        record_id = getattr(record, id_field)
        revision = record.revision
        if revision in grouped[record_id]:
            raise CanonicalConflictError(
                f"duplicate ({id_field}={record_id!r}, revision={revision}) found in canonical "
                f"data -- refusing to silently pick one. Resolve manually (edit the file to "
                f"remove/renumber one line)."
            )
        grouped[record_id][revision] = record
    return {record_id: list(by_revision.values()) for record_id, by_revision in grouped.items()}


def current_by[T](records: list[T], id_field: str) -> dict[str, T]:
    """{logical id: current revision}, current = max(revision) regardless
    of status (DATA_MODEL.md §3) -- callers apply visibility separately.
    Generic over `MemoryRevision`/`Note`/`Proposal` -- all three share
    nothing but an int `revision` field and some string id field.
    """
    grouped = _group_by_id(records, id_field)
    result: dict[str, T] = {}
    for record_id, revs in grouped.items():
        current = current_revision(revs)
        if current is not None:
            result[record_id] = current
    return result


def current_by_record_id(revisions: list[MemoryRevision]) -> dict[str, MemoryRevision]:
    return current_by(revisions, "record_id")


def current_by_note_id(notes: list[Note]) -> dict[str, Note]:
    return current_by(notes, "id")


class RecordNotFoundError(Exception):
    pass


def _get_record[T](revisions_for_id: list[T], record_label: str, *, include_history: bool) -> tuple[T, list[T] | None]:
    if not revisions_for_id:
        raise RecordNotFoundError(f"no such {record_label}")
    current = current_revision(revisions_for_id)
    assert current is not None  # non-empty input, current_revision only returns None for []
    history = sorted(revisions_for_id, key=lambda r: r.revision) if include_history else None
    return current, history


def get_decision(
    layout: RuneLayout, record_id: str, *, include_history: bool = False
) -> tuple[MemoryRevision, list[MemoryRevision] | None]:
    """(current, history) for one Decision by id -- `history` is `None`
    unless `include_history=True` (mirrors `rune search --history`'s
    "history is an explicit opt-in, not always computed" convention).
    Raises `RecordNotFoundError` if `record_id` has zero revisions.
    """
    revisions = read_jsonl(layout.decisions_jsonl, MemoryRevision)
    matching = [r for r in revisions if r.type is RecordType.decision and r.record_id == record_id]
    return _get_record(matching, f"decision {record_id!r}", include_history=include_history)


def get_constraint(
    layout: RuneLayout, record_id: str, *, include_history: bool = False
) -> tuple[MemoryRevision, list[MemoryRevision] | None]:
    revisions = read_jsonl(layout.constraints_jsonl, MemoryRevision)
    matching = [r for r in revisions if r.type is RecordType.constraint and r.record_id == record_id]
    return _get_record(matching, f"constraint {record_id!r}", include_history=include_history)


def get_note(layout: RuneLayout, note_id: str, *, include_history: bool = False) -> tuple[Note, list[Note] | None]:
    revisions = [n for n in read_jsonl(layout.notes_jsonl, Note) if n.id == note_id]
    return _get_record(revisions, f"note {note_id!r}", include_history=include_history)


def load_current_decisions(layout: RuneLayout) -> dict[str, MemoryRevision]:
    revisions = read_jsonl(layout.decisions_jsonl, MemoryRevision)
    return current_by_record_id([r for r in revisions if r.type is RecordType.decision])


def load_current_constraints(layout: RuneLayout) -> dict[str, MemoryRevision]:
    revisions = read_jsonl(layout.constraints_jsonl, MemoryRevision)
    return current_by_record_id([r for r in revisions if r.type is RecordType.constraint])


def load_current_notes(layout: RuneLayout) -> dict[str, Note]:
    return current_by_note_id(read_jsonl(layout.notes_jsonl, Note))


def refresh_cache(layout: RuneLayout) -> None:
    """Re-materializes `memory.db` from the current canonical files right
    after a single decision/constraint/note write, so `rune search`/
    `rune check` see it without waiting for the next `rune update`
    (confirmed with the user: SQLite being a derived cache that only
    refreshes on `rune update` meant a just-approved Decision/Constraint
    or a just-added Note was invisible to search for the rest of the
    session, since the Milestone 7 OpenCode adapter deliberately never
    auto-triggers a full `rune update` on every tool call).

    Deliberately NOT the same cost as `rune update`: this reuses
    `read_current_code_index` (the already-materialized files/symbols/
    edges) instead of re-scanning and re-parsing the source tree, so it's
    a plain "re-read every canonical file, re-run the existing single
    SQLite transaction" pass -- the same work `rune rebuild-cache` does,
    minus the scan. Safe to call after every propose/approve/note write;
    if it fails (e.g. a canonical conflict), the canonical write that
    already happened stands and the cache simply stays behind until the
    next successful `rune update`/`rebuild-cache` -- consistent with
    every other place in this project where the derived cache is allowed
    to lag behind canonical without corrupting anything.

    A no-op when `memory.db` doesn't exist yet at all: `read_current_
    code_index` returns an empty `CodeIndexData` in that case, and
    calling `rebuild_cache` with that would *create* a cache whose
    `files` table is empty -- confirmed by hand this actively misleads
    `rune check`/`rune status`, which read "the files table is empty" as
    "nothing has ever been indexed" and so treat every file as newly
    added/changed, not as "cache absent, nothing to compare against". A
    project that hasn't run `rune init`/`rune update` yet has no code
    index to preserve, so there's nothing for this "lightweight" refresh
    to safely do -- the canonical write already succeeded regardless, and
    the first real `rune update` will materialize everything properly.
    """
    if not layout.memory_db.exists():
        return
    rebuild_cache(layout, code_index=read_current_code_index(layout))
