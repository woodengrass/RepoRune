from __future__ import annotations

from rune.core.memory.hashes import compute_scope_membership_hash, compute_source_hashes
from rune.core.memory.staleness import (
    detect_constraint_transitions,
    detect_decision_transitions,
    detect_note_transitions,
)
from rune.core.storage.models import (
    MemoryRevision,
    Note,
    NoteCategory,
    NoteStatus,
    PersistenceMode,
    RecordStatus,
    RecordType,
    RevisionAuthor,
    Scope,
    ScopeMembers,
    ScopeSource,
    Severity,
)

_NOW = "2026-01-02T00:00:00Z"


def _decision(
    record_id: str = "d1", status: RecordStatus = RecordStatus.active,
    scopes: list[str] = (), files: list[str] = (), symbols: list[str] = (),
) -> MemoryRevision:
    return MemoryRevision(
        record_id=record_id, revision=1, type=RecordType.decision, status=status,
        content="use postgres", scopes=list(scopes), files=list(files), symbols=list(symbols),
        created_by=RevisionAuthor.human, created_at="2026-01-01T00:00:00Z",
    )


def _constraint(
    record_id: str = "c1", status: RecordStatus = RecordStatus.active,
    scopes: list[str] = (), files: list[str] = (), symbols: list[str] = (),
    persistence_mode: PersistenceMode = PersistenceMode.persistent,
    source_hashes: dict[str, str] | None = None, scope_hashes: dict[str, str] | None = None,
    expires_at: str | None = None,
) -> MemoryRevision:
    return MemoryRevision(
        record_id=record_id, revision=1, type=RecordType.constraint, status=status,
        content="no bare except", scopes=list(scopes), files=list(files), symbols=list(symbols),
        severity=Severity.must, persistence_mode=persistence_mode,
        source_hashes=source_hashes or {}, scope_hashes=scope_hashes or {}, expires_at=expires_at,
        created_by=RevisionAuthor.human, created_at="2026-01-01T00:00:00Z",
    )


def _note(
    note_id: str = "n1", status: NoteStatus = NoteStatus.active,
    scopes: list[str] = (), files: list[str] = (), symbols: list[str] = (),
    source_hashes: dict[str, str] | None = None, expires_at: str | None = None,
) -> Note:
    return Note(
        id=note_id, revision=1, category=NoteCategory.observation, content="c", why_persist="w",
        scopes=list(scopes), files=list(files), symbols=list(symbols),
        source=RevisionAuthor.agent, created_at="2026-01-01T00:00:00Z",
        last_verified_at="2026-01-01T00:00:00Z", expires_at=expires_at,
        source_hashes=source_hashes or {}, status=status,
    )


# --------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------


def test_decision_content_change_is_not_a_trigger() -> None:
    """ARCHITECTURE.md §4.6: a Decision represents a choice, not a
    description of current code -- referenced file content changing must
    never move it, only existence (file/symbol/scope disappearing) does.
    `content_hash` doesn't even appear in this function's inputs, so this
    is really testing "changed-but-still-present files don't trigger
    anything", not a hash comparison at all.
    """
    rev = _decision(files=["a.py"])
    result = detect_decision_transitions(
        {"d1": rev}, known_scope_ids=set(), known_files={"a.py"}, known_symbol_ids=set(), now=_NOW
    )
    assert result == []


def test_decision_deleted_file_triggers_review_required() -> None:
    rev = _decision(files=["a.py"])
    result = detect_decision_transitions(
        {"d1": rev}, known_scope_ids=set(), known_files=set(), known_symbol_ids=set(), now=_NOW
    )
    assert len(result) == 1
    assert result[0].status is RecordStatus.review_required
    assert result[0].revision == 2
    assert result[0].created_by is RevisionAuthor.system_staleness
    assert result[0].content == "use postgres"  # full snapshot, not a stub


def test_decision_deleted_scope_triggers_orphaned_not_review_required() -> None:
    rev = _decision(scopes=["auth"], files=["a.py"])
    result = detect_decision_transitions(
        {"d1": rev}, known_scope_ids=set(), known_files={"a.py"}, known_symbol_ids=set(), now=_NOW
    )
    assert len(result) == 1
    assert result[0].status is RecordStatus.orphaned


