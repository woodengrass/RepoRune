"""`rune check`: working-tree changes -> affected scopes -> relevant
constraints (IMPLEMENTATION_PLAN.md Milestone 6: "git diff -> 受影響
files/scopes -> 相關 constraint 清單"). No model calls -- this is a pure
SQLite lookup, same "decisive, cheap, always available" tier as `rune
status`.

"Changed" is computed the same way `rune status` already does (scan the
working tree, diff against the `files` table's last-indexed content_hash)
rather than shelling out to `git diff` a second time -- both describe the
same thing (what's different since the last `rune update`) and reusing
the existing diff machinery keeps this consistent with `status`'s notion
of freshness instead of introducing a second, subtly different one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rune.core.config import load_config
from rune.core.index.scanner import diff_against_previous, scan_files_with_issues
from rune.core.project import RuneLayout
from rune.core.storage.sqlite.materialize import connect_for_read

_VISIBLE_CONSTRAINT_STATUSES = {"active", "review_required", "stale"}
_STATUS_PRIORITY = {"active": 0, "review_required": 1, "stale": 2}


@dataclass(frozen=True)
class RelevantConstraint:
    record_id: str
    content: str
    severity: str
    status: str
    scope_ids: list[str]
    # [] for a constraint with no scope at all -- either genuinely global
    # (DATA_MODEL.md §3: scopes==[] is exactly what "global" means) or a
    # constraint bound directly to files/symbols without ever being
    # attached to a scope; either way there's no scope to list.


@dataclass(frozen=True)
class CheckResult:
    changed_files: list[str] = field(default_factory=list)
    affected_scope_ids: list[str] = field(default_factory=list)
    constraints: list[RelevantConstraint] = field(default_factory=list)


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def check(layout: RuneLayout) -> CheckResult:
    if not layout.memory_db.exists():
        return CheckResult()

    config = load_config(layout.config_path)
    scan_result = scan_files_with_issues(layout.repo_root, config.index)
    scanned = scan_result.files

    conn = connect_for_read(layout)
    try:
        previous_hashes = dict(conn.execute("SELECT path, content_hash FROM files"))
        changeset = diff_against_previous(scanned, previous_hashes)
        changed_files = sorted(
            {f.path for f in changeset.added}
            | {f.path for f in changeset.modified}
            | (set(changeset.deleted_paths) - set(scan_result.unreadable_paths))
        )
        if not changed_files:
            return CheckResult()

        affected_scope_ids: set[str] = set()
        for path in changed_files:
            rows = conn.execute(
                "SELECT scope_id FROM scope_files WHERE file = ? "
                "UNION "
                "SELECT ss.scope_id FROM scope_symbols ss "
                "JOIN symbols sym ON sym.symbol_id = ss.symbol_id "
                "WHERE sym.file = ?",
                (path, path),
            ).fetchall()
            affected_scope_ids.update(row["scope_id"] for row in rows)

        relevant_ids: set[str] = set()

        # Path 1: scope-bound -- a constraint attached to a scope that a
        # changed file belongs to.
        for scope_id in sorted(affected_scope_ids):
            rows = conn.execute(
                "SELECT r.record_id FROM constraint_scopes cs "
                "JOIN constraint_records r ON r.record_id = cs.record_id "
                "WHERE cs.scope_id = ? AND cs.revision = r.current_revision",
                (scope_id,),
            ).fetchall()
            relevant_ids.update(row["record_id"] for row in rows)

        # Path 2: a constraint bound directly to one of the changed files
        # (e.g. persistence_mode=source_bound with `files=[...]` but no
        # `scopes`) -- this used to be invisible to `rune check` entirely,
        # since only the scope-membership path was ever queried, even
        # though the file it's literally bound to is exactly what
        # changed. Confirmed by hand: a SHOULD-severity, file-bound,
        # scope-less constraint on a changed file returned zero results
        # before this fix.
        rows = conn.execute(
            f"SELECT DISTINCT r.record_id FROM constraint_files cf "
            f"JOIN constraint_records r ON r.record_id = cf.record_id AND cf.revision = r.current_revision "
            f"WHERE cf.file IN ({_placeholders(len(changed_files))})",
            changed_files,
        ).fetchall()
        relevant_ids.update(row["record_id"] for row in rows)

        # Path 3: a constraint bound directly to a symbol whose owning
        # file changed (covers a deleted/modified symbol -- the symbol's
        # own file necessarily changed for the symbol to have been
        # touched at all, so matching on the owning file's presence in
        # `changed_files` catches both "symbol content changed" and
        # "symbol deleted").
        rows = conn.execute(
            f"SELECT DISTINCT r.record_id FROM constraint_symbols cs "
            f"JOIN constraint_records r ON r.record_id = cs.record_id AND cs.revision = r.current_revision "
            f"JOIN symbols sym ON sym.symbol_id = cs.symbol_id "
            f"WHERE sym.file IN ({_placeholders(len(changed_files))})",
            changed_files,
        ).fetchall()
        relevant_ids.update(row["record_id"] for row in rows)

        # Global MUST constraints (scopes == []) are relevant to any
        # change, not just ones touching a specific scope/file -- confirmed
        # with the user: `rune check` should include the *current* global
        # MUST rules, since "any change" is exactly the condition a global
        # MUST rule is scoped to apply to.
        rows = conn.execute(
            "SELECT r.record_id FROM constraint_records r "
            "JOIN constraint_revisions v "
            "  ON v.record_id = r.record_id AND v.revision = r.current_revision "
            "WHERE v.severity = 'MUST' AND v.status IN ('active', 'review_required', 'stale') "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM constraint_scopes cs "
            "    WHERE cs.record_id = r.record_id AND cs.revision = r.current_revision"
            "  )"
        ).fetchall()
        relevant_ids.update(row["record_id"] for row in rows)

        constraints: list[RelevantConstraint] = []
        for record_id in relevant_ids:
            row = conn.execute(
                "SELECT v.content, v.severity, v.status FROM constraint_records r "
                "JOIN constraint_revisions v "
                "  ON v.record_id = r.record_id AND v.revision = r.current_revision "
                "WHERE r.record_id = ?",
                (record_id,),
            ).fetchone()
            if row["status"] not in _VISIBLE_CONSTRAINT_STATUSES:
                continue
            scope_rows = conn.execute(
                "SELECT cs.scope_id FROM constraint_scopes cs "
                "JOIN constraint_records r ON r.record_id = cs.record_id "
                "WHERE cs.record_id = ? AND cs.revision = r.current_revision",
                (record_id,),
            ).fetchall()
            constraints.append(
                RelevantConstraint(
                    record_id=record_id, content=row["content"], severity=row["severity"],
                    status=row["status"], scope_ids=sorted(s["scope_id"] for s in scope_rows),
                )
            )

        # MUST first, then by status (an active constraint is the normal
        # case; review_required/stale need a human's attention sooner
        # within their own severity tier), then record_id for a stable
        # order -- not the full 8-layer rank from `rune search` (that
        # rank also orders Decisions/Notes, which `check` doesn't surface
        # at all), just this command's own priority notion.
        constraints.sort(
            key=lambda c: (
                c.severity != "MUST",
                _STATUS_PRIORITY.get(c.status, 99),
                c.record_id,
            )
        )

        return CheckResult(
            changed_files=changed_files,
            affected_scope_ids=sorted(affected_scope_ids),
            constraints=constraints,
        )
    finally:
        conn.close()
