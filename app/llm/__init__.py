"""LLM provider abstraction.

`get_llm_provider()` returns the configured backend. Azure OpenAI is the
out-of-the-box default; set LLM_PROVIDER=stub for offline development.
"""

import logging
from functools import lru_cache

from app.config import settings
from app.llm.base import LLMError, LLMProvider, LLMRefusal
from app.llm.stub import StubProvider

logger = logging.getLogger("company_brain.llm")

__all__ = ["LLMProvider", "LLMError", "LLMRefusal", "get_llm_provider"]


@lru_cache
def get_llm_provider() -> LLMProvider:
    provider = settings.llm_provider.lower()
    if provider == "stub":
        logger.info("Using StubProvider (offline, deterministic embeddings).")
        return StubProvider()
    if provider == "azure":
        # Imported lazily so the stub path has no hard dependency on the openai SDK.
        from app.llm.azure import AzureOpenAIProvider

        return AzureOpenAIProvider(settings)
    if provider == "anthropic":
        from app.llm.anthropic import AnthropicProvider

        return AnthropicProvider(settings)
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")