def test_decision_already_review_required_is_not_touched_again() -> None:
    """Idempotency: once flagged, a decision stays flagged until a human
    acts (new active revision or explicit deactivate) -- the system must
    not re-append an identical review_required revision on every
    subsequent `rune update`.
    """
    rev = _decision(status=RecordStatus.review_required, files=["a.py"])
    result = detect_decision_transitions(
        {"d1": rev}, known_scope_ids=set(), known_files=set(), known_symbol_ids=set(), now=_NOW
    )
    assert result == []


def test_decision_inactive_is_not_touched() -> None:
    rev = _decision(status=RecordStatus.inactive, files=["a.py"])
    result = detect_decision_transitions(
        {"d1": rev}, known_scope_ids=set(), known_files=set(), known_symbol_ids=set(), now=_NOW
    )
    assert result == []


# --------------------------------------------------------------------------
# Constraint
# --------------------------------------------------------------------------


def test_constraint_persistent_never_transitions_even_on_deletion() -> None:
    """DATA_MODEL.md §6's literal table entry: persistent constraints
    "永不自動變動" -- this is a deliberate blanket exemption, not just for
    content changes, confirmed by testing it against a deleted scope too.
    """
    rev = _constraint(persistence_mode=PersistenceMode.persistent, scopes=["gone"])
    result = detect_constraint_transitions(
        {"c1": rev}, known_scope_ids=set(), known_files=set(), known_symbol_ids=set(),
        file_hashes={}, symbol_owning_file={}, scope_by_id={}, now=_NOW,
    )
    assert result == []


def test_constraint_source_bound_hash_mismatch_is_stale() -> None:
    rev = _constraint(
        persistence_mode=PersistenceMode.source_bound, files=["a.py"],
        source_hashes={"a.py": "sha256:old"},
    )
    result = detect_constraint_transitions(
        {"c1": rev}, known_scope_ids=set(), known_files={"a.py"}, known_symbol_ids=set(),
        file_hashes={"a.py": "sha256:new"}, symbol_owning_file={}, scope_by_id={}, now=_NOW,
    )
    assert len(result) == 1
    assert result[0].status is RecordStatus.stale
    # The snapshot itself is refreshed to the current hash, not left
    # pointing at the old one -- otherwise every subsequent `rune update`
    # would keep comparing against the same now-stale baseline and
    # re-append an identical `stale` revision forever (same rationale as
    # core.semantic.worker's failure-revision rule updating source_hash).
    assert result[0].source_hashes == {"a.py": "sha256:new"}


def test_constraint_source_bound_stale_transition_is_idempotent() -> None:
    """Regression test for the infinite-reappend bug: running the check
    twice in a row with the same (still-mismatched-from-the-original)
    content must only produce one transition on the first run and zero on
    the second, because the first run's appended revision already
    refreshed the snapshot to match.
    """
    rev = _constraint(
        persistence_mode=PersistenceMode.source_bound, files=["a.py"],
        source_hashes={"a.py": "sha256:old"},
    )
    first = detect_constraint_transitions(
        {"c1": rev}, known_scope_ids=set(), known_files={"a.py"}, known_symbol_ids=set(),
        file_hashes={"a.py": "sha256:new"}, symbol_owning_file={}, scope_by_id={}, now=_NOW,
    )
    assert len(first) == 1
    second = detect_constraint_transitions(
        {"c1": first[0]}, known_scope_ids=set(), known_files={"a.py"}, known_symbol_ids=set(),
        file_hashes={"a.py": "sha256:new"}, symbol_owning_file={}, scope_by_id={}, now=_NOW,
    )
    assert second == []


def test_constraint_temporary_expiry_transition_is_idempotent() -> None:
    """Same infinite-reappend risk as source_bound, but for a different
    reason: `expires_at` never changes once set, so without an explicit
    "already stale" guard, "now >= expires_at" stays true forever.
    """
    rev = _constraint(persistence_mode=PersistenceMode.temporary, expires_at="2026-01-01T00:00:00Z")
    first = detect_constraint_transitions(
        {"c1": rev}, known_scope_ids=set(), known_files=set(), known_symbol_ids=set(),
        file_hashes={}, symbol_owning_file={}, scope_by_id={}, now=_NOW,
    )
    assert len(first) == 1
    second = detect_constraint_transitions(
        {"c1": first[0]}, known_scope_ids=set(), known_files=set(), known_symbol_ids=set(),
        file_hashes={}, symbol_owning_file={}, scope_by_id={}, now=_NOW,
    )
    assert second == []


