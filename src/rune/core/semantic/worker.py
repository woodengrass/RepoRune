"""Semantic worker: turns stale/missing scope summaries into validated
`ScopeSummary` revisions (ARCHITECTURE.md §4.5, IMPLEMENTATION_PLAN.md
Milestone 5).

Orchestration only lives here — prompt construction, the fallback-policy
retry ladder, staleness detection, and per-run metrics. Network calls go
through `provider.ModelProvider`; text sanitization through
`redaction.redact_raw_scope_summary`; schema/reference checks through
`validation.validate_and_build_scope_summary`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from rune.core.hashing import working_tree_fingerprint
from rune.core.project import utc_now_iso
from rune.core.semantic.provider import ModelProvider, ProviderError
from rune.core.semantic.redaction import redact_raw_scope_summary
from rune.core.semantic.validation import (
    ValidationOutcome,
    validate_and_build_scope_summary,
)
from rune.core.storage.models import (
    PricingConfig,
    Scope,
    ScopeSummary,
    SemanticStatus,
    Symbol,
)

_SYSTEM_PROMPT = """You are a precise code documentation assistant.

You will be given the files and symbols that make up one logical "scope" \
(a module or feature area) of a software repository. Produce a concise, \
structured summary of that scope.

Respond with ONLY a single JSON object -- no markdown code fences, no \
commentary before or after it -- with exactly these keys:

{
  "purpose": "one or two sentences describing what this scope is for",
  "responsibilities": ["short phrases, one responsibility each"],
  "entry_points": ["file paths or symbol ids that are the main entry points"],
  "important_symbols": ["symbol ids worth knowing about"],
  "dependencies": ["scope ids or external package names this scope depends on"],
  "data_flow": ["short phrases describing how data moves through this scope"],
  "invariants": ["short phrases describing invariants that must hold"],
  "known_risks": ["short phrases describing risks or fragile areas"],
  "open_questions": ["short phrases describing anything unclear from the code alone"]
}

