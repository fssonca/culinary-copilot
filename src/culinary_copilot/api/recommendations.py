"""Recommendations API: revision-pinned source-grounded selection (Phase 3).

Business outcomes use HTTP 200 with an ``outcome`` field
(``recommendation`` / ``clarification`` / ``insufficient_evidence``).
Failures use distinct statuses; failed bodies carry stable reason codes
and never expose prompts, secrets, or raw rejected model output, and
contain no generated recommendation or fallback candidates.

HTTP mapping (documented explicitly):
- 422 malformed input (body validation, bad limit/dataset/tool_mode type).
- 404 unknown clarification group.
- 409 stale ``request_revision``/``group_revision``, superseded group, or
  state edits that landed mid-execution (refetch and retry).
- 503 disabled generation (``LLM_RECOMMENDATION_ENABLED=false``), corpus
  unavailable, provider unavailable/auth failure, provider rate limit, or
  provider resource-not-found (distinct ``error.reason`` codes:
  ``generation_disabled``, ``corpus_unavailable``,
  ``provider_unavailable``, ``provider_auth``, ``provider_rate_limited``,
  ``provider_not_found``).
- 504 provider timeout.
- 502 invalid/incomplete provider output or rejected proposal, with stable
  ``error.reason`` codes: ``schema_failure``, ``validation_rejected``,
  ``truncated_incomplete_response`` (with ``incomplete_reason``, token
  usage, and per-attempt metadata in ``detail``),
  ``empty_response``, ``invalid_tool_call``, ``turn_limit_exceeded``,
  ``provider_refusal``, ``provider_content_filter``,
  ``provider_bad_request``, and ``provider_request_error``.
- Provider refusal is a controlled FAILED outcome (502, reason
  ``provider_refusal``). It is never mislabeled ``insufficient_evidence``,
  which is a successful 200 outcome describing the corpus.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.domain.recommendations import RecommendationRequest
from culinary_copilot.recommendations.service import (
    RecommendationFailure,
    RecommendationNotFoundError,
    RecommendationStaleError,
    recommend_for_group,
)


def build_router(
    *, store: Any, engine: Any, settings: Any, provider: Any, epicure: Any
) -> APIRouter:
    bound = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])

    @bound.post("")
    async def recommend_bound(body: RecommendationRequest) -> dict[str, Any]:
        try:
            return await recommend_for_group(
                store=store,
                engine=engine,
                settings=settings,
                provider=provider,
                epicure=epicure,
                group_id=body.group_id,
                expected_request_revision=body.request_revision,
                expected_group_revision=body.group_revision,
                limit=body.limit,
                dataset_id=body.dataset_id,
                tool_mode=body.tool_mode,
            )
        except RecommendationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except RecommendationStaleError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except RecommendationFailure as exc:
            raise HTTPException(
                status_code=exc.http_status,
                detail={"reason": exc.reason, "message": exc.message, **exc.detail},
            ) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except SQLAlchemyError:
            raise HTTPException(status_code=503, detail="Recipe corpus unavailable") from None

    return bound
