"""Secret redaction, applied to agent/model-generated text before it is
written to any canonical file (ARCHITECTURE.md §9, §4.5).

V1's pattern set is a pragmatic, generic default (this project's own spec
document for the exact "§58 style" isn't available in this repo to copy
verbatim) — common API-key/token shapes and a catch-all `key/token/secret/
password = value` assignment pattern. Matches are replaced with
`[REDACTED]`, never deleted outright (a redacted sentence should still read
as a sentence, just with the secret masked), and detection deliberately
errs toward over-redaction: a false-positive match (e.g. a long hex hash
that happens to look like a key) costs a few masked characters, whereas a
missed real secret costs a leaked credential in a file that might be
committed to git.
"""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-or-v1-[A-Za-z0-9]{16,}"),  # OpenRouter secret keys
    re.compile(r"sk-[A-Za-z0-9]{16,}"),  # OpenAI-style secret keys
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_)
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # Generic "key/token/secret/password = value" assignment, quoted or
    # not — catches env-style and code-style leaks the specific patterns
    # above don't name (Bearer tokens, database passwords, etc.).
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|bearer)\b\s*[:=]\s*"
        r"['\"]?[A-Za-z0-9_\-/+=]{8,}['\"]?"
    ),
)


def redact_text(text: str) -> str:
    """Replaces every secret-shaped substring with `[REDACTED]`. Idempotent
    and order-independent enough for this pattern set (patterns don't
    overlap in what they match), applied to one string at a time.
    """
    for pattern in _PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def redact_strings(values: list[str]) -> list[str]:
    return [redact_text(v) for v in values]


# Free-text fields of a raw (not-yet-validated) ScopeSummary payload — the
# fields a model writes prose into, as opposed to entry_points/
# important_symbols/dependencies, which are supposed to be literal file
# paths/symbol_ids/scope_ids/package names and would be corrupted rather
# than protected by text redaction (a real symbol_id could coincidentally
# look like a "key = value" pattern and get mangled, which would then just
# make it fail reference validation for the wrong reason).
_FREE_TEXT_LIST_FIELDS = (
    "responsibilities",
    "data_flow",
    "invariants",
    "known_risks",
    "open_questions",
)


def redact_raw_scope_summary(raw: dict) -> dict:
    """Applied to the freshly-parsed-JSON provider response, before schema/
    reference validation (IMPLEMENTATION_PLAN.md Milestone 5: "Redaction
    pass ... 在驗證之前執行"). Only touches free-text fields; reference-list
    fields pass through untouched.
    """
    redacted = dict(raw)
    purpose = redacted.get("purpose")
    if isinstance(purpose, str):
        redacted["purpose"] = redact_text(purpose)
    for field_name in _FREE_TEXT_LIST_FIELDS:
        values = redacted.get(field_name)
        if isinstance(values, list):
            redacted[field_name] = [redact_text(str(v)) for v in values]
    return redacted
