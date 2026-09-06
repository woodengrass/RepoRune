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

import sqlite3
from dataclasses import dataclass, field

from rune.core.config import load_config
from rune.core.index.scanner import diff_against_previous, scan_files
from rune.core.project import RuneLayout

_VISIBLE_CONSTRAINT_STATUSES = {"active", "review_required", "stale"}


@dataclass(frozen=True)
class RelevantConstraint:
    record_id: str
    content: str
    severity: str
    status: str
    scope_ids: list[str]


@dataclass(frozen=True)
class CheckResult:
    changed_files: list[str] = field(default_factory=list)
    affected_scope_ids: list[str] = field(default_factory=list)
    constraints: list[RelevantConstraint] = field(default_factory=list)


def check(layout: RuneLayout) -> CheckResult:
    if not layout.memory_db.exists():
        return CheckResult()

    config = load_config(layout.config_path)
    scanned = scan_files(layout.repo_root, config.index)

    conn = sqlite3.connect(str(layout.memory_db))
    conn.row_factory = sqlite3.Row
    try:
        previous_hashes = dict(conn.execute("SELECT path, content_hash FROM files"))
        changeset = diff_against_previous(scanned, previous_hashes)
        changed_files = sorted(
            {f.path for f in changeset.added}
            | {f.path for f in changeset.modified}
            | set(changeset.deleted_paths)
        )
        if not changed_files:
            return CheckResult()

        affected_scope_ids: set[str] = set()
        for path in changed_files:
            rows = conn.execute(
                "SELECT scope_id FROM scope_files WHERE file = ?", (path,)
            ).fetchall()
            affected_scope_ids.update(row["scope_id"] for row in rows)

        constraints: list[RelevantConstraint] = []
        seen: dict[str, set[str]] = {}
        for scope_id in sorted(affected_scope_ids):
            rows = conn.execute(
                "SELECT r.record_id, v.status, v.content, v.severity "
                "FROM constraint_scopes cs "
                "JOIN constraint_records r ON r.record_id = cs.record_id "
                "JOIN constraint_revisions v "
                "  ON v.record_id = r.record_id AND v.revision = r.current_revision "
                "WHERE cs.scope_id = ? AND cs.revision = r.current_revision",
                (scope_id,),
            ).fetchall()
            for row in rows:
                if row["status"] not in _VISIBLE_CONSTRAINT_STATUSES:
                    continue
                seen.setdefault(row["record_id"], set()).add(scope_id)

        for record_id, scope_ids in seen.items():
            row = conn.execute(
                "SELECT v.content, v.severity, v.status FROM constraint_records r "
                "JOIN constraint_revisions v "
                "  ON v.record_id = r.record_id AND v.revision = r.current_revision "
                "WHERE r.record_id = ?",
                (record_id,),
            ).fetchone()
            constraints.append(
                RelevantConstraint(
                    record_id=record_id, content=row["content"], severity=row["severity"],
                    status=row["status"], scope_ids=sorted(scope_ids),
                )
            )
        constraints.sort(key=lambda c: (c.severity != "MUST", c.record_id))

        return CheckResult(
            changed_files=changed_files,
            affected_scope_ids=sorted(affected_scope_ids),
            constraints=constraints,
        )
    finally:
        conn.close()