def test_constraint_source_bound_hash_match_is_unchanged() -> None:
    rev = _constraint(
        persistence_mode=PersistenceMode.source_bound, files=["a.py"],
        source_hashes={"a.py": "sha256:same"},
    )
    result = detect_constraint_transitions(
        {"c1": rev}, known_scope_ids=set(), known_files={"a.py"}, known_symbol_ids=set(),
        file_hashes={"a.py": "sha256:same"}, symbol_owning_file={}, scope_by_id={}, now=_NOW,
    )
    assert result == []


def test_constraint_source_bound_deleted_file_is_review_required_not_stale() -> None:
    """Existence check outranks the hash comparison: a deleted file is a
    review_required case (Decision-parity), not silently folded into
    "the hash changed" -- deletion is a more serious event than a content
    edit.
    """
    rev = _constraint(
        persistence_mode=PersistenceMode.source_bound, files=["a.py"],
        source_hashes={"a.py": "sha256:old"},
    )
    result = detect_constraint_transitions(
        {"c1": rev}, known_scope_ids=set(), known_files=set(), known_symbol_ids=set(),
        file_hashes={}, symbol_owning_file={}, scope_by_id={}, now=_NOW,
    )
    assert len(result) == 1
    assert result[0].status is RecordStatus.review_required


def test_constraint_scope_bound_membership_change_is_review_required() -> None:
    old_scope = Scope(
        id="auth", name="Auth", source=ScopeSource.human,
        members=ScopeMembers(files=["a.py"]),
    )
    new_scope = Scope(
        id="auth", name="Auth", source=ScopeSource.human,
        members=ScopeMembers(files=["a.py", "b.py"]),  # membership grew
    )
    rev = _constraint(
        persistence_mode=PersistenceMode.scope_bound, scopes=["auth"],
        scope_hashes={"auth": compute_scope_membership_hash(old_scope)},
    )
    result = detect_constraint_transitions(
        {"c1": rev}, known_scope_ids={"auth"}, known_files=set(), known_symbol_ids=set(),
        file_hashes={}, symbol_owning_file={}, scope_by_id={"auth": new_scope}, now=_NOW,
    )
    assert len(result) == 1
    assert result[0].status is RecordStatus.review_required


def test_constraint_temporary_expired_is_stale_not_inactive() -> None:
    """DATA_MODEL.md §6: expiry of a temporary constraint is `stale`, NOT
    `inactive` -- `inactive` is reserved for an explicit human decision
    that this rule is truly no longer needed; expiry only means "the
    original time limit passed", which may still be worth surfacing.
    """
    rev = _constraint(
        persistence_mode=PersistenceMode.temporary, expires_at="2026-01-01T00:00:00Z",
    )
    result = detect_constraint_transitions(
        {"c1": rev}, known_scope_ids=set(), known_files=set(), known_symbol_ids=set(),
        file_hashes={}, symbol_owning_file={}, scope_by_id={}, now=_NOW,
    )
    assert len(result) == 1
    assert result[0].status is RecordStatus.stale
    assert result[0].created_by is RevisionAuthor.system_lifecycle


def test_constraint_temporary_not_yet_expired_is_unchanged() -> None:
    rev = _constraint(
        persistence_mode=PersistenceMode.temporary, expires_at="2099-01-01T00:00:00Z",
    )
    result = detect_constraint_transitions(
        {"c1": rev}, known_scope_ids=set(), known_files=set(), known_symbol_ids=set(),
        file_hashes={}, symbol_owning_file={}, scope_by_id={}, now=_NOW,
    )
    assert result == []


# --------------------------------------------------------------------------
# Note
# --------------------------------------------------------------------------


def test_note_orphan_takes_priority_over_everything_else() -> None:
    note = _note(
        scopes=["gone"], files=["a.py"], source_hashes={"a.py": "sha256:old"},
        expires_at="2026-01-01T00:00:00Z",  # already expired too, but orphan wins
    )
    result = detect_note_transitions(
        {"n1": note}, known_scope_ids=set(), file_hashes={"a.py": "sha256:new"},
        symbol_owning_file={}, now=_NOW,
    )
    assert len(result) == 1
    assert result[0].status is NoteStatus.orphaned


