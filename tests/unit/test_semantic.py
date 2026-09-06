from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from rune.core.hashing import working_tree_fingerprint
from rune.core.semantic.provider import (
    OpenAICompatibleProvider,
    ProviderError,
    ProviderResponse,
    SemanticHealthStatus,
    check_semantic_health,
    reasoning_payload,
)
from rune.core.semantic.redaction import redact_raw_scope_summary, redact_text
from rune.core.semantic.validation import validate_and_build_scope_summary
from rune.core.semantic.worker import (
    aggregate_metrics,
    compute_source_files,
    mark_possibly_stale,
    needs_refresh,
    refresh_scope_summary,
    run_semantic_refresh,
)
from rune.core.storage.models import (
    ReasoningConfig,
    Scope,
    ScopeMembers,
    ScopeSource,
    ScopeSummary,
    SemanticStatus,
    Symbol,
    SymbolKind,
)

# Deliberately nonexistent: these tests exercise prompt/retry/validation
# logic, not the actual symbol-snippet file reads (covered separately by
# test_read_symbol_snippet_* below). _read_symbol_snippet degrades to ""
# on any read failure rather than raising, so a nonexistent root is a
# safe stand-in wherever a real repo_root isn't the point of the test.
_TEST_REPO_ROOT = Path("/nonexistent-repo-root-for-tests")


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


def test_provider_sends_configured_reasoning_payload() -> None:
    """Config-driven reasoning control (added after confirming by hand
    against the real OpenRouter API that {"effort": ...}/{"enabled": False}/
    {"max_tokens": ...} all measurably change qwen/qwen3.8-flash's
    reasoning_tokens usage): whatever `reasoning` dict the provider was
    constructed with must actually appear in the request body.
    """
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1", api_key="k", model="m",
        client=_client(handler), reasoning={"effort": "low"},
    )
    provider.complete(system_prompt="s", user_prompt="u", max_tokens=10)
    assert captured["body"]["reasoning"] == {"effort": "low"}


def test_provider_omits_reasoning_key_when_not_configured() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1", api_key="k", model="m", client=_client(handler)
    )
    provider.complete(system_prompt="s", user_prompt="u", max_tokens=10)
    assert "reasoning" not in captured["body"]


def test_provider_requests_json_object_response_format() -> None:
    """Best-effort JSON-mode request, confirmed by hand against the real
    OpenRouter API with qwen/qwen3.8-flash before wiring this in -- not
    a hard dependency (worker.py's tolerant JSON extraction stays as the
    safety net regardless of whether a given provider honors this).
    """
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1", api_key="k", model="m", client=_client(handler)
    )
    provider.complete(system_prompt="s", user_prompt="u", max_tokens=10)
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_probe_succeeds_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["reasoning"] == {"enabled": False}  # probe forces reasoning off
        assert body["max_tokens"] == 1
        return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})

    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1", api_key="k", model="m", client=_client(handler)
    )
    provider.probe()  # must not raise -- probe doesn't parse content at all


def test_probe_raises_with_status_code_on_429() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1", api_key="k", model="m", client=_client(handler)
    )
    with pytest.raises(ProviderError) as exc_info:
        provider.probe()
    assert exc_info.value.status_code == 429
    assert exc_info.value.is_rate_limited


def test_probe_raises_without_status_code_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1", api_key="k", model="m", client=_client(handler)
    )
    with pytest.raises(ProviderError) as exc_info:
        provider.probe()
    assert exc_info.value.status_code is None
    assert not exc_info.value.is_rate_limited


def test_provider_error_non_429_status_is_not_rate_limited() -> None:
    assert not ProviderError("HTTP 500: boom", status_code=500).is_rate_limited


# --------------------------------------------------------------------------
# check_semantic_health -- ARCHITECTURE.md §4.5's three-tier provider
# health check (round 15, IMPLEMENTATION_PLAN.md items 82-84)
# --------------------------------------------------------------------------


