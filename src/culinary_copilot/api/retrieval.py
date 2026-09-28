"""Retrieval API: revision-specific bounded evidence summaries for ready requests."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.recipes.repository import SUPPORTED_DATASETS
from culinary_copilot.retrieval.service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    RetrievalNotFoundError,
    RetrievalStaleError,
    retrieve_for_group,
)


class RetrievalRequest(BaseModel):
    """Revision-pinned retrieval over one clarification group."""

    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(min_length=1, max_length=100)
    request_revision: int = Field(ge=1)
    group_revision: int = Field(ge=1)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    dataset_id: str | None = Field(default=None, max_length=200)


def build_router(
    *, store: Any, engine: Any, settings: Any | None = None, embed_provider: Any = None
) -> APIRouter:
    """Retrieval routes (ADR 0001 steps 1-2 wired; step 4 not done).

    ``RETRIEVAL_MODE`` and friends are read from ``settings`` here
    (code default ``fulltext`` preserves existing behavior). The query
    embedding provider is built at startup only when
    ``EMBEDDINGS_ENABLED`` is set, with a model/dimension check. The
    recommendations path intentionally keeps full-text (ADR step 4 is a
    separate owner decision).
    """

    bound = APIRouter(prefix="/api/v1/retrieval", tags=["retrieval"])

    @bound.post("/search")
    async def search_bound(body: RetrievalRequest) -> dict[str, Any]:
        if body.dataset_id is not None and (
            not body.dataset_id.strip() or body.dataset_id not in SUPPORTED_DATASETS
        ):
            raise HTTPException(
                status_code=422,
                detail=(f"Unsupported dataset_id; expected one of {sorted(SUPPORTED_DATASETS)}"),
            )
        mode = str(getattr(settings, "retrieval_mode", "fulltext") or "fulltext")
        if mode not in ("fulltext", "vector", "hybrid"):
            mode = "fulltext"
        try:
            return await retrieve_for_group(
                store=store,
                engine=engine,
                group_id=body.group_id,
                expected_request_revision=body.request_revision,
                expected_group_revision=body.group_revision,
                limit=body.limit,
                dataset_id=body.dataset_id,
                mode=mode,
                embed_provider=embed_provider,
                embedding_model=str(getattr(settings, "embedding_model", "text-embedding-3-small")),
                embedding_dimension=int(getattr(settings, "embedding_dimension", 1536)),
                vector_candidates_n=int(getattr(settings, "retrieval_vector_candidates", 20)),
                rrf_k=int(getattr(settings, "retrieval_rrf_k", 60)),
                allow_fallback=False,
                vector_distance_cutoff=getattr(settings, "retrieval_vector_cutoff", None),
                fulltext_gate=bool(getattr(settings, "retrieval_fulltext_gate", False)),
            )
        except RetrievalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except RetrievalStaleError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError as exc:
            message = str(exc)
            if "corpus unavailable" in message.lower():
                raise HTTPException(status_code=503, detail="Recipe corpus unavailable") from None
            raise HTTPException(status_code=422, detail=message) from None
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Recipe corpus unavailable") from None

    return bound
