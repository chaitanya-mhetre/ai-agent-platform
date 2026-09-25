from __future__ import annotations

from agentplat.providers.anthropic import AnthropicProvider
from agentplat.providers.base import ModelProvider
from agentplat.providers.gemini import GeminiProvider
from agentplat.providers.openai import OpenAIProvider
from agentplat.providers.retry import RetryingProvider
from agentplat.providers.rules import RuleBasedProvider


def build_provider(
    kind: str, *, model: str | None, api_key: str | None, base_url: str | None = None
) -> ModelProvider:
    """Build a provider from config. `fake` (the default) never touches the network."""
    if kind == "fake":
        return RuleBasedProvider()
    if not api_key or not model:
        raise ValueError(f"provider {kind!r} needs an API key and a model name")
    inner: ModelProvider
    if kind == "openai":
        inner = OpenAIProvider(api_key, model, base_url=base_url or "https://api.openai.com/v1")
    elif kind == "anthropic":
        inner = AnthropicProvider(api_key, model)
    elif kind == "gemini":
        inner = GeminiProvider(api_key, model)
    else:
        raise ValueError(f"unknown provider {kind!r}")
    return RetryingProvider(inner)
