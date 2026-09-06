from __future__ import annotations

import json

import httpx
import pytest

from rune.core.semantic.provider import (
    OpenAICompatibleProvider,
    ProviderError,
    ProviderResponse,
)
from rune.core.semantic.redaction import redact_raw_scope_summary, redact_text
from rune.core.semantic.validation import validate_and_build_scope_summary
from rune.core.semantic.worker import (
    aggregate_metrics,
    compute_source_files,
    needs_refresh,
    refresh_scope_summary,
    run_semantic_refresh,
)
from rune.core.storage.models import (
    Scope,
    ScopeMembers,
    ScopeSource,
    ScopeSummary,
    SemanticStatus,
    Symbol,
    SymbolKind,
)


def _symbol(symbol_id: str, file: str, name: str, kind: SymbolKind = SymbolKind.function) -> Symbol:
    return Symbol(
        symbol_id=symbol_id, file=file, name=name, qualified_name=name, kind=kind,
        signature=None, start_line=1, end_line=2,
    )


# --------------------------------------------------------------------------
# provider.py
# --------------------------------------------------------------------------


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://example.invalid/v1",
        transport=httpx.MockTransport(handler),
    )


def test_provider_returns_content_and_usage_on_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "cost": 0.0001},
            },
        )

    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1", api_key="test-key", model="m", client=_client(handler)
    )
    response = provider.complete(system_prompt="sys", user_prompt="usr", max_tokens=100)
    assert response == ProviderResponse(content="hello", input_tokens=12, output_tokens=3, cost=0.0001)


def test_provider_raises_on_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1", api_key="k", model="m", client=_client(handler)
    )
    with pytest.raises(ProviderError, match="HTTP 500"):
        provider.complete(system_prompt="s", user_prompt="u", max_tokens=10)


