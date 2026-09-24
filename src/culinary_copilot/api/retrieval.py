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


def build_router(*, store: Any, engine: Any) -> APIRouter:
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
        try:
            return await retrieve_for_group(
                store=store,
                engine=engine,
                group_id=body.group_id,
                expected_request_revision=body.request_revision,
                expected_group_revision=body.group_revision,
                limit=body.limit,
                dataset_id=body.dataset_id,
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