Only use file paths and symbol ids that literally appear in the listing \
below for "entry_points"/"important_symbols" -- never invent ones that \
are not shown. If you are unsure about a field, return an empty list \
(or, for "purpose", your best short guess -- it must never be empty)."""


def compute_source_files(
    scope: Scope, file_hashes: dict[str, str], symbol_owning_file: dict[str, str]
) -> dict[str, str]:
    """DATA_MODEL.md §2.4 invariant: `source_files` = `scope.members.files`
    unioned with the owning file of every `scope.members.symbols` entry,
    even when `members.files` itself is empty.

    Silently drops a member path/symbol that no longer resolves to a
    real, currently-indexed file (deleted file, unparseable symbol) --
    DATA_MODEL.md §2.4 as written says `source_files == {}` is only valid
    for a scope with zero members, which is technically not true here: a
    scope whose *only* member was deleted still ends up with `{}` even
    though `scope.members` itself is non-empty. Deliberate, not patched:
    there is no real content left to hash for a vanished member, so an
    empty `source_files` is an accurate (if edge-case) report of "nothing
    exists to summarize right now" rather than a bug to paper over, and
    it still participates correctly in staleness detection (an empty-hash
    result differs from whatever the last real hash was, so `needs_refresh`
    still fires once this actually happens, and self-heals if the member
    reappears later). Flagged in review; DATA_MODEL.md's wording should be
    read as "no members ever contributed a real file", not literally "the
    members list is empty".
    """
    paths = set(scope.members.files)
    for symbol_id in scope.members.symbols:
        owning_file = symbol_owning_file.get(symbol_id)
        if owning_file is not None:
            paths.add(owning_file)
    return {path: file_hashes[path] for path in paths if path in file_hashes}


def needs_refresh(current: ScopeSummary | None, source_hash: str) -> bool:
    """A scope needs a refresh attempt this run when there's no current
    summary at all, the last attempt never succeeded (`unavailable`), the
    scope was previously orphaned and has reappeared (`orphaned`), or the
    member files have changed since the current summary was produced.

    For every other status (`fresh`/`possibly_stale`/`stale`), retrying is
    driven *only* by the source_hash comparison, not by status alone --
    including `stale`, which does NOT get special-cased into "always retry
    regardless of hash". This matters because a failed refresh's revision
    (DATA_MODEL.md §2.4's failure-revision rule) updates `source_hash` to
    the *current* value even though nothing new was actually generated --
    so a `stale` scope whose content hasn't changed again since that
    failure will keep comparing equal here and will NOT be retried on the
    next `rune update`, only once its source_hash changes again. The
    fallback policy's bounded retry ladder (primary -> repair retry ->
    fallback -> stop) only bounds the cost of *one* refresh attempt; it
    does not by itself guarantee every subsequent `rune update` re-attempts
    a still-failing scope -- that only happens when there's also a new
    hash to react to. `orphaned` is the one status where reappearing (not
    a hash change) is exactly the trigger: a scope can be deleted and
    recreated with byte-identical member files, which would otherwise
    compare hash-equal to the orphaned revision and stay stuck orphaned
    (and thus excluded from retrieval, per Decision/Constraint's existing
    orphaned semantics) forever, even with a provider available and ready
    to regenerate it.
    """
    if current is None:
        return True
    if current.status in (SemanticStatus.unavailable, SemanticStatus.orphaned):
        return True
    return current.source_hash != source_hash


def detect_orphaned_scopes(
    current_summaries: dict[str, ScopeSummary], current_scope_ids: set[str], now: str
) -> list[ScopeSummary]:
    """Scope-deletion counterpart to Decision/Constraint's existing orphan
    handling (DATA_MODEL.md §6): a scope_id with semantic history but no
    longer present in scopes.json gets one more revision, `status=
    orphaned`, full content copied forward from the last current revision
    (the same "complete snapshot, only status/generated_at changed" rule
    every other system-triggered revision in this project follows) —
    never re-orphans an already-orphaned revision. This is a pure
    structural check (no LLM call), so unlike scope refresh it runs
    regardless of whether a provider is configured and regardless of
    `full`/incremental mode.
    """
    orphaned: list[ScopeSummary] = []
    for scope_id, summary in current_summaries.items():
        if scope_id in current_scope_ids or summary.status is SemanticStatus.orphaned:
            continue
        orphaned.append(
            summary.model_copy(
                update={
                    "revision": summary.revision + 1,
                    "status": SemanticStatus.orphaned,
                    "generated_at": now,
                }
            )
        )
    return orphaned


def mark_possibly_stale(
    scopes: list[Scope],
    current_summaries: dict[str, ScopeSummary],
    file_hashes: dict[str, str],
    symbols: list[Symbol],
    now: str,
) -> list[ScopeSummary]:
    """Called instead of `run_semantic_refresh` for a run where no provider
    is usable at all -- `semantic.enabled=false`, a Step 1 config error, or
    a failed Step 2 startup probe (ARCHITECTURE.md §4.5's three-tier health
    check) -- so a scope whose member files changed doesn't keep silently
    reporting `fresh`/`stale` against content that no longer matches.

    Only scopes that already have real content get a new revision here:
    `current is None` or `status=unavailable` means the scope has never
    successfully generated anything, and `needs_refresh` already returns
    True unconditionally for both of those, so it will be attempted again
    the moment a provider becomes available with no extra bookkeeping
    needed in the meantime (DATA_MODEL.md §2.4's possibly_stale row --
    this was confirmed with the user as a deliberate simplification, not
    an oversight). For every scope that DOES have prior content, this
    follows the same "complete snapshot, only status/hash/timestamp
    changed" rule every other system-triggered revision in this project
    uses: full content copied forward from the current revision, `status=
    possibly_stale`, `source_hash`/`source_files` updated to the current
    values, `generated_at` bumped. Fires again (bumping source_hash again)
    if the source changes yet again while still no provider is available,
    same as any other hash-driven revision.
    """
    symbol_owning_file = {symbol.symbol_id: symbol.file for symbol in symbols}
    stale_markers: list[ScopeSummary] = []
    for scope in scopes:
        current = current_summaries.get(scope.id)
        if current is None or current.status is SemanticStatus.unavailable:
            continue
        source_files = compute_source_files(scope, file_hashes, symbol_owning_file)
        source_hash = working_tree_fingerprint(source_files)
        if current.source_hash == source_hash:
            continue
        stale_markers.append(
            current.model_copy(
                update={
                    "revision": current.revision + 1,
                    "status": SemanticStatus.possibly_stale,
                    "source_hash": source_hash,
                    "source_files": source_files,
                    "generated_at": now,
                }
            )
        )
    return stale_markers


_MAX_SNIPPET_LINES = 200
# Per-symbol cap on how much of a member's source gets embedded in the
# prompt. Without this, one unusually large symbol (a generated file, a
# giant class) could burn the entire max_tokens budget on a single
# snippet, starving every other symbol in the scope of any content at
# all — a truncated-but-present snippet is more useful than an empty
# scope. Not currently config-driven; revisit if real usage shows this
# needs tuning per repo.


def _read_symbol_snippet(repo_root: Path, symbol: Symbol) -> str:
    """Best-effort source snippet for one symbol, sliced by its own
    start_line/end_line (already tracked per Symbol, so no re-parsing is
    needed here). Returns "" on any read/decode failure or an out-of-range
    line span rather than raising -- a missing snippet degrades the prompt
    to metadata-only for that one symbol, it must never abort the whole
    refresh (same failure-isolation principle as everywhere else in this
    project).
    """
    try:
        text = (repo_root / symbol.file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    file_lines = text.splitlines()
    start = max(symbol.start_line - 1, 0)
    end = min(symbol.end_line, len(file_lines))
    if start >= end:
        return ""
    snippet_lines = file_lines[start:end]
    truncated = len(snippet_lines) > _MAX_SNIPPET_LINES
    if truncated:
        snippet_lines = snippet_lines[:_MAX_SNIPPET_LINES]
    snippet = "\n".join(snippet_lines)
    if truncated:
        snippet += "\n... (truncated)"
    return snippet


def _build_user_prompt(repo_root: Path, scope: Scope, symbols: list[Symbol]) -> str:
    lines = [f"Scope id: {scope.id}", f"Scope name: {scope.name}"]
    if scope.description:
        lines.append(f"Scope description: {scope.description}")
    lines.append("")
    lines.append("Member files:")
    for path in sorted(scope.members.files):
        lines.append(f"- {path}")
    lines.append("")
    lines.append(
        "Symbols (symbol_id :: qualified_name (kind) signature, followed by its source):"
    )
    for symbol in sorted(symbols, key=lambda s: s.symbol_id):
        signature = f" {symbol.signature}" if symbol.signature else ""
        lines.append(f"- {symbol.symbol_id} :: {symbol.qualified_name} ({symbol.kind.value}){signature}")
        snippet = _read_symbol_snippet(repo_root, symbol)
        if snippet:
            lines.append("```")
            lines.append(snippet)
            lines.append("```")
    return "\n".join(lines)


def _build_repair_prompt(user_prompt: str, reason: str) -> str:
    return (
        f"{user_prompt}\n\n"
        "Your previous response failed validation for this reason: "
        f"{reason}\n"
        "Return a corrected JSON object only, following the schema exactly."
    )


def _extract_json(text: str) -> dict | None:
    """Tolerates a model wrapping its JSON in markdown fences or prose
    despite being asked not to: tries a direct parse first, then falls
    back to slicing between the first `{` and the last `}`.
    """
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _reference_list_len(raw: dict, key: str) -> int:
    """Regression-safe count of a reference-list field for the
    `reference_strip_rate` metric: must agree with `validation.py`'s own
    `_as_str_list` guard (only a real JSON list counts as entries at all).
    Without this, a malformed non-list value from the model -- e.g. the
    model returning a bare string for `entry_points` instead of a list --
    would make `len(...)` count that string's *characters* instead of 0
    entries, since Python's `len()` doesn't care that a string isn't a
    list. Confirmed by hand: a string value inflated `reference_total`
    into the double digits while the validated summary correctly had zero
    entries for that field.
    """
    values = raw.get(key)
    return len(values) if isinstance(values, list) else 0


@dataclass
class ScopeRefreshMetrics:
    scope_id: str
    schema_success: bool = False
    reference_total: int = 0
    reference_stripped: int = 0
    used_fallback: bool = False
    provider_error: bool = False
    cost: float = 0.0
    latency_seconds: float = 0.0
    input_tokens: int = 0


@dataclass
class RefreshOutcome:
    summary: ScopeSummary
    metrics: ScopeRefreshMetrics
    local_log_lines: list[str] = field(default_factory=list)
    # Full, unsanitized diagnostic text for `.rune/logs/semantic.log` —
    # never written to canonical (ARCHITECTURE.md §4.5).


def _sanitized_last_error(reason: str) -> str:
    """Maps a detailed (possibly provider-response-derived) failure reason
    to the short, canonical-safe classification DATA_MODEL.md §2.4 requires
    for `last_error` — just the exception type name for a provider error,
    never the full message/response text. The detailed `reason` itself is
    only ever logged locally (`.rune/logs/semantic.log`), never returned
    from this function.
    """
    if reason.startswith("provider_error:"):
        exception_type = reason[len("provider_error:") :].split(":", 1)[0].strip()
        return f"provider_error:{exception_type}"
    if reason == "response was not valid JSON":
        return "invalid_json_response"
    # Anything else (schema_validation_failed, "purpose is empty", a
    # strip-reject reason) is a validation-side failure, not a provider or
    # parsing failure.
    return "schema_validation_failed"


def _attempt(
    provider: ModelProvider,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    pricing: PricingConfig | None,
) -> tuple[dict | None, str, float, float, int]:
    """One provider call + JSON parse. Returns (parsed_json_or_None,
    detailed_reason_if_failed, cost, latency_seconds, input_tokens).

    `cost` prefers the provider's own reported figure (OpenRouter returns
    one); when the provider doesn't report cost (e.g. plain OpenAI), it
    falls back to `pricing`'s per-million-token estimate
    (DATA_MODEL.md §7 — explicitly an estimate, not a billing-grade
    figure). With no pricing configured either, cost is reported as 0.0
    rather than silently guessing.
    """
    start = time.monotonic()
    try:
        response = provider.complete(
            system_prompt=system_prompt, user_prompt=user_prompt, max_tokens=max_tokens
        )
    except ProviderError as exc:
        latency = time.monotonic() - start
        return None, f"provider_error:{type(exc).__name__}: {exc}", 0.0, latency, 0
    latency = time.monotonic() - start
    parsed = _extract_json(response.content)

    if response.cost is not None:
        cost = response.cost
    elif pricing is not None:
        cost = (
            response.input_tokens / 1_000_000 * pricing.input_per_million
            + response.output_tokens / 1_000_000 * pricing.output_per_million
        )
    else:
        cost = 0.0

    if parsed is None:
        return None, "response was not valid JSON", cost, latency, response.input_tokens
    return parsed, "", cost, latency, response.input_tokens


def refresh_scope_summary(
    *,
    repo_root: Path,
    scope: Scope,
    primary_provider: ModelProvider,
    fallback_provider: ModelProvider | None,
    source_files: dict[str, str],
    known_files: set[str],
    known_symbol_ids: set[str],
    symbols: list[Symbol],
    current: ScopeSummary | None,
    max_tokens: int = 16000,
    pricing: PricingConfig | None = None,
    redact_secrets: bool = True,
) -> RefreshOutcome:
    """Runs the full fallback-policy ladder for one scope (ARCHITECTURE.md
    §4.5): primary -> 1 repair-prompt retry on primary -> fallback model ->
    give up. Always returns a new revision to append (never `None`) —
    success gets real content; failure copies the previous current
    revision forward (or, if there never was one, an empty `unavailable`
    revision) per DATA_MODEL.md §2.4's revision table.
    """
    now = utc_now_iso()
    source_hash = working_tree_fingerprint(source_files)
    next_revision = (current.revision + 1) if current is not None else 1
    metrics = ScopeRefreshMetrics(scope_id=scope.id)
    local_log: list[str] = []
    system_prompt = _SYSTEM_PROMPT
    user_prompt = _build_user_prompt(repo_root, scope, symbols)

    def _validate(raw: dict, *, model: str) -> ValidationOutcome:
        # `model` must be whichever provider actually produced `raw` --
        # not unconditionally `primary_provider.model`. A summary generated
        # by the *fallback* model used to still get tagged with the
        # primary's name, which would make provider/model audit data
        # (e.g. "which model actually wrote this summary") silently wrong.
        sanitized = redact_raw_scope_summary(raw, enabled=redact_secrets)
        return validate_and_build_scope_summary(
            sanitized,
            scope_id=scope.id,
            revision=next_revision,
            model=model,
            generated_at=now,
            source_hash=source_hash,
            source_files=source_files,
            known_files=known_files,
            known_symbol_ids=known_symbol_ids,
        )

    attempts: list[tuple[ModelProvider, str]] = [(primary_provider, user_prompt)]
    last_reason = ""

    for attempt_index, (provider, prompt) in enumerate(attempts):
        parsed, reason, cost, latency, input_tokens = _attempt(
            provider, system_prompt, prompt, max_tokens, pricing
        )
        metrics.cost += cost
        metrics.latency_seconds += latency
        metrics.input_tokens += input_tokens
        # "parsed is None" covers two different situations that must not be
        # conflated: a true transport/HTTP failure (no response arrived at
        # all -- `reason` is the "provider_error:..." string _attempt
        # produces from a caught ProviderError) versus a response that DID
        # arrive but wasn't valid JSON (there's a real "previous response"
        # to point the repair prompt at). Only the former is a provider
        # error for metrics purposes and only the former should retry with
        # the prompt unchanged instead of a repair prompt.
        attempt_was_provider_error = parsed is None and reason.startswith("provider_error:")
        if parsed is None:
            metrics.provider_error = metrics.provider_error or attempt_was_provider_error
            last_reason = reason
            local_log.append(f"[{scope.id}] attempt failed: {reason}")
        else:
            outcome = _validate(parsed, model=provider.model)
            metrics.reference_total += _reference_list_len(parsed, "entry_points") + _reference_list_len(
                parsed, "important_symbols"
            )
            metrics.reference_stripped += len(outcome.stripped)
            if outcome.summary is not None:
                metrics.schema_success = True
                if outcome.stripped:
                    local_log.append(f"[{scope.id}] stripped entries: {outcome.stripped}")
                return RefreshOutcome(summary=outcome.summary, metrics=metrics, local_log_lines=local_log)
            last_reason = outcome.reject_reason or "validation failed"
            local_log.append(f"[{scope.id}] rejected: {last_reason}")

        if attempt_index == 0:
            # Queue the retry against the same (primary) provider before
            # ever trying the fallback model. A repair prompt only makes
            # sense after a real response failed *validation* -- there's
            # something concrete to correct. A ProviderError means no
            # response ever arrived (network/HTTP failure), so there's
            # nothing to "fix"; retrying with the unmodified original
            # prompt is the right move there, not appending a confusing
            # "your previous response failed because: HTTP 429 ..." preface
            # that has nothing to do with the model's own output. Confirmed
            # by hand this exact case actually happens: a real OpenRouter
            # 429 during Milestone 5 development fed straight into the
            # repair-prompt template before this fix.
            retry_prompt = user_prompt if attempt_was_provider_error else _build_repair_prompt(
                user_prompt, last_reason
            )
            attempts.append((primary_provider, retry_prompt))
        elif attempt_index == 1 and fallback_provider is not None:
            attempts.append((fallback_provider, user_prompt))
            metrics.used_fallback = True

    # Every attempt failed: build the failure revision per DATA_MODEL §2.4.
    sanitized_error = _sanitized_last_error(last_reason)
    local_log.append(f"[{scope.id}] all attempts exhausted, last_error={sanitized_error}: {last_reason}")
    if current is not None:
        failure_summary = current.model_copy(
            update={
                "revision": next_revision,
                "status": SemanticStatus.stale,
                "last_error": sanitized_error,
                "generated_at": now,
                "source_hash": source_hash,
                "source_files": source_files,
            }
        )
    else:
        failure_summary = ScopeSummary(
            scope_id=scope.id,
            revision=next_revision,
            purpose="",
            generated_at=now,
            model=primary_provider.model,
            source_hash=source_hash,
            source_files=source_files,
            status=SemanticStatus.unavailable,
            last_error=sanitized_error,
        )
    return RefreshOutcome(summary=failure_summary, metrics=metrics, local_log_lines=local_log)


@dataclass
class SemanticRefreshResult:
    new_revisions: list[ScopeSummary]
    # New `semantic.jsonl` lines to append, one per attempted scope, in the
    # order they were attempted — success or failure, per
    # `refresh_scope_summary`'s contract of always returning a revision.
    attempted_scope_ids: list[str]
    skipped_scope_ids: list[str]
    # Needed a refresh but the run-level input-token budget ran out before
    # reaching them; deferred to the next `rune update`, not treated as a
    # failure (no revision is appended for these).
    metrics: list[ScopeRefreshMetrics]
    local_log_lines: list[str]


def aggregate_metrics(metrics: list[ScopeRefreshMetrics]) -> dict[str, float]:
    """The six run-level metrics ARCHITECTURE.md §4.5 asks for, computed
    from this run's per-scope `ScopeRefreshMetrics`. Returns all-zero rates
    (not NaN/division-by-zero) when nothing was attempted.
    """
    attempted = len(metrics)
    if attempted == 0:
        return {
            "schema_success_rate": 0.0,
            "reference_strip_rate": 0.0,
            "fallback_rate": 0.0,
            "provider_error_rate": 0.0,
            "cost": 0.0,
            "latency_seconds": 0.0,
        }
    reference_total = sum(m.reference_total for m in metrics)
    return {
        "schema_success_rate": sum(1 for m in metrics if m.schema_success) / attempted,
        "reference_strip_rate": (
            sum(m.reference_stripped for m in metrics) / reference_total
            if reference_total
            else 0.0
        ),
        "fallback_rate": sum(1 for m in metrics if m.used_fallback) / attempted,
        "provider_error_rate": sum(1 for m in metrics if m.provider_error) / attempted,
        "cost": sum(m.cost for m in metrics),
        "latency_seconds": sum(m.latency_seconds for m in metrics),
    }


def run_semantic_refresh(
    *,
    repo_root: Path,
    scopes: list[Scope],
    current_summaries: dict[str, ScopeSummary],
    file_hashes: dict[str, str],
    symbols: list[Symbol],
    primary_provider: ModelProvider,
    fallback_provider: ModelProvider | None,
    max_input_tokens_per_run: int,
    max_tokens_per_call: int = 16000,
    pricing: PricingConfig | None = None,
    redact_secrets: bool = True,
) -> SemanticRefreshResult:
    """Refreshes every scope whose summary `needs_refresh`, stopping once
    the run-level `max_input_tokens_per_run` budget (DATA_MODEL.md §7's
    `SemanticBudget`) is spent — remaining scopes are deferred to the next
    `rune update`, not treated as failures.
    """
    symbol_owning_file = {symbol.symbol_id: symbol.file for symbol in symbols}
    symbols_by_id = {symbol.symbol_id: symbol for symbol in symbols}
    known_files = set(file_hashes)
    known_symbol_ids = set(symbols_by_id)

    new_revisions: list[ScopeSummary] = []
    attempted: list[str] = []
    skipped: list[str] = []
    metrics: list[ScopeRefreshMetrics] = []
    local_log: list[str] = []
    input_tokens_used = 0

    for scope in scopes:
        source_files = compute_source_files(scope, file_hashes, symbol_owning_file)
        current = current_summaries.get(scope.id)
        if not needs_refresh(current, working_tree_fingerprint(source_files)):
            continue
        if input_tokens_used >= max_input_tokens_per_run:
            skipped.append(scope.id)
            continue

        # Prompt content: every symbol explicitly listed as a member, plus
        # every symbol whose owning file is a member file (so a scope
        # defined purely by `members.files` still gets real symbol content
        # in the prompt, not just a bare file list).
        member_symbol_ids = set(scope.members.symbols)
        scope_symbols = [
            symbol
            for symbol in symbols
            if symbol.symbol_id in member_symbol_ids or symbol.file in scope.members.files
        ]

        outcome = refresh_scope_summary(
            repo_root=repo_root,
            scope=scope,
            primary_provider=primary_provider,
            fallback_provider=fallback_provider,
            source_files=source_files,
            known_files=known_files,
            known_symbol_ids=known_symbol_ids,
            symbols=scope_symbols,
            current=current,
            max_tokens=max_tokens_per_call,
            pricing=pricing,
            redact_secrets=redact_secrets,
        )
        new_revisions.append(outcome.summary)
        attempted.append(scope.id)
        metrics.append(outcome.metrics)
        local_log.extend(outcome.local_log_lines)
        input_tokens_used += outcome.metrics.input_tokens

    return SemanticRefreshResult(
        new_revisions=new_revisions,
        attempted_scope_ids=attempted,
        skipped_scope_ids=skipped,
        metrics=metrics,
        local_log_lines=local_log,
    )
