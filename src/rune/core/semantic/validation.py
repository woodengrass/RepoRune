"""Schema + reference validation for a freshly-generated `ScopeSummary`
(ARCHITECTURE.md §4.5, IMPLEMENTATION_PLAN.md Milestone 5).

Two-tier outcome, confirmed in IMPLEMENTATION_PLAN.md:
- **Reject** the whole generation only when a core field (`purpose`) fails
  schema validation or is empty — the summary is unusable as a whole.
- **Strip** individual list entries (`entry_points`/`important_symbols`)
  that reference a file/symbol this run's index doesn't actually contain —
  a cheap model occasionally hallucinating one symbol name shouldn't
  invalidate an otherwise-good summary.

Called with the raw JSON dict *after* `redaction.redact_raw_scope_summary`
has already run on it (worker.py sequences that); this module only does
schema/reference checks, not text sanitization.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from rune.core.storage.models import ScopeSummary


@dataclass(frozen=True)
class ValidationOutcome:
    summary: ScopeSummary | None
    stripped: list[str] = field(default_factory=list)
    # Human-readable descriptions of stripped entries, e.g.
    # "important_symbols: 'app.services.Ghost'" — for logging/metrics, not
    # written to canonical.
    reject_reason: str | None = None
    # Set (summary is None) only for a core-field schema failure. This is
    # the *detailed* reason for local logging; worker.py maps it to a
    # short, sanitized `last_error` classification before writing canonical.


def _as_str_list(raw: dict, key: str) -> list[str]:
    values = raw.get(key) or []
    if not isinstance(values, list):
        return []
    return [str(v) for v in values]


def validate_and_build_scope_summary(
    raw: dict,
    *,
    scope_id: str,
    revision: int,
    model: str,
    generated_at: str,
    source_hash: str,
    source_files: dict[str, str],
    known_files: set[str],
    known_symbol_ids: set[str],
) -> ValidationOutcome:
    # `purpose` is the one core field IMPLEMENTATION_PLAN.md's schema-reject
    # rule names explicitly -- it must be an actual string, not silently
    # coerced from whatever shape the model returned. `str(raw.get(...))`
    # used to accept e.g. a dict for `purpose` and stringify its Python
    # repr into canonical as if it were a real description (confirmed by
    # hand: `{"nested": "dict"}` became the literal text
    # "{'nested': 'dict'}" and was accepted as a valid `fresh` summary).
    # The list fields below stay lenient by design: a wrong shape there is
    # treated the same as an unknown reference (best-effort, not a reason
    # to reject an otherwise-good summary) -- only `purpose` is required to
    # carry real information for the summary to mean anything at all.
    raw_purpose = raw.get("purpose")
    if not isinstance(raw_purpose, str):
        return ValidationOutcome(
            summary=None,
            reject_reason=(
                f"schema_validation_failed: purpose must be a string, "
                f"got {type(raw_purpose).__name__}"
            ),
        )
    try:
        candidate = ScopeSummary(
            scope_id=scope_id,
            revision=revision,
            purpose=raw_purpose,
            responsibilities=_as_str_list(raw, "responsibilities"),
            entry_points=_as_str_list(raw, "entry_points"),
            important_symbols=_as_str_list(raw, "important_symbols"),
            dependencies=_as_str_list(raw, "dependencies"),
            data_flow=_as_str_list(raw, "data_flow"),
            invariants=_as_str_list(raw, "invariants"),
            known_risks=_as_str_list(raw, "known_risks"),
            open_questions=_as_str_list(raw, "open_questions"),
            generated_at=generated_at,
            model=model,
            source_hash=source_hash,
            source_files=source_files,
        )
    except ValidationError as exc:
        return ValidationOutcome(summary=None, reject_reason=f"schema_validation_failed: {exc}")

    if not candidate.purpose.strip():
        return ValidationOutcome(summary=None, reject_reason="purpose is empty")

    stripped: list[str] = []

    entry_points: list[str] = []
    for value in candidate.entry_points:
        if value in known_files or value in known_symbol_ids:
            entry_points.append(value)
        else:
            stripped.append(f"entry_points: {value!r}")

    important_symbols: list[str] = []
    for value in candidate.important_symbols:
        if value in known_symbol_ids:
            important_symbols.append(value)
        else:
            stripped.append(f"important_symbols: {value!r}")

    # `dependencies` may name another scope_id or an external package name
    # (DATA_MODEL.md §2.4) — neither is verifiable against this run's known
    # files/symbols, so it's left untouched (best-effort, not stripped).
    final = candidate.model_copy(
        update={"entry_points": entry_points, "important_symbols": important_symbols}
    )
    return ValidationOutcome(summary=final, stripped=stripped)
