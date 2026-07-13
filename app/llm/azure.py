"""Azure OpenAI provider — the out-of-the-box backend.

Uses the official `openai` SDK. Structured extraction goes through
`client.beta.chat.completions.parse()` (requires api-version >= 2024-08-01 and
a structured-output-capable deployment, e.g. gpt-4o-2024-08-06+). Embeddings go
through a separate `text-embedding-3-*` deployment.
"""

import logging
from typing import TypeVar

from openai import AzureOpenAI
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import Settings
from app.llm.base import LLMRefusal

logger = logging.getLogger("company_brain.llm.azure")

T = TypeVar("T", bound=BaseModel)


class AzureOpenAIProvider:
    def __init__(self, settings: Settings):
        missing = [
            k for k in ("azure_openai_endpoint", "azure_openai_api_key") if not getattr(settings, k)
        ]
        if missing:
            raise ValueError(
                f"Azure OpenAI is not configured (missing: {', '.join(missing)}). "
                "Set the AZURE_OPENAI_* env vars, or use LLM_PROVIDER=stub for offline dev."
            )
        self._client = AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
        )
        self._embedding_deployment = settings.azure_openai_embedding_deployment
        self._chat_deployment = settings.azure_openai_chat_deployment

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20), reraise=True)
    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(
            model=self._embedding_deployment,
            input=texts,
        )
        # The API preserves input order; sort defensively on index anyway.
        return [item.embedding for item in sorted(resp.data, key=lambda d: d.index)]

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20), reraise=True)
    def extract(self, system_prompt: str, user_content: str, schema: type[T]) -> T:
        completion = self._client.beta.chat.completions.parse(
            model=self._chat_deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format=schema,
            temperature=0,
        )
        message = completion.choices[0].message
        if getattr(message, "refusal", None):
            raise LLMRefusal(message.refusal)
        if message.parsed is None:
            raise LLMRefusal("Model returned no parsed structured output.")
        return message.parsed
