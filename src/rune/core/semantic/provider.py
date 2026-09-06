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
from typing import Protocol

import httpx


class ProviderError(Exception):
    """Raised for any provider-level failure: transport error, non-2xx
    response, or a response shape `worker.py` can't even parse a completion
    out of. Never raised for a response that parsed fine but failed schema/
    reference validation — that distinction belongs to validation.py.
    """


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
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._extra_headers = extra_headers or {}
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def complete(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> ProviderResponse:
        headers = {"Authorization": f"Bearer {self._api_key}", **self._extra_headers}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
        }
        try:
            resp = self._client.post("/chat/completions", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"request failed: {exc}") from exc

        if resp.status_code != 200:
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:500]}")

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


class OpenRouterProvider(OpenAICompatibleProvider):
    def __init__(self, *, api_key: str, model: str, client: httpx.Client | None = None) -> None:
        super().__init__(
            base_url="https://openrouter.ai/api/v1", api_key=api_key, model=model, client=client
        )


class OpenAIProvider(OpenAICompatibleProvider):
    def __init__(self, *, api_key: str, model: str, client: httpx.Client | None = None) -> None:
        super().__init__(
            base_url="https://api.openai.com/v1", api_key=api_key, model=model, client=client
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


def build_provider(*, provider_name: str, model: str) -> ModelProvider:
    env_var = _ENV_VAR_BY_PROVIDER.get(provider_name)
    if env_var is None:
        raise ProviderError(
            f"unknown semantic.provider {provider_name!r} "
            f"(known: {sorted(_ENV_VAR_BY_PROVIDER)})"
        )
    api_key = os.environ.get(env_var)
    if not api_key:
        raise ProviderError(f"{env_var} is not set in the environment")
    if provider_name == "openrouter":
        return OpenRouterProvider(api_key=api_key, model=model)
    return OpenAIProvider(api_key=api_key, model=model)
