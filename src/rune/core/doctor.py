"""`rune doctor`: environment/health diagnostics for a `.rune/` project
(IMPLEMENTATION_PLAN.md Milestone 8).

Every check here is read-only and must never raise -- a broken project is
exactly the situation `doctor` exists to report clearly, not crash on. Each
check is independent: one failing (a corrupt `memory.db`, a missing API key)
must not prevent the others from running and reporting their own result.

Design notes (recorded here rather than only in IMPLEMENTATION_PLAN.md
since they explain *why* this module is structured the way it is):

- The provider/API-key check is deliberately static-only (env var presence,
  config shape) -- it does NOT call `check_semantic_health`'s live network
  probe. `rune update` already does that network probe once per run;
  `doctor` is meant to be cheap and side-effect-free to run at any time,
  and the spec's own wording ("只檢查存在與否") only asks for existence
  checks anyway.
- The "canonical vs cache consistency" check reuses `compute_status()`
  (the same freshness computation `rune status` uses) rather than
  inventing a second one. An earlier draft of the Milestone 8 spec
  mentioned comparing `schema_meta.materialized_from_head`/
  `materialized_from_tree_hash` against `project.json` -- those columns do
  not actually exist in `schema.sql` (checked directly: `schema_meta` only
  ever stores a `schema_version` row). `compute_status()`'s
  `cache_usable`/`working_tree_fresh` pair already answers the same
  "is the cache trustworthy and in sync" question through the mechanism
  that's actually implemented, so this check uses that instead of adding
  new schema fields nothing else needs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum

from rune.core.config import ConfigError, load_config
from rune.core.project import RuneLayout
from rune.core.retrieval.context import build_hard_bootstrap
from rune.core.semantic.provider import api_key_env_var
from rune.core.status import compute_status
from rune.core.storage.canonical import read_json_model, read_jsonl
from rune.core.storage.models import (
    MemoryRevision,
    Note,
    ProjectFile,
    Proposal,
    RuneConfig,
    ScopesFile,
)
from rune.core.storage.schema_versions import UnknownSchemaVersionError
from rune.core.storage.sqlite.materialize import CacheUnusableError, connect_for_read


class CheckLevel(str, Enum):
    ok = "ok"
    warn = "warn"
    error = "error"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    level: CheckLevel
    message: str


@dataclass(frozen=True)
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        """False if any check is `error` -- `warn` alone doesn't fail the
        report (mirrors `rune update`'s own distinction between something
        the user should know about vs. something actually broken)."""
        return all(c.level != CheckLevel.error for c in self.checks)


def run_doctor(layout: RuneLayout) -> DoctorReport:
    checks: list[DoctorCheck] = []
    def safely(name: str, check):
        try:
            return check()
        except Exception as exc:  # noqa: BLE001 - doctor must isolate every check failure.
            checks.append(DoctorCheck(name, CheckLevel.error, f"check failed: {exc}"))
            return None

    git_check = safely("git", _check_git)
    if git_check is not None:
        checks.append(git_check)
    config = safely("config", lambda: _check_config(layout, checks))
    safely("tree_sitter_parsers", lambda: _check_treesitter(checks))
    safely("canonical_schema_versions", lambda: _check_canonical_schema_versions(layout, checks))
    safely("cache", lambda: _check_cache_and_freshness(layout, checks))
    if config is not None:
        safely("semantic_provider", lambda: _check_provider_setup(config, checks))
        safely("bootstrap_governance", lambda: _check_bootstrap_governance(layout, config, checks))
    return DoctorReport(checks=checks)


def _check_git() -> DoctorCheck:
    if shutil.which("git") is None:
        return DoctorCheck("git", CheckLevel.error, "git executable not found on PATH.")
    try:
        result = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, encoding="utf-8", check=False
        )
    except OSError as exc:
        return DoctorCheck("git", CheckLevel.error, f"git is on PATH but failed to run: {exc}")
    if result.returncode != 0:
        return DoctorCheck("git", CheckLevel.error, f"`git --version` exited {result.returncode}.")
    return DoctorCheck("git", CheckLevel.ok, result.stdout.strip())


def _check_config(layout: RuneLayout, checks: list[DoctorCheck]) -> RuneConfig | None:
    try:
        config = load_config(layout.config_path)
    except ConfigError as exc:
        checks.append(DoctorCheck("config", CheckLevel.error, str(exc)))
        return None
    checks.append(DoctorCheck("config", CheckLevel.ok, f"{layout.config_path} is valid."))
    return config


def _check_treesitter(checks: list[DoctorCheck]) -> None:
    missing: list[str] = []
    for module_name in ("tree_sitter_python", "tree_sitter_javascript", "tree_sitter_typescript"):
        try:
            __import__(module_name)
        except ImportError:
            missing.append(module_name)
    if missing:
        checks.append(
            DoctorCheck(
                "tree_sitter_parsers", CheckLevel.error,
                f"missing tree-sitter grammar package(s): {', '.join(missing)}.",
            )
        )
    else:
        checks.append(DoctorCheck("tree_sitter_parsers", CheckLevel.ok, "python/javascript/typescript grammars importable."))


# (path, model class, human label) -- Proposal/Note/MemoryRevision are all
# read via `read_jsonl`, ProjectFile/ScopesFile via `read_json_model`; both
# already raise `UnknownSchemaVersionError` for a future schema_version, so
# this check only needs to attempt a real read and catch that one error.
def _check_canonical_schema_versions(layout: RuneLayout, checks: list[DoctorCheck]) -> None:
    problems: list[str] = []
    try:
        read_json_model(layout.project_json, ProjectFile)
        read_json_model(layout.scopes_json, ScopesFile)
        read_jsonl(layout.decisions_jsonl, MemoryRevision)
        read_jsonl(layout.constraints_jsonl, MemoryRevision)
        read_jsonl(layout.notes_jsonl, Note)
        read_jsonl(layout.proposals_jsonl, Proposal)
    except UnknownSchemaVersionError as exc:
        problems.append(str(exc))
    if problems:
        checks.append(DoctorCheck("canonical_schema_versions", CheckLevel.error, "; ".join(problems)))
    else:
        checks.append(DoctorCheck("canonical_schema_versions", CheckLevel.ok, "all canonical files are at a known schema_version."))


def _check_cache_and_freshness(layout: RuneLayout, checks: list[DoctorCheck]) -> None:
    if not layout.memory_db.exists():
        checks.append(
            DoctorCheck("cache", CheckLevel.warn, f"{layout.memory_db} does not exist yet -- run `rune update`.")
        )
        return
    try:
        conn = connect_for_read(layout)
        conn.close()
    except CacheUnusableError as exc:
        checks.append(
            DoctorCheck("cache", CheckLevel.error, f"{layout.memory_db} is not usable: {exc}")
        )
        return
    checks.append(DoctorCheck("cache", CheckLevel.ok, f"{layout.memory_db} opens and is at the current schema version."))

    status = compute_status(layout)
    if status is not None and not status.working_tree_fresh:
        checks.append(
            DoctorCheck(
                "working_tree_freshness", CheckLevel.warn,
                f"working tree has changed since the last `rune update` "
                f"({status.files_modified} modified, {status.files_added} added, "
                f"{status.files_deleted} deleted) -- cache may be stale.",
            )
        )
    else:
        checks.append(DoctorCheck("working_tree_freshness", CheckLevel.ok, "cache matches the current working tree."))


def _check_provider_setup(config: RuneConfig, checks: list[DoctorCheck]) -> None:
    """Existence-only check (no network call) -- see module docstring."""
    semantic = config.semantic
    if not semantic.enabled:
        checks.append(DoctorCheck("semantic_provider", CheckLevel.ok, "semantic.enabled is false; skipped."))
        return
    if not semantic.model:
        checks.append(
            DoctorCheck("semantic_provider", CheckLevel.error, "semantic.enabled is true but semantic.model is empty.")
        )
        return
    env_var = api_key_env_var(semantic.provider)
    if env_var is None:
        checks.append(
            DoctorCheck("semantic_provider", CheckLevel.error, f"unknown semantic.provider {semantic.provider!r}.")
        )
        return
    if not os.environ.get(env_var):
        checks.append(
            DoctorCheck("semantic_provider", CheckLevel.error, f"{env_var} is not set in the environment.")
        )
        return
    checks.append(DoctorCheck("semantic_provider", CheckLevel.ok, f"semantic.model set, {env_var} is present."))


def _check_bootstrap_governance(layout: RuneLayout, config: RuneConfig, checks: list[DoctorCheck]) -> None:
    if not layout.memory_db.exists():
        return
    try:
        hard = build_hard_bootstrap(layout)
    except CacheUnusableError:
        return

    must_count = len(hard.constraints)
    threshold = config.bootstrap.must_count_warn_threshold
    if must_count > threshold:
        approx_tokens = hard.estimated_tokens
        checks.append(
            DoctorCheck(
                "global_must_count", CheckLevel.warn,
                f"{must_count} global MUST constraints (over the {threshold} warn threshold), "
                f"estimated ~{approx_tokens} tokens -- consider consolidating or moving "
                f"machine-checkable rules to a linter.",
            )
        )
    else:
        checks.append(DoctorCheck("global_must_count", CheckLevel.ok, f"{must_count} global MUST constraints."))

    if hard.overflow:
        checks.append(
            DoctorCheck(
                "hard_bootstrap_budget", CheckLevel.warn,
                f"hard bootstrap is ~{hard.estimated_tokens} tokens, over the "
                f"{hard.budget_tokens}-token budget (no rule was dropped) -- raise "
                f"bootstrap.hard_budget_tokens or trim the global MUST set.",
            )
        )
    else:
        checks.append(DoctorCheck("hard_bootstrap_budget", CheckLevel.ok, f"~{hard.estimated_tokens} / {hard.budget_tokens} tokens."))

    _check_non_persistent_global_constraints(layout, checks)


def _check_non_persistent_global_constraints(layout: RuneLayout, checks: list[DoctorCheck]) -> None:
    """ARCHITECTURE.md §7.2: a constraint with `scopes==[]` (global) whose
    `persistence_mode` isn't `persistent` is semantically suspicious --
    `scope_bound`/`source_bound` staleness snapshots have nothing to
    compare against for a rule that isn't attached to any scope/file, and
    `temporary` on a global rule usually means the author meant to scope it
    rather than let it expire project-wide. Flagged, not blocked or
    auto-corrected -- ARCHITECTURE.md leaves the actual authoring choice to
    the human/agent that proposes it.
    """
    conn = connect_for_read(layout)
    try:
        rows = conn.execute(
            "SELECT r.record_id, v.persistence_mode FROM constraint_records r "
            "JOIN constraint_revisions v "
            "  ON v.record_id = r.record_id AND v.revision = r.current_revision "
            "WHERE v.persistence_mode != 'persistent' "
            "  AND v.status IN ('active', 'review_required', 'stale') "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM constraint_scopes cs "
            "    WHERE cs.record_id = r.record_id AND cs.revision = r.current_revision"
            "  )"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return
    ids = sorted(f"{row['record_id']} ({row['persistence_mode']})" for row in rows)
    checks.append(
        DoctorCheck(
            "global_constraint_persistence_mode", CheckLevel.warn,
            f"{len(ids)} global (scope-less) constraint(s) use a non-persistent "
            f"persistence_mode, which is unusual for a global rule: {', '.join(ids)}.",
        )
    )


__all__ = ["CheckLevel", "DoctorCheck", "DoctorReport", "run_doctor"]