def test_health_check_disabled_when_semantic_enabled_is_false() -> None:
    from rune.core.storage.models import SemanticConfig

    health, primary, fallback = check_semantic_health(SemanticConfig(enabled=False, model="m"))
    assert health.status is SemanticHealthStatus.disabled
    assert health.message is None
    assert primary is None
    assert fallback is None


def test_health_check_config_error_when_model_is_the_untouched_default(monkeypatch) -> None:
    """`enabled=True` with an empty `model` is exactly what every fresh
    `rune init` produces -- but the user explicitly wants this to fail
    loudly too, not stay quiet: a project that's actually deployed is
    expected to have a real model configured, so leaving `enabled=True`
    with nothing filled in is a setup mistake, same as a missing API key.
    """
    from rune.core.storage.models import SemanticConfig

    monkeypatch.setenv("OPENROUTER_API_KEY", "irrelevant")
    health, primary, _fallback = check_semantic_health(SemanticConfig(enabled=True, model=""))
    assert health.status is SemanticHealthStatus.config_error
    assert primary is None


def test_health_check_config_error_when_api_key_env_var_missing(monkeypatch) -> None:
    from rune.core.storage.models import SemanticConfig

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    health, primary, _fallback = check_semantic_health(SemanticConfig(enabled=True, model="a-real-model"))
    assert health.status is SemanticHealthStatus.config_error
    assert "OPENROUTER_API_KEY" in health.message
    assert primary is None


def test_health_check_config_error_on_unknown_provider(monkeypatch) -> None:
    from rune.core.storage.models import SemanticConfig

    health, primary, _fallback = check_semantic_health(
        SemanticConfig(enabled=True, provider="not-a-real-provider", model="m")
    )
    assert health.status is SemanticHealthStatus.config_error
    assert primary is None


def test_health_check_rate_limited_on_429_probe(monkeypatch) -> None:
    from rune.core.storage.models import SemanticConfig

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    def fake_build_provider(*, provider_name, model, reasoning=None):
        class _RateLimited:
            model = "m"

            def probe(self):
                raise ProviderError("HTTP 429: slow down", status_code=429)

        return _RateLimited()

    import rune.core.semantic.provider as provider_module

    monkeypatch.setattr(provider_module, "build_provider", fake_build_provider)
    health, primary, _fallback = check_semantic_health(SemanticConfig(enabled=True, model="m"))
    assert health.status is SemanticHealthStatus.rate_limited
    assert primary is None


def test_health_check_probe_failed_retries_once_then_gives_up(monkeypatch) -> None:
    """Non-rate-limit probe failures get exactly one retry -- confirmed by
    counting probe() calls -- before the health check gives up and reports
    `probe_failed`.
    """
    from rune.core.storage.models import SemanticConfig

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    call_count = {"n": 0}

    def fake_build_provider(*, provider_name, model, reasoning=None):
        class _AlwaysBroken:
            model = "m"

            def probe(self):
                call_count["n"] += 1
                raise ProviderError("HTTP 500: broken")

        return _AlwaysBroken()

    import rune.core.semantic.provider as provider_module

    monkeypatch.setattr(provider_module, "build_provider", fake_build_provider)
    health, primary, _fallback = check_semantic_health(SemanticConfig(enabled=True, model="m"))
    assert health.status is SemanticHealthStatus.probe_failed
    assert primary is None
    assert call_count["n"] == 2  # one attempt + one retry, not unbounded


def test_health_check_ok_when_probe_succeeds(monkeypatch) -> None:
    from rune.core.storage.models import SemanticConfig

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    def fake_build_provider(*, provider_name, model, reasoning=None):
        class _Healthy:
            def __init__(self, model):
                self.model = model

            def probe(self):
                return None

        return _Healthy(model)

    import rune.core.semantic.provider as provider_module

    monkeypatch.setattr(provider_module, "build_provider", fake_build_provider)
    health, primary, fallback = check_semantic_health(
        SemanticConfig(enabled=True, model="m", fallback_model="fb")
    )
    assert health.status is SemanticHealthStatus.ok
    assert primary is not None and primary.model == "m"
    assert fallback is not None and fallback.model == "fb"


