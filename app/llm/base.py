"""Provider-agnostic LLM interface.

The extraction engine depends only on this Protocol, so swapping Azure OpenAI
for another backend (or a stub in tests/offline) is a one-line change in the
factory. Two capabilities are needed:

    embed()   — text -> embedding vectors (for semantic search + dedup)
    extract() — text -> a Pydantic-validated structured object
"""

from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Base class for provider failures."""


class LLMRefusal(LLMError):
    """The model refused to produce structured output (safety refusal)."""


@runtime_checkable
class LLMProvider(Protocol):
    """The contract the extraction engine relies on."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input string (order preserved)."""
        ...

    def extract(self, system_prompt: str, user_content: str, schema: type[T]) -> T:
        """Run a structured-output completion and return a validated `schema`."""
        ...
