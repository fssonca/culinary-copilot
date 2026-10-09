"""Technique corpus retrieval (Milestone 3, Phase 4).

Full-text chunk search over ``technique_chunks`` (migration 006) joined
to ``technique_documents`` for provenance, plus exact cosine vector
search over ``technique_embeddings`` (migration 007). Every hit carries
its attribution text and licence link (owner share-alike condition):
no excerpt is ever emitted without both.

Document-level identity is ``doc_id``; chunk identity is
``(doc_id, chunk_id)``. Technique references are a separate evidence
type from recipe identities ``(dataset_id, source_id)``.
"""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy import Engine, text

from culinary_copilot.embeddings.provider import to_pgvector_literal
from culinary_copilot.embeddings.technique_rendering import (
    TECHNIQUE_CHUNK_VERSION,
    TECHNIQUE_RENDER_VERSION,
)

#: Bounded excerpt length per hit (characters).
TECHNIQUE_EXCERPT_CHARS = 600

#: Vector cosine-distance cutoff, mirroring ``search_recipes`` (0.66).
TECHNIQUE_VECTOR_CUTOFF = 0.66


class TechniqueNotConfiguredError(Exception):
    """The technique corpus tables are missing (migration 006 pending)."""


def _missing_table(exc: BaseException) -> bool:
    """True when the error names a missing technique table (any backend)."""
    message = f"{type(exc).__name__}: {exc}".lower()
    return (
        "technique_documents" in message
        or "technique_chunks" in message
        or ("technique_embeddings" in message)
    )


def technique_tables_exist(engine: Engine) -> bool:
    """True when migration 006 tables exist (no exception into callers)."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM technique_documents LIMIT 1"))
        return True
    except Exception:
        return False


def resolve_technique_chunk(engine: Engine, doc_id: str, chunk_id: int) -> dict[str, Any] | None:
    """Resolve one technique chunk to its evidence row (None when absent)."""
    try:
        with engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        """
                    SELECT c.doc_id, c.chunk_id, c.section, c.chunk_text,
                           d.title, d.url, d.licence, d.licence_url,
                           d.attribution_text
                    FROM technique_chunks c
                    JOIN technique_documents d USING (doc_id)
                    WHERE c.doc_id=:doc_id AND c.chunk_id=:chunk_id
                    """
                    ),
                    {"doc_id": doc_id, "chunk_id": chunk_id},
                )
                .mappings()
                .first()
            )
    except Exception:
        return None
    return dict(row) if row is not None else None


def excerpt(chunk_text: str, limit: int = TECHNIQUE_EXCERPT_CHARS) -> str:
    """Bounded excerpt; every caller also receives attribution fields."""
    cleaned = " ".join((chunk_text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "…"


def or_tsquery_text(all_terms_text: str) -> str:
    """Rewrite an AND tsquery text (``a & b``) as OR (``a | b``).

    Pure helper so the fallback is unit-testable without a database.
    Single-term queries have no ``&`` and pass through unchanged.
    """
    return all_terms_text.replace(" & ", " | ")


_FULLTEXT_SQL = """
SELECT c.doc_id, c.chunk_id, c.section, c.chunk_text,
       d.title, d.url, d.licence, d.licence_url,
       d.attribution_text,
       ts_rank_cd(c.search_vector, {tsquery}) AS rank
