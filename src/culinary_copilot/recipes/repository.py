"""Parameterized full-text search and complete source retrieval.

Canonical recipe identity is the ``(dataset_id, source_id)`` pair. Supported
imported datasets are :data:`SUPPORTED_DATASETS`. ``source_id`` values are
always compared exactly: no prefix stripping, integer conversion, or
zero-padding removal.

Matching semantics:

- Dish queries: the full-text query decides eligibility. Available pantry
  terms (``rank_pantry_terms``) only influence ranking through a separate
  OR-scored boost in ``ORDER BY``; a dish match with zero pantry overlap
  remains eligible.
- Pantry-only queries (``match_any_ingredients``): a recipe is eligible when
  its canonical ingredient list overlaps at least one available ingredient
  (``&&`` on exact canonical names, so multiword meaning such as
  "olive oil" is preserved). Results rank by overlap count, then text
  score, then identity. A match never implies the recipe needs no
  additional ingredients.
- Explicit required-ingredient filters (``ingredients``) stay mandatory
  containment (``@>``) wherever supplied.

Duration policy (shared with evidence via :mod:`culinary_copilot.recipes.durations`):

- Only a finite positive ``total_minutes`` can satisfy ``max_minutes``.
  Missing, unverified zero, negative, and non-finite totals are unknown and
  excluded by a ceiling. Without a ceiling no duration filtering applies.
  Eligibility is applied in SQL before ranking and ``LIMIT``.
"""

import math
from typing import Any

from sqlalchemy import Engine, text

from culinary_copilot.recipes.adapters.foodie import FOODIE_DATASET
from culinary_copilot.recipes.import_data import DATASET
from culinary_copilot.recipes.normalize import canonical

FOODCOM_DATASET = DATASET
SUPPORTED_DATASETS: frozenset[str] = frozenset({DATASET, FOODIE_DATASET})

# Pantry ranking boost weight: the dish/text score stays dominant so pantry
# overlap reorders similarly scored rows without overriding eligibility.
PANTRY_RANK_WEIGHT = 0.25


def _validate_search_args(query: str, limit: int, max_minutes: float | None) -> None:
    if not query.strip() or not 1 <= limit <= 50:
        raise ValueError("Provide a query and limit between 1 and 50")
    if max_minutes is not None and (
        not isinstance(max_minutes, (int, float))
        or not math.isfinite(max_minutes)
        or max_minutes <= 0
    ):
        raise ValueError("max_minutes must be a finite positive number")


def _validate_dataset_id(dataset_id: str) -> None:
    if not dataset_id.strip() or dataset_id not in SUPPORTED_DATASETS:
        raise ValueError(f"Unsupported dataset_id; expected one of {sorted(SUPPORTED_DATASETS)}")


