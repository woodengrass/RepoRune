"""MCP server exposing Rune's read/write surface to an MCP-speaking agent
(IMPLEMENTATION_PLAN.md Milestone 8).

Every tool here is a thin wrapper over an existing `rune.core` function --
the same "no business logic in the protocol layer" rule ARCHITECTURE.md §5
already applies to `rune.cli`, extended to this second protocol frontend.
Unlike the OpenCode adapter (`adapters/opencode/`, a separate TypeScript
process that must shell out to the `rune` CLI's `--json` output), this
server runs in-process and imports `rune.core` directly -- there is no
"only through --json" boundary here, so tools call core functions, not the
CLI.

## Tool list (confirmed with the user; supersedes IMPLEMENTATION_PLAN.md's
original "十個 tool" reference to a spec document that no longer exists in
this repo)

Read-only: `rune_status`, `rune_doctor`, `rune_search`, `rune_symbol_search`,
`rune_scope_for`, `rune_scope_read`, `rune_bootstrap`, `rune_related_context`,
`rune_check`, `rune_changes`, `rune_decision_get`, `rune_constraint_get`,
`rune_note_get`.

Write (never bypasses human approval for governance records):
`rune_decision_propose`/`rune_constraint_propose` only ever create a
*pending* proposal -- approval still requires a human running `rune
proposal approve` (or the equivalent host UI); `rune_note_add`/
`rune_note_update` write directly, same as the CLI/OpenCode adapter
already do, since Notes were deliberately designed without an approval
gate (DATA_MODEL.md §2.6).

Deliberately NOT exposed: `rune update` (network/LLM cost + canonical
writes beyond what an MCP tool call should trigger silently), scope
CRUD/`scope suggest` (human-confirmed by design, ARCHITECTURE.md §4.4),
`proposal approve`/`reject`/`edit` (approval is specifically the human
checkpoint governance records exist for -- an agent proposing and then
also approving its own proposal would defeat the purpose).

## Error handling

Every known domain/environment error (bad repo path, corrupt cache, record
not found, validation failure, ...) is converted to `ValueError` by the
`@_tool` decorator below rather than propagating its original exception
type -- one place, applied to every tool, instead of a per-tool try/except
that a new tool could easily forget (a review pass caught exactly that
gap: `CacheUnusableError` from a corrupt `memory.db` was uncaught in every
read tool except the ones that happened to call a function raising a
*different* exception first). The MCP SDK turns a raised exception into a
tool-error response either way; this only makes the message consistent
and readable instead of a raw traceback for the errors this project has
names for.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from rune.core.config import ConfigError, load_config
from rune.core.doctor import run_doctor
from rune.core.memory.notes import NoteNotFoundError, NoteValidationError
from rune.core.memory.notes import note_add as core_note_add
from rune.core.memory.notes import note_update as core_note_update
from rune.core.memory.proposals import ProposalValidationError
from rune.core.memory.proposals import propose as core_propose
from rune.core.memory.records import (
    RecordNotFoundError,
    get_constraint,
    get_decision,
    get_note,
)
from rune.core.project import NotAGitRepoError, RuneLayout, find_repo_root
from rune.core.retrieval.changes import changes as core_changes
from rune.core.retrieval.check import check as core_check
from rune.core.retrieval.context import build_hard_bootstrap, build_soft_bootstrap
from rune.core.retrieval.related_context import RelatedContextValidationError
from rune.core.retrieval.related_context import related_context as core_related_context
from rune.core.retrieval.scope_for import resolve_scope_for
from rune.core.retrieval.scope_read import ScopeNotFoundError
from rune.core.retrieval.scope_read import scope_read as core_scope_read
from rune.core.retrieval.search import search as core_search
from rune.core.retrieval.symbol_search import symbol_search as core_symbol_search
from rune.core.status import compute_status
from rune.core.storage.models import (
    NoteCategory,
    NoteStatus,
    PersistenceMode,
    RecordType,
    Severity,
)
from rune.core.storage.sqlite.materialize import CacheUnusableError

mcp = MCPServer(
    name="rune",
    instructions=(
        "RepoRune (rune) -- repository intelligence for this project: deterministic code "
        "index, persistent Decision/Constraint governance records, expiring Notes, and "
        "LLM-generated scope summaries. Prefer rune_related_context for 'what do I need to "
        "know before touching X' -- it composes scope/search/symbol lookups in one call. "
        "rune_decision_propose/rune_constraint_propose only create a pending proposal; a "
        "human must still approve it. rune_note_add/rune_note_update write immediately."
    ),
)


class _MissingLayoutError(Exception):
    pass


# Every error type a tool body (or a `core` function it calls) can raise
# for a reason a caller should see as a clear message, not a traceback.
# `_tool` catches all of these, once, for every registered tool.
_KNOWN_ERRORS = (
    NotAGitRepoError,
    _MissingLayoutError,
    CacheUnusableError,
    ConfigError,
    ScopeNotFoundError,
    RecordNotFoundError,
    RelatedContextValidationError,
    ProposalValidationError,
    NoteValidationError,
    NoteNotFoundError,
    ValueError,
)


def _tool(fn):
    """Registers `fn` as an MCP tool (`mcp.tool()`) wrapped so any
    `_KNOWN_ERRORS` it raises becomes a `ValueError` with the same
    message. `functools.wraps` preserves `fn`'s name/docstring/annotations
    (via `__wrapped__`) so `mcp.tool()`'s signature/schema introspection
    on the wrapper still sees `fn`'s real parameter list.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except _KNOWN_ERRORS as exc:
            raise ValueError(str(exc)) from exc

    return mcp.tool()(wrapper)