FROM technique_chunks c
JOIN technique_documents d USING (doc_id)
WHERE c.search_vector @@ {tsquery}
ORDER BY rank DESC, c.doc_id, c.chunk_id
LIMIT :limit
"""

#: Poultry species stems that also match the corpus word "poultry"
#: (2026-10-06 live fix): the food-safety documents say "poultry", so
#: "chicken safe internal temperature" missed them under AND matching
#: and the plan's required food-safety citation was never found. The
#: expansion only adds an alternative; it never drops a query term.
POULTRY_STEMS = ("chicken", "turkey", "duck", "goos", "gees", "hen")


def _with_poultry_synonyms(tsquery_text_sql: str) -> str:
    expr = tsquery_text_sql
    for stem in POULTRY_STEMS:
        expr = f"replace({expr}, '''{stem}''', '( ''{stem}'' | ''poultri'' )')"
    return expr


_PARSED_TEXT = _with_poultry_synonyms("plainto_tsquery('english', :query)::text")
_TSQUERY_ALL = f"({_PARSED_TEXT})::tsquery"
# OR fallback: same lexemes, any-term match. Built by rewriting the
# parsed AND tsquery text so lexing/stemming stay identical.
_TSQUERY_ANY = f"replace({_PARSED_TEXT}, ' & ', ' | ')::tsquery"


def _run_fulltext(conn: Any, query: str, limit: int, tsquery: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(_FULLTEXT_SQL.format(tsquery=tsquery)), {"query": query, "limit": limit}
    )
    return [
        {
            "doc_id": row.doc_id,
            "chunk_id": row.chunk_id,
            "section": row.section,
            "title": row.title,
            "url": row.url,
            "licence": row.licence,
            "licence_url": row.licence_url,
            "attribution_text": row.attribution_text,
            "excerpt": excerpt(row.chunk_text),
        }
        for row in rows.mappings()
    ]


def search_techniques_fulltext(
    engine: Engine, query: str, *, limit: int = 5
) -> tuple[list[dict[str, Any]], str]:
    """Rank technique chunks; returns (hits, match) with match all|any.

    Mirrors the recipe full-text contract (``plainto_tsquery`` AND per
    chunk, ``ts_rank_cd`` ordering): every query term must occur in one
    chunk first (``match="all"``). When nothing matches, the same
    lexemes are retried with OR semantics (``match="any"``) so a long
    natural-language query still returns the best partial matches
    instead of an empty result. Raises TechniqueNotConfiguredError
    when the 006 tables are missing.
    """
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    try:
        with engine.connect() as conn:
            hits = _run_fulltext(conn, query, limit, _TSQUERY_ALL)
            if hits:
                return hits, "all"
            return _run_fulltext(conn, query, limit, _TSQUERY_ANY), "any"
    except Exception as exc:
        if _missing_table(exc):
            raise TechniqueNotConfiguredError(
                "technique corpus not loaded (migration 006 pending)"
            ) from exc
        raise


def technique_vector_candidates(
    engine: Engine,
    query_vector: list[float],
    *,
    limit: int = 5,
    model: str = "text-embedding-3-small",
    dimension: int = 1536,
) -> list[dict[str, Any]]:
    """Exact cosine search over current technique chunk embeddings."""
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    if len(query_vector) != dimension or not all(math.isfinite(float(v)) for v in query_vector):
        raise ValueError(f"query vector must be {dimension} finite floats")
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT c.doc_id, c.chunk_id, c.section, c.chunk_text,
                           d.title, d.url, d.licence, d.licence_url,
                           d.attribution_text,
                           (e.embedding <=> CAST(:query_vector AS vector)) AS distance
                    FROM technique_embeddings e
                    JOIN technique_chunks c USING (doc_id, chunk_id)
                    JOIN technique_documents d USING (doc_id)
                    WHERE e.model=:model AND e.dimension=:dimension
                      AND e.renderer_version=:renderer
                      AND e.chunking_version=:chunking
                    ORDER BY distance ASC, c.doc_id, c.chunk_id
                    LIMIT :limit
                    """
                ),
                {
                    "query_vector": to_pgvector_literal(query_vector),
                    "limit": limit,
                    "model": model,
                    "dimension": dimension,
                    "renderer": TECHNIQUE_RENDER_VERSION,
                    "chunking": TECHNIQUE_CHUNK_VERSION,
                },
            )
            out: list[dict[str, Any]] = []
            for row in rows.mappings():
                mapping = dict(row)
                distance_value = mapping.pop("distance", None)
                if isinstance(distance_value, bool) or not isinstance(
                    distance_value, (int, float, str)
                ):
                    mapping["distance"] = 0.0
                else:
                    mapping["distance"] = float(distance_value)
                item = mapping
                item["excerpt"] = excerpt(str(item.get("chunk_text") or ""))
                item.pop("chunk_text", None)
                out.append(item)
            return out
    except Exception as exc:
        if _missing_table(exc):
            raise TechniqueNotConfiguredError(
                "technique embeddings not loaded (migration 007 pending)"
            ) from exc
        raise


def apply_technique_vector_cutoff(
    rows: list[dict[str, Any]],
    cutoff: float | None = TECHNIQUE_VECTOR_CUTOFF,
) -> list[dict[str, Any]]:
    """Keep vector candidates with cosine distance <= cutoff."""
    if cutoff is None:
        return list(rows)
    kept: list[dict[str, Any]] = []
    for row in rows:
        raw_distance: Any = row.get("distance")
        if isinstance(raw_distance, bool) or not isinstance(raw_distance, (int, float, str)):
            continue
        try:
            distance = float(raw_distance)
        except (TypeError, ValueError):
            continue
        if math.isfinite(distance) and distance <= float(cutoff):
            kept.append(row)
    return kept


__all__ = [
    "TECHNIQUE_EXCERPT_CHARS",
    "TECHNIQUE_VECTOR_CUTOFF",
    "TechniqueNotConfiguredError",
    "apply_technique_vector_cutoff",
    "excerpt",
    "or_tsquery_text",
    "resolve_technique_chunk",
    "search_techniques_fulltext",
    "technique_tables_exist",
    "technique_vector_candidates",
]