def test_note_system_transition_does_not_overwrite_created_at() -> None:
    """DATA_MODEL.md §2.6 lists only status/source/last_verified_at as the
    fields a system-triggered Note revision changes -- created_at (the
    note's original creation time) must survive, not get reset to "now"
    on every automatic transition (orphan/expiry/staleness alike).
    """
    note = _note(scopes=["gone"])
    result = detect_note_transitions(
        {"n1": note}, known_scope_ids=set(), file_hashes={}, symbol_owning_file={}, now=_NOW
    )
    assert len(result) == 1
    assert result[0].created_at == note.created_at
    assert result[0].last_verified_at == _NOW


def test_note_ttl_expiry_is_expired() -> None:
    note = _note(expires_at="2026-01-01T00:00:00Z")
    result = detect_note_transitions(
        {"n1": note}, known_scope_ids=set(), file_hashes={}, symbol_owning_file={}, now=_NOW
    )
    assert len(result) == 1
    assert result[0].status is NoteStatus.expired
    assert result[0].source is RevisionAuthor.system_lifecycle


def test_note_source_hash_mismatch_is_stale_and_shown() -> None:
    note = _note(files=["a.py"], source_hashes={"a.py": "sha256:old"})
    result = detect_note_transitions(
        {"n1": note}, known_scope_ids=set(), file_hashes={"a.py": "sha256:new"},
        symbol_owning_file={}, now=_NOW,
    )
    assert len(result) == 1
    assert result[0].status is NoteStatus.stale
    assert result[0].source_hashes == {"a.py": "sha256:new"}  # snapshot refreshed, not left stale


def test_note_source_hash_stale_transition_is_idempotent() -> None:
    note = _note(files=["a.py"], source_hashes={"a.py": "sha256:old"})
    first = detect_note_transitions(
        {"n1": note}, known_scope_ids=set(), file_hashes={"a.py": "sha256:new"},
        symbol_owning_file={}, now=_NOW,
    )
    assert len(first) == 1
    second = detect_note_transitions(
        {"n1": first[0]}, known_scope_ids=set(), file_hashes={"a.py": "sha256:new"},
        symbol_owning_file={}, now=_NOW,
    )
    assert second == []


def test_note_without_source_hashes_is_persistent_regardless_of_deleted_files() -> None:
    """A note that never had files/symbols snapshotted (source_hashes
    empty) is NOT source-bound, even if it happens to list `files` --
    DATA_MODEL.md §6: "只有設了 source_hashes 才 source-bound"."""
    note = _note(files=["a.py"], source_hashes={})
    result = detect_note_transitions(
        {"n1": note}, known_scope_ids=set(), file_hashes={}, symbol_owning_file={}, now=_NOW
    )
    assert result == []


def test_note_archived_is_not_touched() -> None:
    note = _note(status=NoteStatus.archived, expires_at="2026-01-01T00:00:00Z")
    result = detect_note_transitions(
        {"n1": note}, known_scope_ids=set(), file_hashes={}, symbol_owning_file={}, now=_NOW
    )
    assert result == []


# --------------------------------------------------------------------------
# hashes.py
# --------------------------------------------------------------------------


def test_compute_source_hashes_unions_symbol_owning_files() -> None:
    result = compute_source_hashes(
        files=["a.py"], symbols=["sym-1"],
        file_hashes={"a.py": "sha256:a", "b.py": "sha256:b"},
        symbol_owning_file={"sym-1": "b.py"},
    )
    assert result == {"a.py": "sha256:a", "b.py": "sha256:b"}


def test_compute_scope_membership_hash_changes_with_membership() -> None:
    scope_a = Scope(id="s", name="S", source=ScopeSource.human, members=ScopeMembers(files=["a.py"]))
    scope_b = Scope(
        id="s", name="S", source=ScopeSource.human, members=ScopeMembers(files=["a.py", "b.py"])
    )
    assert compute_scope_membership_hash(scope_a) != compute_scope_membership_hash(scope_b)


def test_compute_scope_membership_hash_is_order_independent() -> None:
    scope_a = Scope(
        id="s", name="S", source=ScopeSource.human, members=ScopeMembers(files=["a.py", "b.py"])
    )
    scope_b = Scope(
        id="s", name="S", source=ScopeSource.human, members=ScopeMembers(files=["b.py", "a.py"])
    )
    assert compute_scope_membership_hash(scope_a) == compute_scope_membership_hash(scope_b)