def _layout(path: str | None) -> RuneLayout:
    repo_root = find_repo_root(Path(path) if path else Path.cwd())
    layout = RuneLayout(repo_root=repo_root)
    if not layout.rune_dir.exists():
        raise _MissingLayoutError(f"{layout.rune_dir} does not exist -- run `rune init` first.")
    return layout


def _validate_actor(value: str, field_name: str) -> None:
    if value not in ("agent", "human"):
        raise ValueError(f"{field_name} must be 'agent' or 'human', got {value!r}")


def _constraint_dict(c: Any) -> dict[str, Any]:
    return {"record_id": c.record_id, "severity": c.severity, "content": c.content,
            "status": c.status, "warning": c.warning}


def _note_dict(n: Any) -> dict[str, Any]:
    return {"id": n.id, "category": n.category, "content": n.content, "status": n.status, "warning": n.warning}


def _decision_dict(d: Any) -> dict[str, Any]:
    return {"record_id": d.record_id, "content": d.content, "status": d.status, "warning": d.warning}


def _symbol_dict(s: Any) -> dict[str, Any]:
    return {"symbol_id": s.symbol_id, "file": s.file, "name": s.name, "qualified_name": s.qualified_name,
            "kind": s.kind, "signature": s.signature, "start_line": s.start_line, "end_line": s.end_line}


def _revision_dict(rev: Any) -> dict[str, Any]:
    return rev.model_dump(mode="json")


@_tool
def rune_status(path: str | None = None) -> dict[str, Any]:
    """Project identity, deterministic code index size, and working-tree
    freshness (has anything changed since the last `rune update`)."""
    layout = _layout(path)
    status = compute_status(layout)
    if status is None:
        raise _MissingLayoutError(f"{layout.project_json} is missing or unreadable.")
    return {
        "project_id": status.project_id,
        "name": status.name,
        "last_indexed_head": status.last_indexed_head,
        "last_indexed_tree_hash": status.last_indexed_tree_hash,
        "last_indexed_at": status.last_indexed_at,
        "cache_exists": status.cache_exists,
        "cache_usable": status.cache_usable,
        "files_indexed": status.files_indexed,
        "symbols_indexed": status.symbols_indexed,
        "working_tree_fresh": status.working_tree_fresh,
        "files_modified": status.files_modified,
        "files_added": status.files_added,
        "files_deleted": status.files_deleted,
    }


@_tool
def rune_doctor(path: str | None = None) -> dict[str, Any]:
    """Read-only health diagnostics: config validity, canonical schema
    versions, cache health, git/tree-sitter availability, semantic
    provider setup (existence-only, no network call), and Global MUST
    governance warnings."""
    layout = _layout(path)
    report = run_doctor(layout)
    return {
        "healthy": report.healthy,
        "checks": [{"name": c.name, "level": c.level.value, "message": c.message} for c in report.checks],
    }