def test_provider_raises_on_null_content() -> None:
    """Regression: a reasoning model can burn its whole max_tokens budget on
    the `reasoning` field before emitting any `content` -- confirmed against
    the real OpenRouter API with qwen/qwen3.8-flash during Milestone 5
    development. This must surface as a clear ProviderError, not a
    confusing downstream JSON-parse failure.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": None, "reasoning": "thinking..."}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20},
            },
        )

    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1", api_key="k", model="m", client=_client(handler)
    )
    with pytest.raises(ProviderError, match="empty content"):
        provider.complete(system_prompt="s", user_prompt="u", max_tokens=20)


def test_provider_raises_on_malformed_response_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1", api_key="k", model="m", client=_client(handler)
    )
    with pytest.raises(ProviderError, match="malformed response shape"):
        provider.complete(system_prompt="s", user_prompt="u", max_tokens=10)


def test_provider_raises_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1", api_key="k", model="m", client=_client(handler)
    )
    with pytest.raises(ProviderError, match="request failed"):
        provider.complete(system_prompt="s", user_prompt="u", max_tokens=10)


# --------------------------------------------------------------------------
# redaction.py
# --------------------------------------------------------------------------


def test_redact_text_masks_common_secret_shapes() -> None:
    text = (
        "key sk-abcdefghijklmnopqrstuvwx1234, "
        "aws AKIAABCDEFGHIJKLMNOP, "
        "password=hunter12345678"
    )
    redacted = redact_text(text)
    assert "sk-abcdefghijklmnopqrstuvwx1234" not in redacted
    assert "AKIAABCDEFGHIJKLMNOP" not in redacted
    assert "hunter12345678" not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_redact_raw_scope_summary_only_touches_free_text_fields() -> None:
    raw = {
        "purpose": "uses sk-abcdefghijklmnopqrstuvwx1234 internally",
        "responsibilities": ["talks to password=hunter12345678"],
        "entry_points": ["sk-abcdefghijklmnopqrstuvwx1234looking_symbol_id"],
    }
    redacted = redact_raw_scope_summary(raw)
    assert "sk-" not in redacted["purpose"]
    assert "[REDACTED]" in redacted["responsibilities"][0]
    # entry_points is a structural reference list, not free text -- must
    # pass through completely untouched even though it looks key-shaped,
    # otherwise a real symbol_id could be corrupted into failing reference
    # validation for the wrong reason.
    assert redacted["entry_points"] == raw["entry_points"]


# --------------------------------------------------------------------------
# validation.py
# --------------------------------------------------------------------------


def test_validation_rejects_empty_purpose() -> None:
    outcome = validate_and_build_scope_summary(
        {"purpose": "   "},
        scope_id="s", revision=1, model="m", generated_at="2026-01-01T00:00:00Z",
        source_hash="sha256:x", source_files={}, known_files=set(), known_symbol_ids=set(),
    )
    assert outcome.summary is None
    assert outcome.reject_reason == "purpose is empty"


def test_validation_strips_unknown_entries_without_rejecting() -> None:
    outcome = validate_and_build_scope_summary(
        {
            "purpose": "does things",
            "entry_points": ["real.py", "ghost.py"],
            "important_symbols": ["sym-real", "sym-ghost"],
        },
        scope_id="s", revision=1, model="m", generated_at="2026-01-01T00:00:00Z",
        source_hash="sha256:x", source_files={"real.py": "sha256:y"},
        known_files={"real.py"}, known_symbol_ids={"sym-real"},
    )
    assert outcome.summary is not None
    assert outcome.summary.entry_points == ["real.py"]
    assert outcome.summary.important_symbols == ["sym-real"]
    assert len(outcome.stripped) == 2


def test_validation_leaves_dependencies_untouched() -> None:
    """`dependencies` may be a scope_id or an external package name -- not
    verifiable against known_files/known_symbol_ids, so never stripped."""
    outcome = validate_and_build_scope_summary(
        {"purpose": "p", "dependencies": ["requests", "some-other-scope"]},
        scope_id="s", revision=1, model="m", generated_at="2026-01-01T00:00:00Z",
        source_hash="sha256:x", source_files={}, known_files=set(), known_symbol_ids=set(),
    )
    assert outcome.summary is not None
    assert outcome.summary.dependencies == ["requests", "some-other-scope"]
    assert outcome.stripped == []


# --------------------------------------------------------------------------
# worker.py — pure helpers
# --------------------------------------------------------------------------


def test_compute_source_files_unions_file_and_symbol_owning_files() -> None:
    scope = Scope(
        id="auth", name="Auth", locked=False, source=ScopeSource.human,
        members=ScopeMembers(files=[], symbols=["sym-1", "sym-2"]),
    )
    source_files = compute_source_files(
        scope,
        file_hashes={"auth/service.py": "sha256:a", "auth/token.py": "sha256:b", "other.py": "sha256:c"},
        symbol_owning_file={"sym-1": "auth/service.py", "sym-2": "auth/token.py"},
    )
    assert source_files == {"auth/service.py": "sha256:a", "auth/token.py": "sha256:b"}


def test_needs_refresh_true_when_no_current_summary() -> None:
    assert needs_refresh(None, "sha256:x") is True


def test_needs_refresh_true_when_status_unavailable_even_if_hash_matches() -> None:
    current = ScopeSummary(
        scope_id="s", revision=1, purpose="", generated_at="2026-01-01T00:00:00Z",
        model="m", source_hash="sha256:x", status=SemanticStatus.unavailable,
    )
    assert needs_refresh(current, "sha256:x") is True


def test_needs_refresh_false_when_fresh_and_hash_matches() -> None:
    current = ScopeSummary(
        scope_id="s", revision=1, purpose="p", generated_at="2026-01-01T00:00:00Z",
        model="m", source_hash="sha256:x", status=SemanticStatus.fresh,
    )
    assert needs_refresh(current, "sha256:x") is False


def test_needs_refresh_true_when_hash_differs() -> None:
    current = ScopeSummary(
        scope_id="s", revision=1, purpose="p", generated_at="2026-01-01T00:00:00Z",
        model="m", source_hash="sha256:old", status=SemanticStatus.fresh,
    )
    assert needs_refresh(current, "sha256:new") is True


def test_aggregate_metrics_returns_zeros_for_empty_input() -> None:
    result = aggregate_metrics([])
    assert result["schema_success_rate"] == 0.0
    assert result["reference_strip_rate"] == 0.0


# --------------------------------------------------------------------------
# worker.py — refresh_scope_summary fallback ladder (FakeProvider, no network)
# --------------------------------------------------------------------------


class FakeProvider:
    """Implements the ModelProvider protocol; queued responses are either a
    content string or an Exception instance to raise."""

    def __init__(self, model: str, responses: list) -> None:
        self.model = model
        self._responses = list(responses)
        self.prompts_seen: list[str] = []

    def complete(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> ProviderResponse:
        self.prompts_seen.append(user_prompt)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return ProviderResponse(content=item, input_tokens=100, output_tokens=50, cost=0.01)


def _scope() -> Scope:
    return Scope(
        id="app", name="App", description="", locked=False, source=ScopeSource.human,
        members=ScopeMembers(files=["app/main.py"], symbols=[]),
    )


def _good_json(purpose: str = "does the app thing") -> str:
    return json.dumps({"purpose": purpose, "entry_points": [], "important_symbols": []})


def test_refresh_succeeds_on_first_attempt() -> None:
    provider = FakeProvider("primary-model", [_good_json()])
    outcome = refresh_scope_summary(
        scope=_scope(), primary_provider=provider, fallback_provider=None,
        source_files={"app/main.py": "sha256:x"}, known_files={"app/main.py"},
        known_symbol_ids=set(), symbols=[], current=None,
    )
    assert outcome.summary.status == SemanticStatus.fresh
    assert outcome.summary.revision == 1
    assert outcome.summary.purpose == "does the app thing"
    assert outcome.metrics.schema_success is True
    assert outcome.metrics.used_fallback is False
    assert len(provider.prompts_seen) == 1


def test_refresh_succeeds_on_repair_retry() -> None:
    provider = FakeProvider("primary-model", ["not json at all", _good_json("fixed purpose")])
    outcome = refresh_scope_summary(
        scope=_scope(), primary_provider=provider, fallback_provider=None,
        source_files={"app/main.py": "sha256:x"}, known_files={"app/main.py"},
        known_symbol_ids=set(), symbols=[], current=None,
    )
    assert outcome.summary.status == SemanticStatus.fresh
    assert outcome.summary.purpose == "fixed purpose"
    assert len(provider.prompts_seen) == 2
    assert "failed validation" in provider.prompts_seen[1]


def test_refresh_falls_back_to_fallback_model_after_repair_retry_fails() -> None:
    primary = FakeProvider("primary-model", ["nope", "still nope"])
    fallback = FakeProvider("fallback-model", [_good_json("fallback saved it")])
    outcome = refresh_scope_summary(
        scope=_scope(), primary_provider=primary, fallback_provider=fallback,
        source_files={"app/main.py": "sha256:x"}, known_files={"app/main.py"},
        known_symbol_ids=set(), symbols=[], current=None,
    )
    assert outcome.summary.status == SemanticStatus.fresh
    assert outcome.summary.purpose == "fallback saved it"
    assert outcome.metrics.used_fallback is True
    assert len(primary.prompts_seen) == 2
    assert len(fallback.prompts_seen) == 1


def test_refresh_all_attempts_fail_with_no_prior_summary_yields_unavailable() -> None:
    primary = FakeProvider("primary-model", ["nope", "still nope"])
    fallback = FakeProvider("fallback-model", [ProviderError("provider is down")])
    outcome = refresh_scope_summary(
        scope=_scope(), primary_provider=primary, fallback_provider=fallback,
        source_files={"app/main.py": "sha256:x"}, known_files={"app/main.py"},
        known_symbol_ids=set(), symbols=[], current=None,
    )
    assert outcome.summary.status == SemanticStatus.unavailable
    assert outcome.summary.revision == 1
    assert outcome.summary.purpose == ""
    assert outcome.summary.last_error is not None
    # last_error must be a short sanitized classification, never the raw
    # exception text -- "provider is down" would leak into canonical git
    # history if this regressed.
    assert "provider is down" not in outcome.summary.last_error


def test_refresh_all_attempts_fail_with_prior_summary_copies_content_forward() -> None:
    """Regression test for the round-10 design decision: a refresh failure
    on a scope that previously succeeded must not lose its content -- the
    new (failed) revision copies the old summary's content verbatim and
    only changes status/last_error/generated_at/source_hash/source_files.
    """
    current = ScopeSummary(
        scope_id="app", revision=3, purpose="the real purpose",
        responsibilities=["does real things"], generated_at="2026-01-01T00:00:00Z",
        model="old-model", source_hash="sha256:old", source_files={"app/main.py": "sha256:old"},
        status=SemanticStatus.fresh,
    )
    primary = FakeProvider("primary-model", [ProviderError("timeout"), ProviderError("timeout again")])
    outcome = refresh_scope_summary(
        scope=_scope(), primary_provider=primary, fallback_provider=None,
        source_files={"app/main.py": "sha256:new"}, known_files={"app/main.py"},
        known_symbol_ids=set(), symbols=[], current=current,
    )
    assert outcome.summary.revision == 4
    assert outcome.summary.status == SemanticStatus.stale
    assert outcome.summary.purpose == "the real purpose"  # copied forward, not lost
    assert outcome.summary.responsibilities == ["does real things"]
    from rune.core.hashing import working_tree_fingerprint

    assert outcome.summary.source_hash == working_tree_fingerprint({"app/main.py": "sha256:new"})
    assert outcome.summary.source_hash != current.source_hash  # updated, not stale
    assert outcome.summary.last_error == "provider_error:ProviderError"


def test_refresh_falls_back_to_pricing_estimate_when_provider_reports_no_cost() -> None:
    """DATA_MODEL.md §7 / ARCHITECTURE.md §11: when a provider doesn't
    report cost (unlike OpenRouter, which does), the run-level cost metric
    must fall back to `config.pricing`'s per-million-token estimate rather
    than silently reporting 0.0.
    """
    from rune.core.storage.models import PricingConfig

    class _NoCostProvider:
        model = "no-cost-model"

        def complete(self, *, system_prompt, user_prompt, max_tokens):
            return ProviderResponse(
                content=_good_json(), input_tokens=1_000_000, output_tokens=500_000, cost=None
            )

    pricing = PricingConfig(input_per_million=2.0, output_per_million=8.0)
    outcome = refresh_scope_summary(
        scope=_scope(), primary_provider=_NoCostProvider(), fallback_provider=None,
        source_files={"app/main.py": "sha256:x"}, known_files={"app/main.py"},
        known_symbol_ids=set(), symbols=[], current=None, pricing=pricing,
    )
    # 1,000,000 input tokens @ $2/M + 500,000 output tokens @ $8/M = $2 + $4 = $6
    assert outcome.metrics.cost == pytest.approx(6.0)


def test_refresh_no_fallback_configured_stops_after_repair_retry() -> None:
    primary = FakeProvider("primary-model", ["nope", "still nope"])
    outcome = refresh_scope_summary(
        scope=_scope(), primary_provider=primary, fallback_provider=None,
        source_files={"app/main.py": "sha256:x"}, known_files={"app/main.py"},
        known_symbol_ids=set(), symbols=[], current=None,
    )
    assert outcome.summary.status == SemanticStatus.unavailable
    assert len(primary.prompts_seen) == 2  # no third (fallback) attempt happened


# --------------------------------------------------------------------------
# worker.py — run_semantic_refresh orchestrator
# --------------------------------------------------------------------------


def test_run_semantic_refresh_skips_fresh_unchanged_scopes() -> None:
    scope = _scope()
    current = ScopeSummary(
        scope_id="app", revision=1, purpose="p", generated_at="2026-01-01T00:00:00Z",
        model="m", source_hash="", source_files={"app/main.py": "sha256:x"},
        status=SemanticStatus.fresh,
    )
    # source_hash must match what compute_source_files/working_tree_fingerprint
    # would produce for these file_hashes, so recompute it the same way the
    # orchestrator does rather than hand-writing a guess.
    from rune.core.hashing import working_tree_fingerprint

    current = current.model_copy(
        update={"source_hash": working_tree_fingerprint({"app/main.py": "sha256:x"})}
    )
    provider = FakeProvider("m", [_good_json()])
    result = run_semantic_refresh(
        scopes=[scope], current_summaries={"app": current},
        file_hashes={"app/main.py": "sha256:x"}, symbols=[],
        primary_provider=provider, fallback_provider=None, max_input_tokens_per_run=100_000,
    )
    assert result.attempted_scope_ids == []
    assert result.new_revisions == []
    assert provider.prompts_seen == []  # never called -- nothing was stale


def test_run_semantic_refresh_attempts_scope_with_no_prior_summary() -> None:
    provider = FakeProvider("m", [_good_json()])
    result = run_semantic_refresh(
        scopes=[_scope()], current_summaries={},
        file_hashes={"app/main.py": "sha256:x"}, symbols=[],
        primary_provider=provider, fallback_provider=None, max_input_tokens_per_run=100_000,
    )
    assert result.attempted_scope_ids == ["app"]
    assert len(result.new_revisions) == 1
    assert result.new_revisions[0].status == SemanticStatus.fresh
    assert result.skipped_scope_ids == []


def test_run_semantic_refresh_defers_scopes_once_token_budget_is_spent() -> None:
    scope_a = Scope(
        id="a", name="A", locked=False, source=ScopeSource.human,
        members=ScopeMembers(files=["a.py"], symbols=[]),
    )
    scope_b = Scope(
        id="b", name="B", locked=False, source=ScopeSource.human,
        members=ScopeMembers(files=["b.py"], symbols=[]),
    )
    provider = FakeProvider("m", [_good_json(), _good_json()])
    result = run_semantic_refresh(
        scopes=[scope_a, scope_b], current_summaries={},
        file_hashes={"a.py": "sha256:a", "b.py": "sha256:b"}, symbols=[],
        primary_provider=provider, fallback_provider=None,
        max_input_tokens_per_run=100,  # FakeProvider reports 100 input_tokens per call
    )
    assert result.attempted_scope_ids == ["a"]
    assert result.skipped_scope_ids == ["b"]
    assert len(result.new_revisions) == 1


def test_run_semantic_refresh_includes_member_file_symbols_in_prompt() -> None:
    """A scope defined only via `members.files` (no explicit `members.symbols`)
    must still get real symbol content in its prompt, not just a bare file
    list -- otherwise the model has nothing to summarize.
    """
    scope = Scope(
        id="app", name="App", locked=False, source=ScopeSource.human,
        members=ScopeMembers(files=["app/main.py"], symbols=[]),
    )
    symbol = _symbol("sym-1", "app/main.py", "run")
    provider = FakeProvider("m", [_good_json()])
    run_semantic_refresh(
        scopes=[scope], current_summaries={}, file_hashes={"app/main.py": "sha256:x"},
        symbols=[symbol], primary_provider=provider, fallback_provider=None,
        max_input_tokens_per_run=100_000,
    )
    assert "sym-1" in provider.prompts_seen[0]
    assert "run" in provider.prompts_seen[0]
