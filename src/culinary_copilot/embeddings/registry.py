"""Supported embedding models: separate registry from text generation.

The Luna-only registry in ``llm/models.py`` governs generation and must
never be reused for embeddings. Query and corpus vectors must use the
same entry here; configuration refuses anything else.

Verified 2026-09-24:
- https://developers.openai.com/api/docs/guides/embeddings (3-small 1536,
  3-large 3072, dimensions param on 3-series, cosine recommended,
  normalized vectors, cl100k_base for 3-series token counting).
- https://developers.openai.com/api/reference/resources/embeddings/methods/create
  (per-input 8192 tokens, aggregate 300k per request, <=2048 inputs,
  per-index mapping, usage block).
- https://developers.openai.com/api/docs/pricing (Standard per 1M input
  tokens: 3-small $0.02, 3-large $0.13, ada-002 $0.10).
Re-verify before any live run and bump ``EMBED_PRICING_VERSION`` on change.
"""

from __future__ import annotations

from dataclasses import dataclass

EMBED_PRICING_VERSION = "2026-09-24-embed-v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


@dataclass(frozen=True)
class EmbeddingPrices:
    input_per_1m: float
    source_url: str
    date_verified: str


@dataclass(frozen=True)
class EmbeddingSpec:
    name: str
    dimension: int
    max_input_tokens: int
    max_inputs_per_request: int
    max_total_tokens_per_request: int
    prices: EmbeddingPrices
    docs_url: str


SUPPORTED_EMBEDDING_MODELS: dict[str, EmbeddingSpec] = {
    "text-embedding-3-small": EmbeddingSpec(
        name="text-embedding-3-small",
        dimension=1536,
        max_input_tokens=8192,
        max_inputs_per_request=2048,
        max_total_tokens_per_request=300_000,
        prices=EmbeddingPrices(
            input_per_1m=0.02,
            source_url="https://developers.openai.com/api/docs/pricing",
            date_verified="2026-09-24",
        ),
        docs_url="https://developers.openai.com/api/docs/guides/embeddings",
    ),
    "text-embedding-3-large": EmbeddingSpec(
        name="text-embedding-3-large",
        dimension=3072,
        max_input_tokens=8192,
        max_inputs_per_request=2048,
        max_total_tokens_per_request=300_000,
        prices=EmbeddingPrices(
            input_per_1m=0.13,
            source_url="https://developers.openai.com/api/docs/pricing",
            date_verified="2026-09-24",
        ),
        docs_url="https://developers.openai.com/api/docs/guides/embeddings",
    ),
    "text-embedding-ada-002": EmbeddingSpec(
        name="text-embedding-ada-002",
        dimension=1536,
        max_input_tokens=8192,
        max_inputs_per_request=2048,
        max_total_tokens_per_request=300_000,
        prices=EmbeddingPrices(
            input_per_1m=0.10,
            source_url="https://developers.openai.com/api/docs/pricing",
            date_verified="2026-09-24",
        ),
        docs_url="https://developers.openai.com/api/docs/guides/embeddings",
    ),
}


def embedding_spec(name: str | None) -> EmbeddingSpec | None:
    """Registry entry for ``name``, or None when unsupported."""
    if not name:
        return None
    return SUPPORTED_EMBEDDING_MODELS.get(name)


def require_supported_embedding_model(name: str) -> str:
    """Return ``name`` if supported; raise ValueError otherwise."""
    if name not in SUPPORTED_EMBEDDING_MODELS:
        raise ValueError(
            f"Unsupported embedding model {name!r}; "
            f"supported: {sorted(SUPPORTED_EMBEDDING_MODELS)}. "
            "Add a verified entry to culinary_copilot/embeddings/registry.py."
        )
    return name


def estimate_cost_usd(tokens: int | None, model: str | None) -> float | None:
    """Estimated embedding cost, or None when tokens/pricing unknown."""
    if tokens is None:
        return None
    spec = embedding_spec(model)
    if spec is None:
        return None
    return tokens / 1e6 * spec.prices.input_per_1m