@_tool
def rune_search(
    query: str,
    kinds: list[str] | None = None,
    include_history: bool = False,
    limit: int = 50,
    path: str | None = None,
) -> dict[str, Any]:
    """Ranked full-text search across scope summaries, Decisions,
    Constraints, and Notes (ARCHITECTURE.md §4.8's eight-layer priority
    order). `kinds`, if given, restricts to a subset of "decision",
    "constraint", "note", "semantic". `include_history=True` also returns
    non-current/superseded revisions -- default is current+visible only."""
    layout = _layout(path)
    results = core_search(
        layout, query, history=include_history, limit=limit,
        kinds=frozenset(kinds) if kinds else None,
    )
    return {
        "results": [
            {"kind": r.kind, "rank": r.rank, "id": r.id, "text": r.text, "status": r.status,
             "warning": r.warning, "revision": r.revision}
            for r in results
        ]
    }


@_tool
def rune_symbol_search(
    query: str | None = None,
    name: str | None = None,
    qualified_name: str | None = None,
    kind: str | None = None,
    file_path: str | None = None,
    limit: int = 50,
    path: str | None = None,
) -> dict[str, Any]:
    """Structured symbol lookup by name/qualified_name/kind/owning file,
    optionally combined with a full-text `query` over qualified_name +
    signature. All given filters must match (AND)."""
    layout = _layout(path)
    results = core_symbol_search(
        layout, query=query, name=name, qualified_name=qualified_name,
        kind=kind, path=file_path, limit=limit,
    )
    return {"results": [_symbol_dict(s) for s in results]}


@_tool
def rune_scope_for(file_path: str | None = None, symbol: str | None = None, path: str | None = None) -> dict[str, Any]:
    """Which scope(s) a file (or, via `symbol`, the file that symbol is
    defined in) belongs to, each with its semantic summary and its
    current+visible MUST/SHOULD constraints and Notes -- exactly one of
    `file_path`/`symbol` is required."""
    if not file_path and not symbol:
        raise ValueError("one of file_path or symbol is required")
    layout = _layout(path)
    resolved_path, results = resolve_scope_for(layout, path=file_path, symbol=symbol)
    return {
        "path": resolved_path,
        "scopes": [
            {
                "scope_id": s.scope_id, "name": s.name, "description": s.description,
                "summary": s.summary, "summary_status": s.summary_status,
                "constraints": [_constraint_dict(c) for c in s.constraints],
                "notes": [_note_dict(n) for n in s.notes],
            }
            for s in results
        ],
    }


@_tool
def rune_scope_read(scope_id: str, path: str | None = None) -> dict[str, Any]:
    """The full agent-facing view of one scope by id: metadata, member
    files/symbols, its semantic summary, and its current+visible
    Decisions/Constraints/Notes."""
    layout = _layout(path)
    result = core_scope_read(layout, scope_id)
    return {
        "scope_id": result.scope_id, "name": result.name, "description": result.description,
        "locked": result.locked, "source": result.source, "files": result.files, "symbols": result.symbols,
        "summary": result.summary, "summary_status": result.summary_status,
        "decisions": [_decision_dict(d) for d in result.decisions],
        "constraints": [_constraint_dict(c) for c in result.constraints],
        "notes": [_note_dict(n) for n in result.notes],
    }


@_tool
def rune_bootstrap(mode: str, path: str | None = None) -> dict[str, Any]:
    """Structured agent-injection payload (ARCHITECTURE.md §7.3): mode
    "hard" returns current+visible global MUST Constraints plus
    critical=true global Decisions; mode "soft" returns a project
    overview, scope summaries, memory freshness, and the remaining
    non-critical global Decisions."""
    if mode not in ("hard", "soft"):
        raise ValueError("mode must be 'hard' or 'soft'")
    layout = _layout(path)
    if mode == "hard":
        hard = build_hard_bootstrap(layout)
        return {
            "mode": "hard",
            "constraints": [
                {"record_id": c.record_id, "severity": c.severity, "content": c.content,
                 "source_document": c.source_document, "source_section": c.source_section}
                for c in hard.constraints
            ],
            "decisions": [{"record_id": d.record_id, "content": d.content} for d in hard.decisions],
            "estimated_tokens": hard.estimated_tokens, "budget_tokens": hard.budget_tokens, "overflow": hard.overflow,
        }
    soft = build_soft_bootstrap(layout)
    return {
        "mode": "soft",
        "project_name": soft.project_name, "working_tree_fresh": soft.working_tree_fresh,
        "files_indexed": soft.files_indexed, "symbols_indexed": soft.symbols_indexed,
        "scopes": [
            {"scope_id": s.scope_id, "name": s.name, "description": s.description, "summary": s.summary}
            for s in soft.scopes
        ],
        "decisions": [{"record_id": d.record_id, "content": d.content} for d in soft.decisions],
        "estimated_tokens": soft.estimated_tokens, "budget_tokens": soft.budget_tokens, "overflow": soft.overflow,
    }


