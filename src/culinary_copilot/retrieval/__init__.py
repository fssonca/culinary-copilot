"""Phase 1 retrieval integration: ready requests to full recipe evidence.

Reads authoritative clarification state from the store, maps it
deterministically to a full-text query, and fetches bounded full source
documents via the existing repository. No embeddings or generation.
"""

from culinary_copilot.retrieval.query import RetrievalQuery, map_request_to_query
from culinary_copilot.retrieval.service import (
    DEFAULT_LIMIT,
    MAX_EXCERPT_CHARS,
    MAX_LIMIT,
    RetrievalNotFoundError,
    RetrievalStaleError,
    build_evidence,
    evaluate_readiness,
    retrieve_for_group,
)

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_EXCERPT_CHARS",
    "MAX_LIMIT",
    "RetrievalNotFoundError",
    "RetrievalQuery",
    "RetrievalStaleError",
    "build_evidence",
    "evaluate_readiness",
    "map_request_to_query",
    "retrieve_for_group",
]