def _search(
    engine: Engine,
    query: str,
    *,
    ingredients: list[str] | None,
    max_minutes: float | None,
    limit: int,
    dataset_id: str | None,
    match_any_ingredients: list[str] | None = None,
    rank_pantry_terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    _validate_search_args(query, limit, max_minutes)
    if dataset_id is not None:
        _validate_dataset_id(dataset_id)
    match_any = [canonical(i) for i in match_any_ingredients or [] if str(i).strip()]
    pantry = [canonical(i) for i in rank_pantry_terms or [] if str(i).strip()]
    dataset_filter = "AND dataset_id=:dataset" if dataset_id is not None else ""
    params: dict[str, Any] = {
        "query": query,
        "limit": limit,
        "ingredients": [canonical(i) for i in ingredients or []],
        "minutes": max_minutes,
        "match_any": match_any,
        "pantry": pantry,
    }
    if dataset_id is not None:
        params["dataset"] = dataset_id
    if match_any:
        select_extra = """,
                    (SELECT count(*) FROM unnest(ingredient_names) n
                     WHERE n = ANY(CAST(:match_any AS text[]))) AS pantry_overlap"""
        match_clause = "ingredient_names && CAST(:match_any AS text[])"
        order_clause = "ORDER BY pantry_overlap DESC, score DESC, dataset_id, source_id"
    else:
        select_extra = ""
        match_clause = "search_vector @@ plainto_tsquery('english', :query)"
        if pantry:
            # The rank expression repeats the SELECT-list ts_rank call
            # instead of the `score` alias, which PostgreSQL rejects
            # inside ORDER BY expressions.
            order_clause = """ORDER BY (ts_rank_cd(
                        search_vector, plainto_tsquery('english', :query))
                      + CAST(:pantry_weight AS double precision)
                      * COALESCE((SELECT max(ts_rank_cd(
                            search_vector, plainto_tsquery('english', term)))
                        FROM unnest(CAST(:pantry AS text[])) AS term), 0))
                      DESC, dataset_id, source_id"""
            params["pantry_weight"] = PANTRY_RANK_WEIGHT
        else:
            order_clause = "ORDER BY score DESC, dataset_id, source_id"
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
            SELECT dataset_id, source_id, title, total_minutes, servings,
                    document->'flags' AS flags,
                    ts_rank_cd(search_vector, plainto_tsquery('english', :query)) AS score
                    {select_extra}
            FROM recipes
            WHERE {match_clause}
              {dataset_filter}
              AND ingredient_names @> CAST(:ingredients AS text[])
              AND (CAST(:minutes AS double precision) IS NULL
                    OR (total_minutes > 0 AND total_minutes <= :minutes
                        AND total_minutes <> 'Infinity'::float8
                        AND total_minutes = total_minutes))
            {order_clause} LIMIT :limit
        """),
            params,
        )
        return [dict(row) for row in rows.mappings()]


def search_recipes(
    engine: Engine,
    query: str,
    *,
    ingredients: list[str] | None = None,
    max_minutes: float | None = None,
    limit: int = 5,
    dataset_id: str | None = None,
    match_any_ingredients: list[str] | None = None,
    rank_pantry_terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Keyword search within one dataset.

    ``dataset_id=None`` preserves the legacy Food.com-only default
    (``DATASET``); pass an explicit supported dataset to scope the search.
    Use :func:`search_all` for cross-dataset discovery. An explicit
    ``dataset_id`` must belong to :data:`SUPPORTED_DATASETS`.

    Ordering is deterministic: full-text score first (plus an optional
    pantry ranking boost that never affects eligibility), then
    ``(dataset_id, source_id)`` so score ties are stable. In
    pantry-overlap mode (``match_any_ingredients``) overlap count leads,
    then score, then identity. When ``max_minutes`` is given, only finite
    positive durations can satisfy it; missing, zero, negative, and
    non-finite totals are excluded.
    """
    resolved = dataset_id if dataset_id is not None else DATASET
    return _search(
        engine,
        query,
        ingredients=ingredients,
        max_minutes=max_minutes,
        limit=limit,
        dataset_id=resolved,
        match_any_ingredients=match_any_ingredients,
        rank_pantry_terms=rank_pantry_terms,
    )


def search_all(
    engine: Engine,
    query: str,
    *,
    ingredients: list[str] | None = None,
    max_minutes: float | None = None,
    limit: int = 5,
    match_any_ingredients: list[str] | None = None,
    rank_pantry_terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Cross-dataset discovery search (no dataset filter).

    Applies the same filters, result shape, and deterministic ordering as
    :func:`search_recipes`: score first (plus an optional pantry ranking
    boost), then ``(dataset_id, source_id)``; overlap count leads in
    pantry-overlap mode. When ``max_minutes`` is given, only finite
    positive durations can satisfy it. Every row includes ``dataset_id``
    and ``source_id``.
    """
    return _search(
        engine,
        query,
        ingredients=ingredients,
        max_minutes=max_minutes,
        limit=limit,
        dataset_id=None,
        match_any_ingredients=match_any_ingredients,
        rank_pantry_terms=rank_pantry_terms,
    )


def get_recipe(
    engine: Engine, source_id: str, *, dataset_id: str | None = None
) -> dict[str, Any] | None:
    """Dataset-aware lookup.

    Compatibility: ``get_recipe(engine, "000038")`` still finds the Food.com
    record (tries the legacy dataset first, then any dataset). New code should
    pass ``dataset_id`` explicitly. The original zero-padded ``source_id`` is
    never mutated for comparison; cross-dataset integer-ID matching (e.g. 38)
    must use a separate comparison key, not global zero-stripping.

    With ``dataset_id``, lookup matches the exact ``(dataset_id, source_id)``
    pair and never falls back to another dataset. ``dataset_id`` must belong
    to :data:`SUPPORTED_DATASETS`. Duplicate alias IDs remain provenance
    metadata; only canonical records are addressable here.
    """
    if dataset_id is not None:
        _validate_dataset_id(dataset_id)
    with engine.connect() as conn:
        if dataset_id is not None:
            result = conn.execute(
                text(
                    "SELECT document FROM recipes "
                    "WHERE dataset_id=:dataset AND source_id=:source_id"
                ),
                {"dataset": dataset_id, "source_id": source_id},
            ).scalar_one_or_none()
            return dict(result) if result is not None else None
        result = conn.execute(
            text("SELECT document FROM recipes WHERE dataset_id=:dataset AND source_id=:source_id"),
            {"dataset": DATASET, "source_id": source_id},
        ).scalar_one_or_none()
        if result is not None:
            return dict(result)
        fallback = conn.execute(
            text(
                "SELECT document FROM recipes WHERE source_id=:source_id "
                "ORDER BY dataset_id LIMIT 1"
            ),
            {"source_id": source_id},
        ).scalar_one_or_none()
        return dict(fallback) if fallback is not None else None