@_tool
def rune_related_context(
    file_path: str | None = None,
    symbol: str | None = None,
    query: str | None = None,
    include: list[str] | None = None,
    max_items: int = 20,
    path: str | None = None,
) -> dict[str, Any]:
    """One call for the most relevant Rune context for a task: scopes,
    constraints, decisions, notes, semantic summaries, and symbols related
    to a path, a symbol, and/or a free-text query -- the primary
    context-retrieval tool for a pull-style agent, avoiding a manual
    scope_for -> search -> search -> search chain. At least one of
    `file_path`/`symbol`/`query` is required. `include` restricts to a
    subset of scopes/constraints/decisions/notes/semantic/symbols;
    `max_items` caps each bucket independently."""
    layout = _layout(path)
    result = core_related_context(
        layout, path=file_path, symbol=symbol, query=query,
        include=set(include) if include else None, max_items=max_items,
    )
    return {
        "scopes": [
            {"scope_id": s.scope_id, "name": s.name, "description": s.description,
             "summary": s.summary, "summary_status": s.summary_status}
            for s in result.scopes
        ],
        "constraints": [_constraint_dict(c) for c in result.constraints],
        "decisions": [_decision_dict(d) for d in result.decisions],
        "notes": [_note_dict(n) for n in result.notes],
        "semantic": [
            {"scope_id": s.scope_id, "name": s.name, "summary": s.summary, "summary_status": s.summary_status}
            for s in result.semantic
        ],
        "symbols": [_symbol_dict(s) for s in result.symbols],
    }


@_tool
def rune_check(path: str | None = None) -> dict[str, Any]:
    """Working-tree changes -> affected scopes -> relevant Constraints
    (MUST/SHOULD/global). No model calls."""
    layout = _layout(path)
    result = core_check(layout)
    return {
        "changed_files": result.changed_files,
        "affected_scope_ids": result.affected_scope_ids,
        "constraints": [
            {"record_id": c.record_id, "content": c.content, "severity": c.severity,
             "status": c.status, "scope_ids": c.scope_ids}
            for c in result.constraints
        ],
    }


@_tool
def rune_changes(path: str | None = None) -> dict[str, Any]:
    """Working-tree changed files and affected scopes (same diff `rune_check`
    uses), plus every scope currently marked `possibly_stale`/`stale`
    project-wide -- the backlog of semantic summaries that need `rune
    update` to regenerate, not just ones touched by the current diff."""
    layout = _layout(path)
    result = core_changes(layout)
    return {
        "changed_files": result.changed_files,
        "affected_scope_ids": result.affected_scope_ids,
        "stale_semantic": [{"scope_id": s.scope_id, "status": s.status} for s in result.stale_semantic],
    }


@_tool
def rune_decision_get(record_id: str, include_history: bool = False, path: str | None = None) -> dict[str, Any]:
    """Current revision of one Decision by record_id (does not fold in
    non-current revisions unless `include_history=True`)."""
    layout = _layout(path)
    current, history = get_decision(layout, record_id, include_history=include_history)
    out = {"current": _revision_dict(current)}
    if history is not None:
        out["history"] = [_revision_dict(r) for r in history]
    return out


@_tool
def rune_constraint_get(record_id: str, include_history: bool = False, path: str | None = None) -> dict[str, Any]:
    """Current revision of one Constraint by record_id, subject to the
    same current/visible semantics as every other Rune read path."""
    layout = _layout(path)
    current, history = get_constraint(layout, record_id, include_history=include_history)
    out = {"current": _revision_dict(current)}
    if history is not None:
        out["history"] = [_revision_dict(r) for r in history]
    return out


@_tool
def rune_note_get(note_id: str, include_history: bool = False, path: str | None = None) -> dict[str, Any]:
    """Current revision of one Note by id."""
    layout = _layout(path)
    current, history = get_note(layout, note_id, include_history=include_history)
    out = {"current": _revision_dict(current)}
    if history is not None:
        out["history"] = [_revision_dict(r) for r in history]
    return out


