"""`ModelProvider` protocol + OpenAI-compatible chat-completions implementations
(ARCHITECTURE.md §4.5, IMPLEMENTATION_PLAN.md Milestone 5).

Providers are deliberately dumb: they send one prompt, get one response back,
and raise `ProviderError` for anything that isn't a usable completion (network
failure, non-2xx, malformed response shape, or an empty `content` — which
happens with "thinking" models that spend their whole `max_tokens` budget on
the `reasoning` field before ever emitting `content`; see the module docstring
note below). Prompt construction, JSON parsing, schema validation, and the
retry/fallback policy all live in `worker.py` — a provider never decides
whether a response is "good enough", it just reports what came back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

import httpx

from rune.core.storage.models import ReasoningConfig, SemanticConfig


class ProviderError(Exception):
    """Raised for any provider-level failure: transport error, non-2xx
    response, or a response shape `worker.py` can't even parse a completion
    out of. Never raised for a response that parsed fine but failed schema/
    reference validation — that distinction belongs to validation.py.

    `status_code` is the HTTP status when one was actually received (None
    for a transport-level failure that never got a response at all — a
    timeout or connection error). This is the structured detail the
    three-tier provider health check (ARCHITECTURE.md §4.5) needs to tell
    "the provider is rate-limiting us" (429 — expected, not the user's
    fault) apart from every other failure (bad key, bad model name,
    genuine outage — all of which should fail loudly instead).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    input_tokens: int
    output_tokens: int
    cost: float | None
    # Provider-reported cost in USD when the API returns one (OpenRouter
    # does); None otherwise, in which case the caller falls back to
    # config.pricing's estimate (ARCHITECTURE.md §11, DATA_MODEL.md §7).


