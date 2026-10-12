from app.core import settings

from .base import LLMProvider


def build_llm(model: str | None = None) -> LLMProvider:
    """Return the configured LLM provider.

    Every caller goes through here so the provider is chosen in one place.
    `USE_REAL_LLM=false` swaps in the mock for local work and tests without
    touching call sites.
    """
    if not settings.USE_REAL_LLM:
        from .mock_llm import MockLLMProvider

        return MockLLMProvider()

    from .openrouter_llm import OpenRouterLLMProvider

    return OpenRouterLLMProvider(model=model)