@_tool
def rune_decision_propose(
    record_id: str,
    content: str,
    rationale: str = "",
    scopes: list[str] | None = None,
    files: list[str] | None = None,
    symbols: list[str] | None = None,
    critical: bool = False,
    source_document: str | None = None,
    source_section: str | None = None,
    created_by: str = "agent",
    path: str | None = None,
) -> dict[str, Any]:
    """Propose a new Decision. Sits pending until a human runs `rune
    proposal approve` -- this tool never approves its own proposal."""
    _validate_actor(created_by, "created_by")
    layout = _layout(path)
    config = load_config(layout.config_path)
    proposal = core_propose(
        layout, type=RecordType.decision, record_id=record_id, content=content,
        rationale=rationale, scopes=scopes or [], files=files or [], symbols=symbols or [],
        critical=critical, source_document=source_document, source_section=source_section,
        created_by=created_by, redact_secrets=config.security.redact_secrets,
    )
    return {"proposal_id": proposal.proposal_id, "record_id": record_id, "status": "pending"}


@_tool
def rune_constraint_propose(
    record_id: str,
    content: str,
    severity: str,
    persistence_mode: str,
    rationale: str = "",
    scopes: list[str] | None = None,
    files: list[str] | None = None,
    symbols: list[str] | None = None,
    expires_at: str | None = None,
    source_document: str | None = None,
    source_section: str | None = None,
    machine_check_hint: str | None = None,
    created_by: str = "agent",
    path: str | None = None,
) -> dict[str, Any]:
    """Propose a new Constraint. Sits pending until a human runs `rune
    proposal approve` -- a Global MUST Constraint must never be approved
    by the agent that proposed it; `source_hashes`/`scope_hashes` are
    computed automatically at approval time, never supplied here."""
    _validate_actor(created_by, "created_by")
    layout = _layout(path)
    config = load_config(layout.config_path)
    proposal = core_propose(
        layout, type=RecordType.constraint, record_id=record_id, content=content,
        rationale=rationale, scopes=scopes or [], files=files or [], symbols=symbols or [],
        severity=Severity(severity), persistence_mode=PersistenceMode(persistence_mode),
        expires_at=expires_at, source_document=source_document, source_section=source_section,
        machine_check_hint=machine_check_hint, created_by=created_by,
        redact_secrets=config.security.redact_secrets,
    )
    return {"proposal_id": proposal.proposal_id, "record_id": record_id, "status": "pending"}


@_tool
def rune_note_add(
    category: str,
    content: str,
    why_persist: str,
    scopes: list[str] | None = None,
    files: list[str] | None = None,
    symbols: list[str] | None = None,
    importance: float = 0.5,
    confidence: float = 0.5,
    evidence: list[str] | None = None,
    expires_at: str | None = None,
    source: str = "agent",
    path: str | None = None,
) -> dict[str, Any]:
    """Add a new Note. No approval gate -- writes immediately, through the
    same redaction/schema/lifecycle rules the CLI/OpenCode adapter use."""
    _validate_actor(source, "source")
    layout = _layout(path)
    config = load_config(layout.config_path)
    note = core_note_add(
        layout, category=NoteCategory(category), content=content, why_persist=why_persist,
        scopes=scopes or [], files=files or [], symbols=symbols or [],
        importance=importance, confidence=confidence, evidence=evidence or [],
        expires_at=expires_at, source=source, redact_secrets=config.security.redact_secrets,
    )
    return {"id": note.id, "category": note.category.value, "revision": note.revision}


@_tool
def rune_note_update(
    note_id: str,
    content: str | None = None,
    why_persist: str | None = None,
    status: str | None = None,
    evidence: list[str] | None = None,
    scopes: list[str] | None = None,
    files: list[str] | None = None,
    symbols: list[str] | None = None,
    importance: float | None = None,
    confidence: float | None = None,
    expires_at: str | None = None,
    recompute_source_hashes: bool = False,
    source: str = "agent",
    path: str | None = None,
) -> dict[str, Any]:
    """Append a new revision to an existing Note -- verify/archive/re-scope
    it, or clear a field by passing an empty list for evidence/scopes/
    files/symbols. Fields not given are carried forward unchanged."""
    _validate_actor(source, "source")
    layout = _layout(path)
    config = load_config(layout.config_path)
    updated = core_note_update(
        layout, note_id, content=content, why_persist=why_persist,
        status=NoteStatus(status) if status else None,
        evidence=evidence, scopes=scopes, files=files, symbols=symbols,
        importance=importance, confidence=confidence, expires_at=expires_at,
        source=source, redact_secrets=config.security.redact_secrets,
        recompute_source_hashes=recompute_source_hashes,
    )
    return {"id": updated.id, "revision": updated.revision, "status": updated.status.value}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
