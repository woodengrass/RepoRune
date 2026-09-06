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
from rune.core.storage.sqlite.materialize import read_current_code_index, rebuild_cache


def _group_by_id[T](records: list[T], id_field: str) -> dict[str, list[T]]:
    grouped: dict[str, list[T]] = defaultdict(list)
    for record in records:
        grouped[getattr(record, id_field)].append(record)
    return dict(grouped)


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
    """
    rebuild_cache(layout, code_index=read_current_code_index(layout))
