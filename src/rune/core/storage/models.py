"""Canonical Pydantic models for rune.

These models mirror DATA_MODEL.md exactly. Do not change field shapes, enum
values, or the current/visible distinction without re-proposing the change
per CLAUDE-facing governance rules in ARCHITECTURE.md / IMPLEMENTATION_PLAN.md.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Shared validators / constrained types
# --------------------------------------------------------------------------


def _validate_iso8601_utc(value: str) -> str:
    """Enforces the convention documented in DATA_MODEL.md §1: timestamps
    are ISO-8601 UTC strings (e.g. "2026-09-06T11:11:00Z"). Rejects naive
    datetimes and non-UTC offsets rather than silently accepting them.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"not a valid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"timestamp must be UTC (zero offset): {value!r}")
    return value


Timestamp = Annotated[str, AfterValidator(_validate_iso8601_utc)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class StrictModel(BaseModel):
    """Base for `.rune/config.toml` models: unknown keys are a config typo,
    not something to silently ignore (a misspelled `[semanic]` table or a
    stray field should fail loudly, not vanish).
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Shared enums
# --------------------------------------------------------------------------


class RevisionAuthor(str, Enum):
    """Who produced a revision. See DATA_MODEL.md §2.5.

    `system_staleness` / `system_lifecycle` values exist so that
    core.memory.staleness can append lifecycle-transition revisions without
    violating this enum (a plain Literal["agent","human"] cannot represent
    system-generated revisions).
    """

    agent = "agent"
    human = "human"
    system_staleness = "system:staleness"
    system_lifecycle = "system:lifecycle"


# --------------------------------------------------------------------------
# Project (project.json)
# --------------------------------------------------------------------------


class ProjectFile(BaseModel):
    schema_version: int = 1
    project_id: str
    name: str
    created_at: Timestamp
    last_indexed_head: str | None = None
    last_indexed_tree_hash: str | None = None
    last_indexed_at: Timestamp | None = None


# --------------------------------------------------------------------------
# Code index primitives (derived; not canonical JSON, only used between
# core.index and core.storage.sqlite.materialize)
# --------------------------------------------------------------------------


class IndexedFileStatus(str, Enum):
    ok = "ok"
    parse_error = "parse_error"


class IndexedFile(BaseModel):
    path: str
    language: str
    content_hash: str
    size: int
    mtime: float
    git_blob_hash: str | None = None
    indexed_at: str
    status: IndexedFileStatus = IndexedFileStatus.ok


class SymbolKind(str, Enum):
    function = "function"
    class_ = "class"
    method = "method"
    interface = "interface"
    type = "type"
    variable = "variable"
    constant = "constant"
    component = "component"


class Symbol(BaseModel):
    symbol_id: str
    file: str
    name: str
    qualified_name: str
    kind: SymbolKind
    signature: str | None = None
    start_line: int
    end_line: int


class EdgeType(str, Enum):
    imports = "imports"
    references = "references"
    calls = "calls"
    extends = "extends"
    implements = "implements"


class Edge(BaseModel):
    source_symbol: str | None = None
    source_file: str
    target_symbol: str | None = None
    target_file: str | None = None
    edge_type: EdgeType
    confidence: Confidence = 1.0


# --------------------------------------------------------------------------
# Scope (scopes.json)
# --------------------------------------------------------------------------


class ScopeSource(str, Enum):
    auto = "auto"
    model = "model"
    human = "human"


class ScopeMembers(BaseModel):
    files: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)


class Scope(BaseModel):
    id: str
    name: str
    description: str = ""
    locked: bool = False
    source: ScopeSource
    members: ScopeMembers = Field(default_factory=ScopeMembers)


class ScopesFile(BaseModel):
    schema_version: int = 1
    scopes: list[Scope] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Semantic summary (semantic.jsonl)
# --------------------------------------------------------------------------


class SemanticStatus(str, Enum):
    fresh = "fresh"
    possibly_stale = "possibly_stale"
    stale = "stale"
    unavailable = "unavailable"
    # This scope has never had a successful generation, and the most recent
    # attempt failed. Distinct from `stale` (which means "there IS real
    # content, it may just be outdated / the last refresh attempt failed"):
    # when `unavailable`, every content field below is an empty placeholder
    # (`purpose=""`, list fields `[]`) and must never be shown to an agent
    # as if it were a real description (DATA_MODEL.md §2.4).
    orphaned = "orphaned"
    # The scope this summary belonged to no longer exists in scopes.json
    # (deleted). Mirrors Decision/Constraint's existing orphaned status
    # (DATA_MODEL.md §6: "引用的整個 scope 消失 -> status=orphaned") for
    # parity rather than inventing separate semantics for ScopeSummary.
    # `core.update` appends this revision (full content copied forward,
    # only status/generated_at changed, same completeness rule as every
    # other system-triggered revision) whenever a scope_id present in
    # semantic.jsonl's current revisions is absent from the current
    # scopes.json. Excluded from SQLite materialization — semantic_objects.
    # scope_id has a real FK to scopes(id), so a vanished scope can never
    # get a row there regardless of status.


class ScopeSummary(BaseModel):
    scope_id: str
    revision: int = Field(ge=1)
    # Per-scope_id monotonically increasing; current = max(revision), same
    # convention as Decision/Constraint/Note (DATA_MODEL.md §1, §3). Added
    # so a failed-generation status can persist across a `rebuild-cache`
    # instead of only living in one run's in-memory state — see §2.4's
    # revision table for what each outcome (success / retry-with-content /
    # first-ever-failure) writes.
    purpose: str
    responsibilities: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    important_symbols: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    data_flow: list[str] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    known_risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    generated_at: Timestamp
    model: str
    source_hash: str
    source_files: dict[str, str] = Field(default_factory=dict)
    status: SemanticStatus = SemanticStatus.fresh
    last_error: str | None = None
    # Only a short, sanitized classification (e.g. "provider_error:
    # TimeoutError", "schema_validation_failed") -- never the raw provider
    # response or exception text/traceback. This file is canonical and may
    # be committed to git, so its sensitivity bar is the same as source
    # code; the unsanitized error goes to the local, gitignored
    # `.rune/logs/semantic.log` instead (ARCHITECTURE.md §4.5).
    schema_version: int = 1


# --------------------------------------------------------------------------
# Decision / Constraint revisions (decisions.jsonl, constraints.jsonl)
# --------------------------------------------------------------------------


class RecordType(str, Enum):
    decision = "decision"
    constraint = "constraint"


class RecordStatus(str, Enum):
    active = "active"
    review_required = "review_required"
    stale = "stale"
    orphaned = "orphaned"
    inactive = "inactive"


class Severity(str, Enum):
    must = "MUST"
    should = "SHOULD"
    info = "INFO"


class PersistenceMode(str, Enum):
    persistent = "persistent"
    scope_bound = "scope_bound"
    source_bound = "source_bound"
    temporary = "temporary"


class MemoryRevision(BaseModel):
    record_id: str
    revision: int = Field(ge=1)
    type: RecordType
    status: RecordStatus
    content: str
    rationale: str = ""
    scopes: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    severity: Severity | None = None
    persistence_mode: PersistenceMode | None = None

    source_hashes: dict[str, str] = Field(default_factory=dict)
    scope_hashes: dict[str, str] = Field(default_factory=dict)
    expires_at: Timestamp | None = None

    # Global Code Standards / Hard Policy Injection support (ARCHITECTURE.md §7).
    critical: bool = False
    # Decision-only convention: marks this Decision as eligible for hard
    # bootstrap (§7.3, §7.5). Not all Decisions should appear in hard
    # bootstrap — only a human-marked few. Constraints do not use this
    # field to express "global"; that is derived from `scopes == []` (§7.2).
    source_document: str | None = None
    source_section: str | None = None
    # Optional traceability to CODE_STANDARDS.md (or another human-readable
    # doc) this MUST/SHOULD rule was extracted from. Purely informational —
    # never affects staleness/current/visible logic.
    machine_check_hint: str | None = None
    # Constraint-only convention: name of an external tool that can verify
    # this rule (e.g. "ruff", "mypy", "pytest"). rune never runs the check
    # itself in V1 — this is only a pointer for humans/tooling (§7.8).

    created_by: RevisionAuthor
    approved_by: str | None = None
    created_at: Timestamp
    schema_version: int = 1


# --------------------------------------------------------------------------
# Pending proposal (proposals.jsonl)
# --------------------------------------------------------------------------


class ProposalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    edited = "edited"


class Proposal(BaseModel):
    proposal_id: str
    revision: int = Field(ge=1)
    type: RecordType
    record_id: str
    payload: MemoryRevision
    status: ProposalStatus = ProposalStatus.pending
    created_by: Literal["agent", "human"]  # propose actions are never system-generated
    created_at: Timestamp
    resolved_at: Timestamp | None = None
    resolved_by: str | None = None
    schema_version: int = 1


# --------------------------------------------------------------------------
# Note (notes.jsonl)
# --------------------------------------------------------------------------


class NoteCategory(str, Enum):
    pitfall = "pitfall"
    observation = "observation"
    workaround = "workaround"
    implementation_detail = "implementation_detail"
    known_issue = "known_issue"
    temporary_context = "temporary_context"
    investigation_result = "investigation_result"


class NoteStatus(str, Enum):
    active = "active"
    stale = "stale"
    expired = "expired"
    orphaned = "orphaned"
    archived = "archived"


class Note(BaseModel):
    id: str
    revision: int = Field(ge=1)
    category: NoteCategory
    content: str
    why_persist: str
    scopes: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    importance: Confidence = 0.5
    confidence: Confidence = 0.5
    source: RevisionAuthor
    evidence: list[str] = Field(default_factory=list)
    created_at: Timestamp
    last_verified_at: Timestamp
    expires_at: Timestamp | None = None
    source_hashes: dict[str, str] = Field(default_factory=dict)
    status: NoteStatus = NoteStatus.active
    schema_version: int = 1


# --------------------------------------------------------------------------
# Config (.rune/config.toml)
# --------------------------------------------------------------------------


class IndexConfig(StrictModel):
    include: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude: list[str] = Field(
        default_factory=lambda: ["**/node_modules/**", "**/.git/**", ".rune/cache/**"]
    )


class SemanticBudget(StrictModel):
    max_input_tokens_per_run: int = 150_000


class ReasoningConfig(StrictModel):
    """Controls "thinking"/reasoning-token spend for reasoning-capable
    models (confirmed against the real OpenRouter API with
    qwen/qwen3.8-flash — `reasoning.enabled=False` drops reasoning_tokens
    to 0, `effort` and `max_tokens` both measurably reduce it). All three
    fields are optional and independent; `provider.py` only includes what's
    actually set in the request payload, leaving the model's own default
    behavior untouched when nothing here is configured.
    """

    enabled: bool = True
    effort: Literal["low", "medium", "high"] | None = None
    max_tokens: int | None = None
    # A hard cap on reasoning tokens specifically (distinct from
    # SemanticConfig.max_tokens, which caps the whole completion —
    # reasoning + content combined).


class SemanticConfig(StrictModel):
    enabled: bool = True
    provider: str = "openrouter"
    model: str = ""
    fallback_model: str | None = None
    budget: SemanticBudget = Field(default_factory=SemanticBudget)
    max_tokens: int = 16000
    # Per-call completion token budget. For a reasoning model, this is
    # shared between the `reasoning` and `content` fields — confirmed by
    # hand that a too-small value can starve `content` entirely (see
    # provider.py's empty-content ProviderError). `reasoning.max_tokens`
    # below caps reasoning specifically, leaving headroom for `content`.
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)


class NotesConfig(StrictModel):
    temporary_context_ttl_days: int = 7
    investigation_result_ttl_days: int = 30


class SecurityConfig(StrictModel):
    redact_secrets: bool = True


class ProposalsConfig(StrictModel):
    commit_to_git: bool = False


class PricingConfig(StrictModel):
    input_per_million: float = 0.0
    output_per_million: float = 0.0


class BootstrapConfig(StrictModel):
    """See ARCHITECTURE.md §7.7. Hard bootstrap must never silently drop a
    MUST constraint when it exceeds `hard_budget_tokens` — the CLI reports
    `overflow=True` instead. `must_count_warn_threshold` drives a `rune
    doctor` consolidation warning, not a hard limit.
    """

    hard_budget_tokens: int = 3000
    soft_budget_tokens: int = 8000
    must_count_warn_threshold: int = 30


class RuneConfig(StrictModel):
    version: int = 1
    index: IndexConfig = Field(default_factory=IndexConfig)
    semantic: SemanticConfig = Field(default_factory=SemanticConfig)
    notes: NotesConfig = Field(default_factory=NotesConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    proposals: ProposalsConfig = Field(default_factory=ProposalsConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    bootstrap: BootstrapConfig = Field(default_factory=BootstrapConfig)
