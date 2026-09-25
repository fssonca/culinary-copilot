"""Versioned embedding-document rendering and chunking (Phase 5, v1).

Separate from full-text ``recipes/search.py`` (``SEARCH_DOCUMENT_VERSION``).
An embedding's reusable identity is::

    dataset_id + source_id + model + dimension
    + embed renderer version + chunking version
    + sha256 of the exact embedded text

Source provenance (import id, revision) is stored separately and never
part of the reuse identity: a renderer change must invalidate vectors
even when the source hash is unchanged. v1 emits exactly one chunk per
recipe (deterministic truncation); the schema and aggregation logic
already support multiple chunks per recipe version.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

EMBED_DOCUMENT_VERSION = "1"
CHUNKING_VERSION = "1"
EMBED_MAX_CHARS = 7000


def render_embed_text(recipe: Mapping[str, Any]) -> str:
    """Deterministic embed text from a normalized recipe dict."""
    title = " ".join(str(recipe.get("title") or "").split())
    description = " ".join(str(recipe.get("description") or "").split())
    ingredients = recipe.get("ingredients") or []
    names: list[str] = []
    for item in ingredients:
        if isinstance(item, Mapping):
            name = str(item.get("canonical") or item.get("name") or "").strip()
            if name:
                names.append(" ".join(name.split()))
    instructions = recipe.get("instructions") or []
    steps: list[str] = []
    if isinstance(instructions, list):
        for step in instructions:
            if isinstance(step, str) and step.strip():
                steps.append(" ".join(step.split()))
    parts: list[str] = []
    if title:
        parts.append(title)
    if description:
        parts.append(description)
    if names:
        parts.append("Ingredients: " + "; ".join(names))
    if steps:
        parts.append("Steps: " + " ".join(steps))
    return "\n".join(parts)


def chunk_embed_text(text: str) -> tuple[list[str], bool]:
    """Single-chunk v1: return ``([chunk], truncated)`` deterministically."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= EMBED_MAX_CHARS:
        return [cleaned], False
    return [cleaned[:EMBED_MAX_CHARS]], True


def embedded_text_hash(text: str) -> str:
    """SHA-256 of the exact embedded chunk text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens_bytes(text: str) -> int:
    """Conservative token UPPER BOUND for reservation (never an average).

    Uses the UTF-8 byte length plus a framing constant: the project rule
    (docs/recommendations.md) is that every token spans at least one byte,
    so the token count never exceeds the byte count. Character counts
    underestimate non-ASCII text (one code point can be several tokens),
    and must never size a spend reservation. Retry-inclusive reservations
    multiply by ``(max_retries + 1)`` at the call site; cumulative spend
    accumulates in the run ledger.
    """
    return max(1, len(text.encode("utf-8")) + 8)


def embedding_identity(
    *,
    dataset_id: str,
    source_id: str,
    model: str,
    dimension: int,
    chunk_index: int,
    embedded_text: str,
) -> dict[str, Any]:
    """Full reusable identity for one chunk embedding."""
    return {
        "dataset_id": dataset_id,
        "source_id": source_id,
        "model": model,
        "dimension": dimension,
        "renderer_version": EMBED_DOCUMENT_VERSION,
        "chunking_version": CHUNKING_VERSION,
        "chunk_index": chunk_index,
        "embedded_text_hash": embedded_text_hash(embedded_text),
    }
