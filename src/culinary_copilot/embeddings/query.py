"""Async query embedding, outside the synchronous SQL repository.

The repository stays synchronous (``asyncio.to_thread`` at the service
boundary, as in Phase 1). Query text is embedded with the same model and
dimension as the corpus; mismatches fail closed before any call.
``allow_fallback`` selects the unavailable-outcome contract: False fails
closed with ``EmbeddingUnavailableError``; True returns None so the
caller can use full-text IF it discloses the fallback explicitly.
Full-text mode never calls this helper.
"""

from __future__ import annotations

import asyncio

from culinary_copilot.embeddings.provider import (
    EmbeddingProvider,
    EmbeddingUnavailableError,
    check_model_dimension,
)


async def embed_query(
    provider: EmbeddingProvider | None,
    query_text: str,
    *,
    model: str,
    dimension: int,
    timeout_s: float = 20.0,
    allow_fallback: bool = False,
) -> list[float] | None:
    """Embed one query string; None only when fallback is explicitly allowed."""
    check_model_dimension(model, dimension)
    cleaned = " ".join(query_text.split())
    if not cleaned:
        raise EmbeddingUnavailableError("empty query text")
    if provider is None:
        if allow_fallback:
            return None
        raise EmbeddingUnavailableError("embeddings unavailable: no provider")
    try:
        result = await asyncio.wait_for(provider.embed_texts([cleaned]), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        if allow_fallback:
            return None
        raise EmbeddingUnavailableError("query embedding timed out") from exc
    if len(result.vectors) != 1 or len(result.vectors[0]) != dimension:
        raise EmbeddingUnavailableError("query embedding dimension mismatch")
    return result.vectors[0]
