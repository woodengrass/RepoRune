from __future__ import annotations

import json

import httpx
import pytest

from rune.core.semantic.provider import OpenAICompatibleProvider, ProviderError


def _provider(response_json: object) -> OpenAICompatibleProvider:
    def handler(_request: httpx.Request) -> httpx.Response:
        # `json=` rejects NaN/Infinity before the provider sees them; raw JSON
        # lets the test exercise validation of an untrusted provider payload.
        return httpx.Response(200, text=json.dumps(response_json, allow_nan=True))

    client = httpx.Client(
        base_url="https://example.invalid/v1",
        transport=httpx.MockTransport(handler),
    )
    return OpenAICompatibleProvider(
        base_url="https://example.invalid/v1",
        api_key="test-key",
        model="test-model",
        client=client,
    )


@pytest.mark.parametrize(
    "usage",
    [[], "not-an-object", 1],
)
def test_provider_rejects_non_object_usage(usage: object) -> None:
    provider = _provider({"choices": [{"message": {"content": "ok"}}], "usage": usage})

    with pytest.raises(ProviderError, match="usage must be an object"):
        provider.complete(system_prompt="system", user_prompt="user", max_tokens=10)


@pytest.mark.parametrize("cost", [True, float("nan"), float("inf"), -0.1])
def test_provider_rejects_non_finite_boolean_or_negative_cost(cost: object) -> None:
    provider = _provider({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"cost": cost},
    })

    with pytest.raises(ProviderError, match="malformed usage cost"):
        provider.complete(system_prompt="system", user_prompt="user", max_tokens=10)


def test_provider_rejects_non_object_response_shape() -> None:
    provider = _provider([{"choices": []}])

    with pytest.raises(ProviderError, match="response body must be an object"):
        provider.complete(system_prompt="system", user_prompt="user", max_tokens=10)