def test_reasoning_payload_none_when_config_is_default() -> None:
    """An all-default ReasoningConfig (enabled=True, nothing else set) must
    not force anything -- omit the field entirely so the model's own
    default behavior is untouched.
    """
    assert reasoning_payload(None) is None
    assert reasoning_payload(ReasoningConfig()) is None


def test_reasoning_payload_disabled_takes_priority_over_effort() -> None:
    config = ReasoningConfig(enabled=False, effort="high", max_tokens=999)
    assert reasoning_payload(config) == {"enabled": False}


def test_reasoning_payload_includes_only_set_fields() -> None:
    assert reasoning_payload(ReasoningConfig(effort="low")) == {"effort": "low"}
    assert reasoning_payload(ReasoningConfig(max_tokens=500)) == {"max_tokens": 500}
    assert reasoning_payload(ReasoningConfig(effort="high", max_tokens=200)) == {
        "effort": "high", "max_tokens": 200,
    }


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


def test_redact_raw_scope_summary_redacts_dependencies_as_free_text() -> None:
    """Regression test: `dependencies` may be a scope_id or an external
    package name (DATA_MODEL.md §2.4) -- unlike entry_points/
    important_symbols, nothing validates it against known files/symbols,
    so a secret-shaped string there had no protection at all before this
    fix (it isn't stripped like an unknown reference would be, since
    there's no "known dependencies" set to check against).
    """
    raw = {"purpose": "p", "dependencies": ["sk-abcdefghijklmnopqrstuvwx1234"]}
    redacted = redact_raw_scope_summary(raw)
    assert redacted["dependencies"] == ["[REDACTED]"]


def test_redact_raw_scope_summary_enabled_false_bypasses_everything() -> None:
    """Wires up config.security.redact_secrets, which existed since
    Milestone 1 but nothing ever read -- redaction ran unconditionally
    regardless of the config value until this fix.
    """
    raw = {"purpose": "sk-abcdefghijklmnopqrstuvwx1234"}
    assert redact_raw_scope_summary(raw, enabled=False) == raw
    assert redact_raw_scope_summary(raw, enabled=True) != raw


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


def test_validation_rejects_non_string_purpose_instead_of_coercing() -> None:
    """Regression test: `purpose` used to go through `str(raw.get(...))`,
    which happily stringifies anything -- a dict became the literal text
    "{'nested': 'dict'}" and was accepted as a valid `fresh` summary.
    IMPLEMENTATION_PLAN.md's schema-reject rule names `purpose` as the
    field whose failure must reject the whole generation; silently
    coercing a wrong type isn't the same as validating it.
    """
    outcome = validate_and_build_scope_summary(
        {"purpose": {"nested": "dict, not a string"}},
        scope_id="s", revision=1, model="m", generated_at="2026-01-01T00:00:00Z",
        source_hash="sha256:x", source_files={}, known_files=set(), known_symbol_ids=set(),
    )
    assert outcome.summary is None
    assert "purpose must be a string" in (outcome.reject_reason or "")


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


