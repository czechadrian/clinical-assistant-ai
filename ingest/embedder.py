"""
embedder.py — provider-agnostic embedding interface.

Provider choice: OpenAI text-embedding-3-small
  - Anthropic has no standalone embedding API (Claude models are generation-only).
  - text-embedding-3-small produces 1536-dimensional vectors, matching the
    existing doc_chunks.embedding column (vector(1536)).
  - Cheapest high-quality option (~$0.02 / 1M tokens); a typical 50-page PDF
    costs less than $0.01 to embed.
  - The OpenAI SDK handles retry and timeouts natively.

To swap providers: implement a class that satisfies the Embedder protocol and
replace `OpenAIEmbedder(...)` at the call sites. No other code changes needed.
"""

from typing import Protocol, runtime_checkable

from openai import AsyncOpenAI

# Stay well within the API limit of 2048 inputs per request.
# 100 is a safe default that also limits per-batch memory usage.
_EMBED_BATCH_SIZE = 100


@runtime_checkable
class Embedder(Protocol):
    """Minimal interface any embedding provider must implement."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text, in the same order as *texts*."""
        ...

    async def aclose(self) -> None:
        """Release any held resources (HTTP connections, thread pools, etc.)."""
        ...


class OpenAIEmbedder:
    """OpenAI embeddings via AsyncOpenAI.  Satisfies the Embedder protocol.

    Retry policy is delegated to the OpenAI SDK (max_retries=2 covers transient
    5xx and rate-limit errors).  No separate retry wrapper is needed because the
    SDK uses exponential backoff internally, consistent with the Day 12 policy.
    """

    def __init__(self, api_key: str, model: str = "text-embedding-3-small") -> None:
        self._model = model
        self._client = AsyncOpenAI(
            api_key=api_key,
            max_retries=2,
            timeout=30.0,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts* in batches of _EMBED_BATCH_SIZE.

        Returns a list of float vectors in the same order as the input.
        Empty input returns an empty list without making any API calls.
        """
        if not texts:
            return []
        results: list[list[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH_SIZE):
            batch = texts[i : i + _EMBED_BATCH_SIZE]
            response = await self._client.embeddings.create(
                model=self._model,
                input=batch,
            )
            # Sort by index: the API does not guarantee response order matches input.
            ordered = sorted(response.data, key=lambda e: e.index)
            results.extend(e.embedding for e in ordered)
        return results

    async def aclose(self) -> None:
        await self._client.close()
