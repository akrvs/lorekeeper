"""Anthropic (Claude) provider — extraction on Claude, embeddings delegated.

Structured extraction goes through the official `anthropic` SDK's
`client.messages.parse()`, which constrains the response to the Pydantic
schema (structured outputs) and returns a validated instance.

Anthropic has no embeddings endpoint, so `embed()` delegates:
  * if an Azure `text-embedding-3-*` deployment is configured, use it
    (hybrid: Claude for extraction quality, Azure for vectors);
  * otherwise fall back to the deterministic offline embeddings from the
    stub — semantic search degrades to lexical-ish matching, but the graph,
    dedup-by-name, and every MCP tool keep working.
"""

import logging
from typing import TypeVar

import anthropic
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import Settings
from app.llm.base import LLMError, LLMRefusal
from app.llm.stub import StubProvider

logger = logging.getLogger("company_brain.llm.anthropic")

T = TypeVar("T", bound=BaseModel)


class AnthropicProvider:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None):
        # The SDK resolves credentials from the environment (ANTHROPIC_API_KEY,
        # auth token, or an `ant auth login` profile) when no key is passed.
        self._client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key or None)
        self._model = settings.anthropic_model
        self._max_tokens = settings.anthropic_max_tokens

        if settings.azure_openai_endpoint and settings.azure_openai_api_key:
            from app.llm.azure import AzureOpenAIProvider

            self._embedder = AzureOpenAIProvider(settings)
            logger.info("AnthropicProvider: embeddings via Azure deployment.")
        else:
            self._embedder = StubProvider(settings.embedding_dim)
            logger.warning(
                "AnthropicProvider: no embedding backend configured — using "
                "deterministic offline embeddings (set AZURE_OPENAI_* for real vectors)."
            )

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._embedder.embed(texts)

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20), reraise=True)
    def extract(self, system_prompt: str, user_content: str, schema: type[T]) -> T:
        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
                output_format=schema,
            )
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Anthropic request rejected: {exc.message}") from exc

        if response.stop_reason == "refusal":
            raise LLMRefusal("Claude declined to produce structured output for this document.")
        if response.stop_reason == "max_tokens":
            raise LLMError(
                "Extraction truncated at max_tokens "
                f"({self._max_tokens}) — raise ANTHROPIC_MAX_TOKENS."
            )
        if response.parsed_output is None:
            raise LLMRefusal("Model returned no parsed structured output.")
        return response.parsed_output