class ModelProvider(Protocol):
    model: str

    def complete(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> ProviderResponse: ...


class OpenAICompatibleProvider:
    """Generic chat-completions client for any OpenAI-compatible API
    (OpenRouter, OpenAI itself, and self-hosted/compatible endpoints).
    `OpenRouterProvider`/`OpenAIProvider` below are just this with the
    base_url pre-filled — the request/response shape is identical.

    `client` is accepted purely for test injection (an `httpx.Client` built
    with a `MockTransport`, so tests never touch the network); production
    callers omit it and get one built from `base_url`/`timeout`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
        reasoning: dict | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._extra_headers = extra_headers or {}
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self._reasoning = reasoning
        # `reasoning` is an OpenRouter extension (confirmed by hand: {"effort":
        # "low"}/{"enabled": False}/{"max_tokens": N} all measurably change
        # qwen/qwen3.8-flash's reasoning_tokens usage). Sent through unchanged
        # for any OpenAI-compatible base_url, but only OpenRouter is confirmed
        # to honor it — an OpenAI-proper endpoint would likely just ignore an
        # unrecognized field, not error, so this is left generic rather than
        # gated to one subclass.

    def complete(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> ProviderResponse:
        headers = {"Authorization": f"Bearer {self._api_key}", **self._extra_headers}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            # Best-effort strengthening, not a hard dependency: this is the
            # standard OpenAI-compatible field for "the message content
            # must be a JSON object", confirmed by hand to work against the
            # real OpenRouter API with qwen/qwen3.8-flash. Worker.py's own
            # JSON extraction (which tolerates markdown fences/prose) stays
            # in place regardless, so a provider/model that ignores this
            # field entirely degrades to exactly today's behavior rather
            # than breaking.
            "response_format": {"type": "json_object"},
        }
        if self._reasoning is not None:
            payload["reasoning"] = self._reasoning
        try:
            resp = self._client.post("/chat/completions", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"request failed: {exc}") from exc

        if resp.status_code != 200:
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:500]}", status_code=resp.status_code)

        try:
            data = resp.json()
            message = data["choices"][0]["message"]
            content = message["content"]
            usage = data.get("usage") or {}
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderError(f"malformed response shape: {exc}") from exc

        if content is None:
            # A "thinking" model (e.g. qwen3.8-flash) burned the entire
            # max_tokens budget on the `reasoning` field before it ever got
            # to `content` — confirmed by hand against the real OpenRouter
            # API during Milestone 5 development. This is a caller-fixable
            # budget problem, not a transient fluke, so it's surfaced
            # distinctly rather than as a generic malformed-response error.
            raise ProviderError(
                "provider returned empty content — max_tokens was likely "
                "exhausted by reasoning tokens before any output; increase "
                "max_tokens for reasoning-capable models"
            )

        return ProviderResponse(
            content=content,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            cost=usage.get("cost"),
        )

    def probe(self) -> None:
        """One minimal request used by the startup health check
        (ARCHITECTURE.md §4.5, Step 2) to confirm the API key and model
        name actually work before committing to a full per-scope refresh
        pass. Raises `ProviderError` exactly like `complete` — callers
        inspect `status_code`/`is_rate_limited` to decide how to react.
        Doesn't reuse `complete`: this only needs to know "did a 200 come
        back", not parse a completion out of the response, and `reasoning`
        is forced off regardless of config — the goal is reachability and
        auth, not exercising the reasoning path, and a reasoning-capable
        model could otherwise burn this tiny max_tokens budget entirely on
        thinking and return empty content, which would look like a probe
        failure that isn't actually one.
        """
        headers = {"Authorization": f"Bearer {self._api_key}", **self._extra_headers}
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "reasoning": {"enabled": False},
        }
        try:
            resp = self._client.post("/chat/completions", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"request failed: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:500]}", status_code=resp.status_code)


class OpenRouterProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: httpx.Client | None = None,
        reasoning: dict | None = None,
    ) -> None:
        super().__init__(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            model=model,
            client=client,
            reasoning=reasoning,
        )


class OpenAIProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: httpx.Client | None = None,
        reasoning: dict | None = None,
    ) -> None:
        super().__init__(
            base_url="https://api.openai.com/v1",
            api_key=api_key,
            model=model,
            client=client,
            reasoning=reasoning,
        )


# ARCHITECTURE.md §9: "API key 只從環境變數讀取；config.toml 絕不含機密" — the
# API key never comes from config.toml or any project file, only from the
# process environment. A project-root `token.env`-style file some
# developers keep around for local testing is a shell/dotenv-loading
# convenience *outside* rune's own code, never something rune itself reads.
_ENV_VAR_BY_PROVIDER = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def api_key_env_var(provider_name: str) -> str | None:
    """The environment variable `build_provider` would read the API key
    from for `provider_name`, or `None` if the provider name isn't
    recognized. Exposed so `rune doctor` can report presence/absence of
    the right variable without duplicating `_ENV_VAR_BY_PROVIDER` or
    performing `build_provider`'s live network probe.
    """
    return _ENV_VAR_BY_PROVIDER.get(provider_name)


def reasoning_payload(config: ReasoningConfig | None) -> dict | None:
    """Translates `SemanticConfig.reasoning` into the request-body shape
    OpenRouter expects. Returns None (omit the field entirely, use the
    model's own default) when nothing is actually configured — `enabled`
    defaults to True and an all-default `ReasoningConfig` shouldn't force
    anything.
    """
    if config is None:
        return None
    if not config.enabled:
        return {"enabled": False}
    payload: dict = {}
    if config.effort is not None:
        payload["effort"] = config.effort
    if config.max_tokens is not None:
        payload["max_tokens"] = config.max_tokens
    return payload or None


def build_provider(
    *, provider_name: str, model: str, reasoning: ReasoningConfig | None = None
) -> ModelProvider:
    env_var = _ENV_VAR_BY_PROVIDER.get(provider_name)
    if env_var is None:
        raise ProviderError(
            f"unknown semantic.provider {provider_name!r} "
            f"(known: {sorted(_ENV_VAR_BY_PROVIDER)})"
        )
    api_key = os.environ.get(env_var)
    if not api_key:
        raise ProviderError(f"{env_var} is not set in the environment")
    reasoning_dict = reasoning_payload(reasoning)
    if provider_name == "openrouter":
        return OpenRouterProvider(api_key=api_key, model=model, reasoning=reasoning_dict)
    return OpenAIProvider(api_key=api_key, model=model, reasoning=reasoning_dict)


class SemanticHealthStatus(str, Enum):
    """Outcome of the three-tier provider health check (ARCHITECTURE.md
    §4.5) run once per `rune update`, before any per-scope refresh.
    """

    ok = "ok"
    # config.semantic.enabled is false. Not an error -- the user turned
    # this off on purpose -- so callers must stay silent about it. An
    # empty `model` with enabled=True is NOT folded into this: that's
    # config_error (see below) -- a deployed project is expected to have
    # a real model configured, so leaving it blank must fail loudly.
    disabled = "disabled"
    # Step 1 (static, no network): empty model, unknown provider, or the
    # provider's API key env var isn't set. A basic setup mistake the
    # user needs to go fix, not a transient condition -- callers should
    # report this loudly rather than let it fail silently update after
    # update.
    config_error = "config_error"
    # Step 2 probe hit HTTP 429. Expected and not the user's fault --
    # callers should just let the user know, not treat it as broken.
    rate_limited = "rate_limited"
    # Step 2 probe failed for any other reason, even after one retry.
    # Something is actually wrong (bad key rejected by the provider, a
    # model name the provider doesn't recognize, a real outage) --
    # callers should report this loudly.
    probe_failed = "probe_failed"


@dataclass(frozen=True)
class SemanticHealthCheck:
    status: SemanticHealthStatus
    message: str | None = None


def check_semantic_health(config: SemanticConfig) -> tuple[SemanticHealthCheck, ModelProvider | None, ModelProvider | None]:
    """Runs the three-tier check and, only on success, returns the
    (primary, fallback) providers to actually use this run -- avoids
    building/probing the primary provider twice. Never raises: every
    failure mode is reported through the returned `SemanticHealthCheck`
    instead, because a semantic-provider problem must never abort the
    deterministic code index (ARCHITECTURE.md §4.9's failure-isolation
    principle applies here too).
    """
    if not config.enabled:
        return SemanticHealthCheck(status=SemanticHealthStatus.disabled), None, None
    if not config.model:
        # An empty model with semantic left enabled is exactly as much a
        # setup mistake as a missing API key -- a deployed project must
        # have this filled in to work at all, so it's `config_error`
        # (loud, non-zero CLI exit) rather than a silent skip. This is a
        # deliberate choice, not an oversight: earlier during development
        # this was folded into `disabled` to keep an unconfigured fresh
        # project quiet, but the user explicitly overrode that -- once
        # this project is actually deployed, `semantic.enabled=True`
        # without a real model means someone forgot to finish setup, and
        # that should fail loudly rather than silently do nothing.
        return (
            SemanticHealthCheck(
                status=SemanticHealthStatus.config_error,
                message="semantic.enabled is true but semantic.model is empty in config.toml",
            ),
            None,
            None,
        )

    try:
        primary = build_provider(
            provider_name=config.provider, model=config.model, reasoning=config.reasoning
        )
    except ProviderError as exc:
        return (
            SemanticHealthCheck(status=SemanticHealthStatus.config_error, message=str(exc)),
            None,
            None,
        )

    fallback: ModelProvider | None = None
    if config.fallback_model:
        try:
            fallback = build_provider(
                provider_name=config.provider, model=config.fallback_model, reasoning=config.reasoning
            )
        except ProviderError:
            # The fallback model is optional -- a bad fallback config must
            # not block the primary from running, same as the pre-existing
            # (pre-health-check) behavior.
            fallback = None

    # Step 2: one lightweight connection probe against the primary model,
    # with one retry for anything that isn't a rate limit.
    last_error: ProviderError | None = None
    for _ in range(2):
        try:
            primary.probe()
            return SemanticHealthCheck(status=SemanticHealthStatus.ok), primary, fallback
        except ProviderError as exc:
            if exc.is_rate_limited:
                return (
                    SemanticHealthCheck(
                        status=SemanticHealthStatus.rate_limited,
                        message=f"provider rate-limited the startup check: {exc}",
                    ),
                    None,
                    None,
                )
            last_error = exc
    return (
        SemanticHealthCheck(
            status=SemanticHealthStatus.probe_failed,
            message=f"provider startup check failed after one retry: {last_error}",
        ),
        None,
        None,
    )
