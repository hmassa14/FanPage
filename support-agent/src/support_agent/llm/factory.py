from __future__ import annotations

from ..config import Settings
from .base import LLMProvider


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    from .fake_provider import FakeProvider

    return FakeProvider(settings)
