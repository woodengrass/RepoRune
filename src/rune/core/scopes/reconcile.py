"""`rune scope reconcile` (Milestone 9): a deterministic, auditable
generalization of the existing incremental import-based auto-assignment
(`assign_new_files_from_imports`, Milestone 4) into a full AUTO/KEEP/
REVIEW/BROKEN classification, per ARCHITECTURE.md §4.4's "Scope Membership
Reconciliation" section.

**"Changed set" is defined structurally, not via a working-tree diff.**
`rune update`'s own incremental scope auto-assignment (`core.update`) has
its own notion of "added files" because it runs *during* the same pass
that discovers them. `rune scope reconcile` runs later, against whatever
is already materialized (the intended flow is "merge branches -> `rune
update` on the merged tree -> `rune scope reconcile`", ARCHITECTURE.md
§16.4) -- so its changed set is simply the disagreement between canonical
`scopes.json` and the current materialized code index:

- A **file** present in the current index but a member of *no* scope is
  exactly the "added file" case Milestone 4 already has a rule for --
  reconcile applies that same rule (`high_confidence_import_candidates`).
  This is file-only, not symbol-only-too: Milestone 4's own auto-
  assignment rule was always file-only (import edges are a file-level
  relationship; there's no equivalent high-confidence heuristic for "this
  specific symbol, not just its file, belongs in scope X"), so there is
  deliberately no AUTO/REVIEW classification for a symbol that exists in
  the index but isn't a member of any scope's `members.symbols` -- most
  scopes only ever use `members.files` in practice, and treating every
  never-symbol-scoped function/class in the repo as a REVIEW entry every
  run would flood the report with noise no one asked reconcile to
  surface. Flagged, not silently decided: if per-symbol "never scoped"
  review turns out to be wanted, that is a scope expansion for a future
  round to confirm with the user first, per this project's own
  "先問使用者，再動手" convention -- not something this round should
  guess at, especially with no existing UX precedent for how noisy that
  would be on a real repo.
- A file **or symbol** that is a scope member but no longer exists in the
  current index is exactly the "deleted, membership target gone" case --
  this direction (existing membership -> BROKEN/REVIEW) does apply
  symmetrically to both, since it is not a "new item" flood risk: it is
  strictly bounded by how many memberships already exist in `scopes.json`.

Everything else -- a file that changed content but is *already* a member
of some scope, or was already correctly absent from all scopes -- is by
construction untouched by this diff: its relationship to scope membership
hasn't changed, so it's never visited, which *is* "untouched region
frozen" (ARCHITECTURE.md §4.4 point 1), not a separate filter bolted on
top.

**Deliberately NOT implemented (see IMPLEMENTATION_PLAN.md Milestone 9
decision log for the full reasoning)**: re-verifying whether an *already*-
assigned file (previously auto-assigned or human-added, the schema cannot
tell which -- DATA_MODEL.md §9) still uniquely satisfies the high-
confidence import rule after a merge changed the graph around it
(ARCHITECTURE.md §16.6's closing paragraph gestures at this). Doing that
for every existing membership is, in effect, re-clustering the whole
repository on every reconcile run, which every non-goal in this milestone
explicitly rules out. This is a known, documented gap, not an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from rune.core.project import RuneLayout
from rune.core.scopes.model import (
    high_confidence_import_candidates,
    load_scopes,
    member_files_by_unlocked_scope,
)
from rune.core.storage.models import ScopesFile, ScopeSource
from rune.core.storage.sqlite.materialize import read_current_code_index


class ReconcileClassification(str, Enum):
    auto = "AUTO"
    keep = "KEEP"
    review = "REVIEW"
    broken = "BROKEN"


@dataclass(frozen=True)
class ReconcileEntry:
    scope_id: str | None
    target_type: str  # "file" | "symbol"
    target: str
    classification: ReconcileClassification
    reason: str
    applied: bool = False
    candidate_scope_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconcileResult:
    full: bool
    entries: list[ReconcileEntry] = field(default_factory=list)
    auto_count: int = 0
    review_count: int = 0
    keep_count: int = 0
    broken_count: int = 0
    suspicious_churn: bool = False
    applied: bool = False
    # True only when at least one AUTO entry was actually written to the
    # returned ScopesFile this run -- distinct from `auto_count`, which
    # counts AUTO-*eligible* entries even when `full=True` or
    # `suspicious_churn=True` blocked every one of them from being applied.


def _is_protected(source: ScopeSource, locked: bool) -> bool:
    # Neither `Scope`/`ScopeMembers` carries per-membership provenance
    # (DATA_MODEL.md §9), so "human-authoritative" can only be
    # approximated at the whole-scope level: `locked` (an explicit human
    # action, `rune scope lock`) or `source == human` (created via `rune
    # scope create`, which defaults to `locked=True` but can be unlocked
    # later while remaining human-authored). Either one is enough to
    # protect every membership in that scope from silent removal.
    return locked or source is ScopeSource.human


def reconcile(
    layout: RuneLayout, *, full: bool = False, large_churn_threshold: int = 20
) -> tuple[ReconcileResult, ScopesFile | None]:
    """Computes the AUTO/KEEP/REVIEW/BROKEN classification and, for a
    non-`--full` run that isn't blocked by the large-churn guardrail,
    returns a mutated `ScopesFile` with every AUTO entry already applied
    (the caller -- the CLI -- is responsible for calling `save_scopes`
    with it, same defer-the-canonical-write pattern `core.update` uses
    elsewhere). Returns `(result, None)` whenever nothing should be
    written: `full=True` (output is always candidate/diff-only per
    ARCHITECTURE.md §4.4 point 4), no AUTO-eligible entries, or
    `suspicious_churn` tripped the large-churn guardrail (point 5).

    Never raises for a missing/empty code index -- an empty `CodeIndexData`
    (e.g. `memory.db` doesn't exist yet) just means every existing
    membership target looks "missing", which is the correct, honest
    answer (the caller should have required `rune update` to have run
    first; this function doesn't re-derive that policy).
    """
    scopes_file = load_scopes(layout)
    code_index = read_current_code_index(layout)
    known_files = {f.path for f in code_index.files}
    known_symbol_ids = {s.symbol_id for s in code_index.symbols}
    symbol_owning_file = {s.symbol_id: s.file for s in code_index.symbols}

    entries: list[ReconcileEntry] = []
    broken_count = 0
    keep_count = 0

    for scope in sorted(scopes_file.scopes, key=lambda s: s.id):
        protected = _is_protected(scope.source, scope.locked)
        for target in sorted(scope.members.files):
            if target in known_files:
                keep_count += 1
                if full:
                    entries.append(
                        ReconcileEntry(
                            scope.id, "file", target, ReconcileClassification.keep,
                            "membership target still present in the current code index",
                        )
                    )
                continue
            if protected:
                entries.append(
                    ReconcileEntry(
                        scope.id, "file", target, ReconcileClassification.broken,
                        "human/locked membership target no longer exists in the code "
                        "index; flagged for review, never silently removed",
                    )
                )
                broken_count += 1
            else:
                entries.append(
                    ReconcileEntry(
                        scope.id, "file", target, ReconcileClassification.review,
                        "membership target no longer exists in the code index; "
                        "candidate removal, needs human confirmation",
                    )
                )

        for target in sorted(scope.members.symbols):
            if target in known_symbol_ids:
                keep_count += 1
                if full:
                    entries.append(
                        ReconcileEntry(
                            scope.id, "symbol", target, ReconcileClassification.keep,
                            "membership target still present in the current code index",
                        )
                    )
                continue
            if protected:
                entries.append(
                    ReconcileEntry(
                        scope.id, "symbol", target, ReconcileClassification.broken,
                        "human/locked membership target no longer exists in the code "
                        "index; flagged for review, never silently removed",
                    )
                )
                broken_count += 1
            else:
                entries.append(
                    ReconcileEntry(
                        scope.id, "symbol", target, ReconcileClassification.review,
                        "membership target no longer exists in the code index; "
                        "candidate removal, needs human confirmation",
                    )
                )

    assigned_files = {f for scope in scopes_file.scopes for f in scope.members.files}
    member_files_by_scope = member_files_by_unlocked_scope(scopes_file, symbol_owning_file)
    auto_candidates: list[tuple[str, str]] = []
    for path in sorted(known_files - assigned_files):
        candidates = high_confidence_import_candidates(member_files_by_scope, path, code_index.edges)
        if len(candidates) == 1:
            scope_id = next(iter(candidates))
            entries.append(
                ReconcileEntry(
                    scope_id, "file", path, ReconcileClassification.auto,
                    "unique high-confidence (imports, confidence=1.0) edge into exactly "
                    "one unlocked scope",
                )
            )
            auto_candidates.append((path, scope_id))
        elif not candidates:
            entries.append(
                ReconcileEntry(
                    None, "file", path, ReconcileClassification.review,
                    "no high-confidence import evidence links this file to any scope",
                )
            )
        else:
            entries.append(
                ReconcileEntry(
                    None, "file", path, ReconcileClassification.review,
                    "ambiguous: high-confidence import evidence links this file to "
                    "more than one candidate scope",
                    candidate_scope_ids=tuple(sorted(candidates)),
                )
            )

    auto_count = len(auto_candidates)
    review_count = sum(1 for e in entries if e.classification is ReconcileClassification.review)
    suspicious_churn = auto_count > large_churn_threshold

    updated_scopes_file: ScopesFile | None = None
    applied = False
    if not full and auto_candidates and not suspicious_churn:
        updated_scopes_file = scopes_file.model_copy(deep=True)
        by_index = {s.id: i for i, s in enumerate(updated_scopes_file.scopes)}
        for path, scope_id in auto_candidates:
            idx = by_index[scope_id]
            scope = updated_scopes_file.scopes[idx]
            if path in scope.members.files:
                continue  # already a member somehow; nothing to apply
            updated_scopes_file.scopes[idx] = scope.model_copy(
                update={
                    "members": scope.members.model_copy(
                        update={"files": sorted([*scope.members.files, path])}
                    )
                }
            )
        applied = True
        entries = [
            ReconcileEntry(
                e.scope_id, e.target_type, e.target, e.classification, e.reason,
                applied=True, candidate_scope_ids=e.candidate_scope_ids,
            )
            if e.classification is ReconcileClassification.auto
            else e
            for e in entries
        ]

    result = ReconcileResult(
        full=full,
        entries=entries,
        auto_count=auto_count,
        review_count=review_count,
        keep_count=keep_count,
        broken_count=broken_count,
        suspicious_churn=suspicious_churn,
        applied=applied,
    )
    return result, updated_scopes_file
