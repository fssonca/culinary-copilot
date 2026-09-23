"""Parameterized full-text search and complete source retrieval.

Canonical recipe identity is the ``(dataset_id, source_id)`` pair. Supported
imported datasets are :data:`SUPPORTED_DATASETS`. ``source_id`` values are
always compared exactly: no prefix stripping, integer conversion, or
zero-padding removal.
"""

import math
from typing import Any

from sqlalchemy import Engine, text

from culinary_copilot.recipes.adapters.foodie import FOODIE_DATASET
from culinary_copilot.recipes.import_data import DATASET
from culinary_copilot.recipes.normalize import canonical

FOODCOM_DATASET = DATASET
SUPPORTED_DATASETS: frozenset[str] = frozenset({DATASET, FOODIE_DATASET})


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
) -> list[dict[str, Any]]:
    _validate_search_args(query, limit, max_minutes)
    if dataset_id is not None:
        _validate_dataset_id(dataset_id)
    dataset_filter = "AND dataset_id=:dataset" if dataset_id is not None else ""
    params: dict[str, Any] = {
        "query": query,
        "limit": limit,
        "ingredients": [canonical(i) for i in ingredients or []],
        "minutes": max_minutes,
    }
    if dataset_id is not None:
        params["dataset"] = dataset_id
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
            SELECT dataset_id, source_id, title, total_minutes, servings,
                   document->'flags' AS flags,
                   ts_rank_cd(search_vector, plainto_tsquery('english', :query)) AS score
            FROM recipes
            WHERE search_vector @@ plainto_tsquery('english', :query)
              {dataset_filter}
              AND ingredient_names @> CAST(:ingredients AS text[])
              AND (CAST(:minutes AS double precision) IS NULL
                   OR (total_minutes IS NOT NULL AND total_minutes <= :minutes))
            ORDER BY score DESC, dataset_id, source_id LIMIT :limit
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
) -> list[dict[str, Any]]:
    """Keyword search within one dataset.

    ``dataset_id=None`` preserves the legacy Food.com-only default
    (``DATASET``); pass an explicit supported dataset to scope the search.
    Use :func:`search_all` for cross-dataset discovery. An explicit
    ``dataset_id`` must belong to :data:`SUPPORTED_DATASETS`.

    Ordering is deterministic: full-text score first, then
    ``(dataset_id, source_id)`` so score ties are stable. When
    ``max_minutes`` is given, recipes with an unknown duration are excluded.
    """
    resolved = dataset_id if dataset_id is not None else DATASET
    return _search(
        engine,
        query,
        ingredients=ingredients,
        max_minutes=max_minutes,
        limit=limit,
        dataset_id=resolved,
    )


def search_all(
    engine: Engine,
    query: str,
    *,
    ingredients: list[str] | None = None,
    max_minutes: float | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Cross-dataset discovery search (no dataset filter).

    Applies the same filters, result shape, and deterministic ordering as
    :func:`search_recipes`: score first, then ``(dataset_id, source_id)``.
    When ``max_minutes`` is given, recipes with an unknown duration are
    excluded. Every row includes ``dataset_id`` and ``source_id``.
    """
    return _search(
        engine,
        query,
        ingredients=ingredients,
        max_minutes=max_minutes,
        limit=limit,
        dataset_id=None,
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
