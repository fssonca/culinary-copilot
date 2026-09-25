"""Opt-in vector/hybrid orchestration (Phase 5); full-text stays the default.

``mode="fulltext"`` makes zero embedding calls. ``vector``/``hybrid``
embed the query async (outside the sync repository) with the configured
model/dimension, run exact cosine search with freshness filters, and fuse
with RRF after chunk→recipe aggregation. Unavailable embeddings fail
closed unless ``allow_fallback`` is set, in which case the caller must
disclose the full-text fallback — never silently.
"""

from __future__ import annotations

import asyncio
from typing import Any

from culinary_copilot.embeddings.provider import EmbeddingProvider
from culinary_copilot.embeddings.query import embed_query


async def retrieve_with_mode(
    engine: Any,
    query: Any,
    *,
    limit: int,
    mode: str = "fulltext",
    provider: EmbeddingProvider | None = None,
    model: str = "text-embedding-3-small",
    dimension: int = 1536,
    timeout_s: float = 20.0,
    vector_candidates_n: int = 20,
    rrf_k: int = 60,
    allow_fallback: bool = False,
) -> dict[str, Any]:
    """Run one retrieval mode; returns rows plus mode metadata."""
    from culinary_copilot.recipes.vector_search import rrf_fuse, vector_candidates
    from culinary_copilot.retrieval.service import _search_sync

    if mode == "fulltext":
        rows = await asyncio.to_thread(_search_sync, engine, query, limit)
        return {"mode": "fulltext", "rows": rows, "fallback": None}
    if mode not in {"vector", "hybrid"}:
        raise ValueError("mode must be one of fulltext, vector, hybrid")
    query_vector = await embed_query(
        provider,
        query.query_text,
        model=model,
        dimension=dimension,
        timeout_s=timeout_s,
        allow_fallback=allow_fallback,
    )
    if query_vector is None:
        rows = await asyncio.to_thread(_search_sync, engine, query, limit)
        return {
            "mode": mode,
            "rows": rows,
            "fallback": "fulltext (embeddings unavailable, disclosed)",
        }
    vector_rows = await asyncio.to_thread(
        vector_candidates,
        engine,
        query_vector,
        ingredients=list(query.required_ingredients or []),
        max_minutes=query.max_minutes,
        limit=vector_candidates_n,
        dataset_id=query.dataset_id,
        match_any_ingredients=query.match_any_ingredients,
        rank_pantry_terms=list(query.rank_pantry_terms or []),
        model=model,
        dimension=dimension,
    )
    if mode == "vector":
        return {"mode": "vector", "rows": vector_rows, "fallback": None}
    fulltext_rows = await asyncio.to_thread(
        _search_sync, engine, query, max(limit, vector_candidates_n)
    )
    fused = rrf_fuse(fulltext_rows, vector_rows, k=rrf_k, limit=limit)
    return {"mode": "hybrid", "rows": fused, "fallback": None}