def test_needs_refresh_true_when_status_orphaned_even_if_hash_matches() -> None:
    """Regression test: a scope deleted and then recreated with
    byte-identical member files used to stay stuck `orphaned` forever --
    the orphaned revision's source_hash equals the freshly recomputed one
    (nothing about the content changed), and orphaned wasn't in the
    "always retry" set alongside `unavailable`, so it was invisible to
    retrieval (Decision/Constraint's existing orphaned semantics) with no
    way back even with a provider ready to regenerate it. Reproduced by
    hand before this fix.
    """
    current = ScopeSummary(
        scope_id="s", revision=2, purpose="old purpose", generated_at="2026-01-01T00:00:00Z",
        model="m", source_hash="sha256:x", status=SemanticStatus.orphaned,
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


def _scope_with_files(scope_id: str, files: list[str]) -> Scope:
    return Scope(
        id=scope_id, name=scope_id, locked=False, source=ScopeSource.human,
        members=ScopeMembers(files=files),
    )


def test_mark_possibly_stale_skips_scope_that_never_succeeded() -> None:
    """DATA_MODEL.md §2.4's round-15 decision: a scope with no current
    summary at all, or one that's still `unavailable`, needs no extra
    revision here -- `needs_refresh` already returns True unconditionally
    for both, so it's picked up the moment a provider is available again.
    """
    scope = _scope_with_files("app", ["app/a.py"])
    unavailable = ScopeSummary(
        scope_id="app", revision=1, purpose="", generated_at="2026-01-01T00:00:00Z",
        model="m", source_hash="sha256:old", status=SemanticStatus.unavailable,
    )
    result = mark_possibly_stale(
        [scope], {"app": unavailable}, {"app/a.py": "sha256:new"}, [], "2026-01-02T00:00:00Z"
    )
    assert result == []

    result_no_current = mark_possibly_stale(
        [scope], {}, {"app/a.py": "sha256:new"}, [], "2026-01-02T00:00:00Z"
    )
    assert result_no_current == []


def test_mark_possibly_stale_skips_scope_whose_hash_is_unchanged() -> None:
    scope = _scope_with_files("app", ["app/a.py"])
    fresh = ScopeSummary(
        scope_id="app", revision=1, purpose="p", generated_at="2026-01-01T00:00:00Z",
        model="m", source_hash=working_tree_fingerprint({"app/a.py": "sha256:same"}),
        source_files={"app/a.py": "sha256:same"}, status=SemanticStatus.fresh,
    )
    result = mark_possibly_stale(
        [scope], {"app": fresh}, {"app/a.py": "sha256:same"}, [], "2026-01-02T00:00:00Z"
    )
    assert result == []


def test_mark_possibly_stale_appends_revision_copying_old_content_forward() -> None:
    """The core round-15 behavior: member files changed, no provider is
    usable this run, but the scope has real prior content -- copy it
    forward into a new revision, only touching status/hash/timestamp, so
    a consumer sees "this is possibly outdated" instead of a silently
    wrong `fresh`.
    """
    scope = _scope_with_files("app", ["app/a.py"])
    old_hash = working_tree_fingerprint({"app/a.py": "sha256:old"})
    current = ScopeSummary(
        scope_id="app", revision=3, purpose="does the app thing",
        responsibilities=["handles requests"], generated_at="2026-01-01T00:00:00Z",
        model="m", source_hash=old_hash, source_files={"app/a.py": "sha256:old"},
        status=SemanticStatus.fresh,
    )
    result = mark_possibly_stale(
        [scope], {"app": current}, {"app/a.py": "sha256:new"}, [], "2026-01-02T00:00:00Z"
    )
    assert len(result) == 1
    updated = result[0]
    assert updated.revision == 4
    assert updated.status is SemanticStatus.possibly_stale
    assert updated.generated_at == "2026-01-02T00:00:00Z"
    assert updated.source_files == {"app/a.py": "sha256:new"}
    assert updated.source_hash == working_tree_fingerprint({"app/a.py": "sha256:new"})
    # Content is copied forward unchanged -- this is a status/hash/
    # timestamp-only revision, never a fabricated re-summary.
    assert updated.purpose == "does the app thing"
    assert updated.responsibilities == ["handles requests"]


def test_mark_possibly_stale_fires_again_on_a_second_hash_change() -> None:
    """A scope already `possibly_stale` whose source changes yet again
    while still no provider is available gets another revision reflecting
    the latest files -- same hash-driven trigger as any other
    system-appended revision, no special-casing for "already stale".
    """
    scope = _scope_with_files("app", ["app/a.py"])
    current = ScopeSummary(
        scope_id="app", revision=4, purpose="p", generated_at="2026-01-02T00:00:00Z",
        model="m", source_hash=working_tree_fingerprint({"app/a.py": "sha256:mid"}),
        source_files={"app/a.py": "sha256:mid"}, status=SemanticStatus.possibly_stale,
    )
    result = mark_possibly_stale(
        [scope], {"app": current}, {"app/a.py": "sha256:newer"}, [], "2026-01-03T00:00:00Z"
    )
    assert len(result) == 1
    assert result[0].revision == 5
    assert result[0].source_files == {"app/a.py": "sha256:newer"}


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
        repo_root=_TEST_REPO_ROOT,
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


def test_refresh_reference_total_metric_ignores_malformed_non_list_field() -> None:
    """Regression test: a model can return a non-list value for
    `entry_points`/`important_symbols` (e.g. a bare string) despite the
    schema asking for a list. `validation.py`'s `_as_str_list` already
    guards against this (treats it as zero entries), but the
    `reference_total` metric computed alongside it used bare `len(...)` --
    which for a string counts *characters*, not list entries, wildly
    inflating `reference_strip_rate`. Confirmed by hand: a string value
    produced a double-digit `reference_total` for a summary whose
    validated entry_points was correctly empty.
    """
    provider = FakeProvider(
        "primary-model",
        [json.dumps({"purpose": "p", "entry_points": "not-a-list-just-a-string"})],
    )
    outcome = refresh_scope_summary(
        repo_root=_TEST_REPO_ROOT,
        scope=_scope(), primary_provider=provider, fallback_provider=None,
        source_files={"app/main.py": "sha256:x"}, known_files={"app/main.py"},
        known_symbol_ids=set(), symbols=[], current=None,
    )
    assert outcome.summary.entry_points == []
    assert outcome.metrics.reference_total == 0
    assert outcome.metrics.reference_stripped == 0


def test_refresh_succeeds_on_repair_retry() -> None:
    provider = FakeProvider("primary-model", ["not json at all", _good_json("fixed purpose")])
    outcome = refresh_scope_summary(
        repo_root=_TEST_REPO_ROOT,
        scope=_scope(), primary_provider=provider, fallback_provider=None,
        source_files={"app/main.py": "sha256:x"}, known_files={"app/main.py"},
        known_symbol_ids=set(), symbols=[], current=None,
    )
    assert outcome.summary.status == SemanticStatus.fresh
    assert outcome.summary.purpose == "fixed purpose"
    assert len(provider.prompts_seen) == 2
    assert "failed validation" in provider.prompts_seen[1]


def test_refresh_invalid_json_response_does_not_count_as_provider_error_metric() -> None:
    """Regression test, same root cause as the retry-prompt fix above but
    for the `provider_error` metric: a response that arrived but wasn't
    valid JSON is a schema/parsing problem, not a transport failure --
    `metrics.provider_error` (meant to track real ProviderError/network
    failures per ARCHITECTURE.md §4.5's six metrics) must not be set for
    it, or `provider_error_rate` would count "the model wrote bad JSON" as
    if the provider itself were unreliable.
    """
    provider = FakeProvider("primary-model", ["not json at all", _good_json("fixed purpose")])
    outcome = refresh_scope_summary(
        repo_root=_TEST_REPO_ROOT,
        scope=_scope(), primary_provider=provider, fallback_provider=None,
        source_files={"app/main.py": "sha256:x"}, known_files={"app/main.py"},
        known_symbol_ids=set(), symbols=[], current=None,
    )
    assert outcome.metrics.provider_error is False


def test_refresh_after_provider_error_retries_with_unmodified_prompt_not_repair_prompt() -> None:
    """Regression test: the retry after attempt 0 used to always build a
    repair prompt ("your previous response failed validation for this
    reason: ...") even when attempt 0 never got a response at all --
    reason was a raw provider_error string (network/HTTP failure), which
    got fed straight into the repair-prompt template. That's nonsensical
    (there's no "previous response" to explain a fix for) and, confirmed
    against the real OpenRouter API during Milestone 5 development, this
    exact scenario happens for real: a transient 429 rate-limit on the
    first call. The retry after a pure provider error must resend the
    original prompt unchanged, not a repair-styled one.
    """
    provider = FakeProvider(
        "primary-model", [ProviderError("HTTP 429: rate limited"), _good_json("recovered")]
    )
    outcome = refresh_scope_summary(
        repo_root=_TEST_REPO_ROOT,
        scope=_scope(), primary_provider=provider, fallback_provider=None,
        source_files={"app/main.py": "sha256:x"}, known_files={"app/main.py"},
        known_symbol_ids=set(), symbols=[], current=None,
    )
    assert outcome.summary.status == SemanticStatus.fresh
    assert outcome.summary.purpose == "recovered"
    assert len(provider.prompts_seen) == 2
    assert provider.prompts_seen[0] == provider.prompts_seen[1]  # unmodified retry
    assert "failed validation" not in provider.prompts_seen[1]


def test_refresh_falls_back_to_fallback_model_after_repair_retry_fails() -> None:
    primary = FakeProvider("primary-model", ["nope", "still nope"])
    fallback = FakeProvider("fallback-model", [_good_json("fallback saved it")])
    outcome = refresh_scope_summary(
        repo_root=_TEST_REPO_ROOT,
        scope=_scope(), primary_provider=primary, fallback_provider=fallback,
        source_files={"app/main.py": "sha256:x"}, known_files={"app/main.py"},
        known_symbol_ids=set(), symbols=[], current=None,
    )
    assert outcome.summary.status == SemanticStatus.fresh
    assert outcome.summary.purpose == "fallback saved it"
    assert outcome.metrics.used_fallback is True
    assert len(primary.prompts_seen) == 2
    assert len(fallback.prompts_seen) == 1
    # Regression: a summary the fallback model actually produced used to
    # still get tagged with the primary model's name, making provider/
    # model audit data (e.g. "which model wrote this?") silently wrong.
    assert outcome.summary.model == "fallback-model"


def test_refresh_all_attempts_fail_with_no_prior_summary_yields_unavailable() -> None:
    primary = FakeProvider("primary-model", ["nope", "still nope"])
    fallback = FakeProvider("fallback-model", [ProviderError("provider is down")])
    outcome = refresh_scope_summary(
        repo_root=_TEST_REPO_ROOT,
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
        repo_root=_TEST_REPO_ROOT,
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
        repo_root=_TEST_REPO_ROOT,
        scope=_scope(), primary_provider=_NoCostProvider(), fallback_provider=None,
        source_files={"app/main.py": "sha256:x"}, known_files={"app/main.py"},
        known_symbol_ids=set(), symbols=[], current=None, pricing=pricing,
    )
    # 1,000,000 input tokens @ $2/M + 500,000 output tokens @ $8/M = $2 + $4 = $6
    assert outcome.metrics.cost == pytest.approx(6.0)


def test_refresh_no_fallback_configured_stops_after_repair_retry() -> None:
    primary = FakeProvider("primary-model", ["nope", "still nope"])
    outcome = refresh_scope_summary(
        repo_root=_TEST_REPO_ROOT,
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
        repo_root=_TEST_REPO_ROOT,
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
        repo_root=_TEST_REPO_ROOT,
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
        repo_root=_TEST_REPO_ROOT,
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
        repo_root=_TEST_REPO_ROOT,
        scopes=[scope], current_summaries={}, file_hashes={"app/main.py": "sha256:x"},
        symbols=[symbol], primary_provider=provider, fallback_provider=None,
        max_input_tokens_per_run=100_000,
    )
    assert "sym-1" in provider.prompts_seen[0]
    assert "run" in provider.prompts_seen[0]


def test_run_semantic_refresh_prompt_includes_symbol_source_code(tmp_path) -> None:
    """Regression test: the prompt used to contain only symbol metadata
    (name/kind/signature), never the member files' actual implementation
    -- a model has very little to work with for `purpose`/`data_flow`/
    `invariants` without seeing the real code. Uses a real file on disk
    (not the sentinel _TEST_REPO_ROOT) so this exercises the actual
    line-range read, not just that the code path doesn't crash.
    """
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text(
        "def run():\n"
        "    unmistakable_marker_xyz = 42\n"
        "    return unmistakable_marker_xyz\n",
        encoding="utf-8",
    )
    scope = Scope(
        id="app", name="App", locked=False, source=ScopeSource.human,
        members=ScopeMembers(files=["app/main.py"], symbols=[]),
    )
    symbol = Symbol(
        symbol_id="sym-1", file="app/main.py", name="run", qualified_name="run",
        kind=SymbolKind.function, signature="def run()", start_line=1, end_line=3,
    )
    provider = FakeProvider("m", [_good_json()])
    run_semantic_refresh(
        repo_root=tmp_path,
        scopes=[scope], current_summaries={}, file_hashes={"app/main.py": "sha256:x"},
        symbols=[symbol], primary_provider=provider, fallback_provider=None,
        max_input_tokens_per_run=100_000,
    )
    assert "unmistakable_marker_xyz" in provider.prompts_seen[0]


def test_run_semantic_refresh_prompt_survives_unreadable_member_file(tmp_path) -> None:
    """A missing/unreadable file must degrade that one symbol's snippet to
    nothing, not crash the whole refresh -- same failure-isolation
    principle as everywhere else in this project. The file is listed in
    `members.files` but deliberately never created.
    """
    scope = Scope(
        id="app", name="App", locked=False, source=ScopeSource.human,
        members=ScopeMembers(files=["app/missing.py"], symbols=[]),
    )
    symbol = Symbol(
        symbol_id="sym-1", file="app/missing.py", name="ghost", qualified_name="ghost",
        kind=SymbolKind.function, signature=None, start_line=1, end_line=2,
    )
    provider = FakeProvider("m", [_good_json()])
    result = run_semantic_refresh(
        repo_root=tmp_path,
        scopes=[scope], current_summaries={}, file_hashes={"app/missing.py": "sha256:x"},
        symbols=[symbol], primary_provider=provider, fallback_provider=None,
        max_input_tokens_per_run=100_000,
    )
    assert result.new_revisions[0].status == SemanticStatus.fresh  # did not crash
    assert "sym-1" in provider.prompts_seen[0]  # metadata still present


def test_run_semantic_refresh_prompt_truncates_oversized_symbol_snippet(tmp_path) -> None:
    (tmp_path / "app").mkdir()
    huge_body = "\n".join(f"    line_{i} = {i}" for i in range(500))
    (tmp_path / "app" / "big.py").write_text(f"def run():\n{huge_body}\n", encoding="utf-8")
    scope = Scope(
        id="app", name="App", locked=False, source=ScopeSource.human,
        members=ScopeMembers(files=["app/big.py"], symbols=[]),
    )
    symbol = Symbol(
        symbol_id="sym-1", file="app/big.py", name="run", qualified_name="run",
        kind=SymbolKind.function, signature="def run()", start_line=1, end_line=501,
    )
    provider = FakeProvider("m", [_good_json()])
    run_semantic_refresh(
        repo_root=tmp_path,
        scopes=[scope], current_summaries={}, file_hashes={"app/big.py": "sha256:x"},
        symbols=[symbol], primary_provider=provider, fallback_provider=None,
        max_input_tokens_per_run=100_000,
    )
    prompt = provider.prompts_seen[0]
    assert "(truncated)" in prompt
    assert "line_499" not in prompt  # past the cap, never included
