"""Exact vector search and RRF hybrid retrieval (Phase 5, offline-capable).

Distance: ``<=>`` cosine distance, matching the official OpenAI embeddings
guidance (normalized vectors; cosine and Euclidean rank identically).
Exact search only — no HNSW/IVFFlat until measurements justify it.

Matching rules carried forward from full-text (repository.py):
- mandatory required-ingredient containment (``@>``) wherever supplied;
- pantry-only overlap eligibility (``&&``) with overlap-count ranking;
- dish + pantry: dish decides eligibility, pantry only boosts ranking;
- duration ceiling and dataset scoping applied identically in SQL.

The vector branch never requires a full-text match: eligibility is
mandatory ingredients + duration + dataset only, ordered by cosine
distance. Nearest-neighbor search returns the closest available rows
even for irrelevant queries, so callers must distinguish empty eligible
sets (no rows) from weak semantic matches (candidates returned, relevance
not established) — no similarity cutoff is invented here.

Multi-chunk: chunks aggregate to one recipe score BEFORE the vector
candidate limit, then RRF fuses the full-text and vector recipe rankings
(``score = sum(1 / (k + rank))``, ``k = RETRIEVAL_RRF_K``). Raw scores are
never averaged. Full sources are fetched afterward via ``get_recipe``.
"""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy import Engine, text

from culinary_copilot.embeddings.provider import to_pgvector_literal
from culinary_copilot.embeddings.rendering import CHUNKING_VERSION, EMBED_DOCUMENT_VERSION
from culinary_copilot.recipes.normalize import canonical
from culinary_copilot.recipes.repository import SUPPORTED_DATASETS

RRF_DEFAULT_K = 60


def _filters(
    *,
    ingredients: list[str] | None,
    max_minutes: float | None,
    dataset_id: str | None,
    match_any_ingredients: list[str] | None,
) -> tuple[str, dict[str, Any]]:
    if dataset_id is not None and dataset_id not in SUPPORTED_DATASETS:
        raise ValueError(f"Unsupported dataset_id; expected one of {sorted(SUPPORTED_DATASETS)}")
    match_any = [canonical(i) for i in match_any_ingredients or [] if str(i).strip()]
    clauses = [
        "ingredient_names @> CAST(:ingredients AS text[])",
        """(CAST(:minutes AS double precision) IS NULL
            OR (total_minutes > 0 AND total_minutes <= :minutes
                AND total_minutes <> 'Infinity'::float8
                AND total_minutes = total_minutes))""",
    ]
    params: dict[str, Any] = {
        "ingredients": [canonical(i) for i in ingredients or []],
        "minutes": max_minutes,
        "match_any": match_any,
    }
    if dataset_id is not None:
        clauses.append("r.dataset_id=:dataset")
        params["dataset"] = dataset_id
    if match_any:
        clauses.append("r.ingredient_names && CAST(:match_any AS text[])")
    return " AND ".join(clauses), params


def vector_candidates(
    engine: Engine,
    query_vector: list[float],
    *,
    ingredients: list[str] | None = None,
    max_minutes: float | None = None,
    limit: int = 20,
    dataset_id: str | None = None,
    match_any_ingredients: list[str] | None = None,
    rank_pantry_terms: list[str] | None = None,
    model: str = "text-embedding-3-small",
    dimension: int = 1536,
    renderer_version: str = EMBED_DOCUMENT_VERSION,
    chunking_version: str = CHUNKING_VERSION,
) -> list[dict[str, Any]]:
    """Exact cosine search; chunks aggregated to recipes before ``limit``.

    Stale rows are excluded in SQL: only the current model, dimension,
    renderer version, and chunking version are eligible. Text-hash freshness
    for the remaining rows is guaranteed by the CLI's atomic per-recipe
    replacement plus its re-read-before-commit guard (a source changed
    during embedding is never published as current).

    Pantry ranking mirrors full-text (repository.py): ``rank_pantry_terms``
    never decide eligibility — distance does — but recipes overlapping more
    pantry terms sort first at equal distance tiers. Ordering is
    ``(-pantry_overlap, distance)`` so the semantic signal stays dominant
    while pantry availability breaks ties and near-ties deterministically.
    """
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    if len(query_vector) != dimension or not all(math.isfinite(float(v)) for v in query_vector):
        raise ValueError(f"query vector must be {dimension} finite floats")
    where, params = _filters(
        ingredients=ingredients,
        max_minutes=max_minutes,
        dataset_id=dataset_id,
        match_any_ingredients=match_any_ingredients,
    )
    params["query_vector"] = to_pgvector_literal(query_vector)
    params["limit"] = limit * 3  # over-fetch chunks; aggregate before limit
    params["model"] = model
    params["dimension"] = dimension
    params["renderer"] = renderer_version
    params["chunking"] = chunking_version
    pantry = {canonical(t) for t in rank_pantry_terms or [] if str(t).strip()}
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
            SELECT r.dataset_id, r.source_id, r.title, r.ingredient_names,
                   min(e.embedding <=> CAST(:query_vector AS vector)) AS distance,
                   count(*) AS chunks
            FROM recipe_embeddings e
            JOIN recipes r USING (dataset_id, source_id)
            WHERE e.model=:model AND e.dimension=:dimension
              AND e.renderer_version=:renderer AND e.chunking_version=:chunking
              AND {where}
            GROUP BY r.dataset_id, r.source_id, r.title, r.ingredient_names
            ORDER BY distance ASC, r.dataset_id, r.source_id
            LIMIT :limit
        """),
            params,
        )
        out: list[dict[str, Any]] = []
        for row in rows.mappings():
            item = dict(row)
            names = {canonical(n) for n in (item.pop("ingredient_names") or [])}
            item["pantry_overlap"] = len(names & pantry) if pantry else 0
            out.append(item)
        out.sort(
            key=lambda r: (
                -int(r["pantry_overlap"]),
                float(r["distance"]),
                str(r["dataset_id"]),
                str(r["source_id"]),
            )
        )
        return out[:limit]


def rrf_fuse(
    fulltext: list[dict[str, Any]],
    vector: list[dict[str, Any]],
    *,
    k: int = RRF_DEFAULT_K,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Fuse two recipe rankings by reciprocal rank; chunks never inflate rank."""
    scores: dict[tuple[str, str], float] = {}
    keep: dict[tuple[str, str], dict[str, Any]] = {}
    for rank, row in enumerate(fulltext, start=1):
        key = (str(row.get("dataset_id")), str(row.get("source_id")))
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        keep.setdefault(key, dict(row))
    for rank, row in enumerate(vector, start=1):
        key = (str(row.get("dataset_id")), str(row.get("source_id")))
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        keep.setdefault(key, dict(row))
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    out: list[dict[str, Any]] = []
    for (dataset_id, source_id), score in ordered[:limit]:
        row = dict(keep[(dataset_id, source_id)])
        row["rrf_score"] = score
        out.append(row)
    return out
