"""Recipe-text embeddings (Phase 5): separate from Epicure ingredient vectors."""

from culinary_copilot.embeddings.provider import (
    DisabledEmbeddingProvider,
    EmbeddingProvider,
    EmbeddingResult,
    EmbeddingUnavailableError,
    EmbeddingUsage,
    EmbeddingValidationError,
    FakeEmbeddingProvider,
    OpenAIEmbeddingProvider,
)
from culinary_copilot.embeddings.query import embed_query
from culinary_copilot.embeddings.registry import (
    DEFAULT_EMBEDDING_MODEL,
    EMBED_PRICING_VERSION,
    SUPPORTED_EMBEDDING_MODELS,
    embedding_spec,
    estimate_cost_usd,
    require_supported_embedding_model,
)
from culinary_copilot.embeddings.rendering import (
    CHUNKING_VERSION,
    EMBED_DOCUMENT_VERSION,
    EMBED_MAX_CHARS,
    chunk_embed_text,
    embedded_text_hash,
    embedding_identity,
    estimate_tokens_bytes,
    render_embed_text,
)

__all__ = [
    "CHUNKING_VERSION",
    "DEFAULT_EMBEDDING_MODEL",
    "EMBED_DOCUMENT_VERSION",
    "EMBED_MAX_CHARS",
    "EMBED_PRICING_VERSION",
    "SUPPORTED_EMBEDDING_MODELS",
    "DisabledEmbeddingProvider",
    "EmbeddingProvider",
    "EmbeddingResult",
    "EmbeddingUnavailableError",
    "EmbeddingUsage",
    "EmbeddingValidationError",
    "FakeEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "chunk_embed_text",
    "embedded_text_hash",
    "embedding_identity",
    "embedding_spec",
    "embed_query",
    "estimate_cost_usd",
    "estimate_tokens_bytes",
    "render_embed_text",
    "require_supported_embedding_model",
]
