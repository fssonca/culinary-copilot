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

Streaming (Phase 4): ``POST /api/v1/recommendations/stream`` takes the
same request body and serves ``text/event-stream`` (Starlette
``StreamingResponse``, no new dependency). One workflow, two
transports: both endpoints run ``recommend_for_group``; the stream
passes a stage hook, and entry checks run only inside the workflow. The
endpoint waits for the first stage (``accepted``) before responding, so
anything the workflow rejects before that (404, 409 at entry, 422, 503
disabled/corpus) is an HTTP error with the same body as the JSON
endpoint. After that, every outcome is exactly one terminal event: a
``final`` or an ``error`` (mid-run 409s, provider and validation
failures, stream limits, and unexpected errors as 500
``internal_error``). A client disconnect cancels the workflow with the
reason as the cancellation message; nothing is emitted afterwards and the
workflow records the run, including completed turns' usage, in
telemetry. See ``recommendations/stream.py`` for the event contract.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import StreamingResponse

from culinary_copilot.domain.recommendations import RecommendationRequest
from culinary_copilot.recommendations.service import (
    RecommendationFailure,
    RecommendationNotFoundError,
    RecommendationStaleError,
    recommend_for_group,
)
from culinary_copilot.recommendations.stream import (
    error_payload,
    final_payload,
    keepalive_comment,
    sse_event,
    stage_payload,
)

# How often the stream loop checks for a client disconnect while waiting.
_POLL_S = 0.2


def _http_error(exc: BaseException) -> HTTPException | None:
    """Map a workflow exception to the JSON endpoint's HTTP error.

    Returns None for exceptions that have no controlled mapping.
    """
    if isinstance(exc, RecommendationNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RecommendationStaleError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RecommendationFailure):
        return HTTPException(
            status_code=exc.http_status,
            detail={"reason": exc.reason, "message": exc.message, **exc.detail},
        )
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, SQLAlchemyError):
        return HTTPException(status_code=503, detail="Recipe corpus unavailable")
    return None


def _error_fields(exc: BaseException) -> tuple[int, str, str, dict[str, Any]]:
    """Stable (status, reason, message, detail) for a stream ``error`` event."""
    if isinstance(exc, RecommendationNotFoundError):
        return 404, "unknown_group", str(exc)[:500], {}
    if isinstance(exc, RecommendationStaleError):
        return 409, "stale_revision", str(exc)[:500], {}
    if isinstance(exc, RecommendationFailure):
        return int(exc.http_status), exc.reason, exc.message, dict(exc.detail or {})
    if isinstance(exc, ValueError):
        return 422, "malformed", str(exc)[:500], {}
    if isinstance(exc, SQLAlchemyError):
        return 503, "corpus_unavailable", "Recipe corpus unavailable", {}
    # Unexpected: no message text (it may carry internals), only the type.
    return 500, "internal_error", "Internal error", {"error_type": type(exc).__name__}


def _cancel(task: asyncio.Task[Any], reason: str) -> None:
    """Cancel with ``reason`` as the message, keeping the first reason given."""
    if not task.done() and not task.cancelling():
        task.cancel(reason)


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
        except Exception as exc:
            mapped = _http_error(exc)
            if mapped is None:
                raise
            raise mapped from None

    @bound.post("/stream")
    async def recommend_stream_bound(body: RecommendationRequest, request: Request) -> Any:
        # At least one stage plus the terminal event; one slot is always
        # reserved for the terminal event.
        max_events = max(2, int(settings.rec_stream_max_events))
        max_duration = float(settings.rec_stream_max_duration_s)
        keepalive_s = float(settings.rec_stream_keepalive_s)
        started = time.monotonic()
        # Bounded and lossless: when the client falls behind, the workflow
        # waits at its next stage instead of dropping events.
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=max_events)

        async def _sink(stage: str, detail: dict[str, Any]) -> None:
            await queue.put((stage, dict(detail)))

        task = asyncio.create_task(
            recommend_for_group(
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
                on_stage=_sink,
                endpoint="recommendations",
                transport="sse",
            )
        )
        # Entry checks run in the workflow before its first stage; wait for
        # that stage or an early failure before committing to a stream.
        first_get = asyncio.create_task(queue.get())
        try:
            await asyncio.wait({task, first_get}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            first_get.cancel()
            _cancel(task, "client_disconnect")
            raise
        if not first_get.done():
            first_get.cancel()
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                mapped = _http_error(exc)
                if mapped is None:
                    raise exc
                raise mapped from None
        buffered: list[tuple[str, dict[str, Any]]] = (
            [first_get.result()] if first_get.done() and not first_get.cancelled() else []
        )

        def _ids() -> dict[str, Any]:
            snapshot = store.get_snapshot(body.group_id)
            if snapshot is None:
                return {"request_id": None, "group_id": body.group_id}
            state, _group = snapshot
            return {"request_id": state.request_id, "group_id": body.group_id}

        ids = _ids()

        def _stage_event(seq: int, stage: str, detail: dict[str, Any]) -> str:
            return sse_event(
                "stage",
                stage_payload(
                    seq=seq,
                    request_id=str(ids["request_id"]),
                    group_id=str(ids["group_id"]),
                    request_revision=body.request_revision,
                    group_revision=body.group_revision,
                    stage=stage,
                    detail=detail,
                ),
            )

        def _error_event(
            seq: int, status: int, reason: str, message: str, detail: dict[str, Any]
        ) -> str:
            return sse_event(
                "error",
                error_payload(
                    seq=seq,
                    request_id=ids["request_id"],
                    group_id=ids["group_id"],
                    request_revision=body.request_revision,
                    group_revision=body.group_revision,
                    status=status,
                    reason=reason,
                    message=message,
                    detail=detail,
                ),
            )

        async def _generate() -> Any:
            seq = 0
            skipped = 0
            last_sent = time.monotonic()
            try:
                while True:
                    if await request.is_disconnected():
                        _cancel(task, "client_disconnect")
                        return
                    # 1. Send buffered and queued stages within the stage budget.
                    while buffered or not queue.empty():
                        stage, detail = buffered.pop(0) if buffered else queue.get_nowait()
                        if seq >= max_events - 1:
                            if task.done():
                                # Finished: skip leftover progress, still send
                                # the terminal event (counted in the payload).
                                skipped += 1
                                continue
                            _cancel(task, "stream_event_limit_exceeded")
                            yield _error_event(
                                seq,
                                503,
                                "stream_event_limit_exceeded",
                                "Streaming event bound exceeded",
                                {"max_events": max_events},
                            )
                            return
                        yield _stage_event(seq, stage, detail)
                        seq += 1
                        last_sent = time.monotonic()
                    # 2. Exactly one terminal event once the workflow ends.
                    if task.done():
                        if task.cancelled():
                            return
                        exc = task.exception()
                        if exc is None:
                            payload = final_payload(seq=seq, body=dict(task.result()))
                            if skipped:
                                payload["skipped_stages"] = skipped
                            yield sse_event("final", payload)
                        else:
                            status, reason, message, detail = _error_fields(exc)
                            yield _error_event(seq, status, reason, message, detail)
                        return
                    # 3. Duration bound.
                    if time.monotonic() - started > max_duration:
                        _cancel(task, "stream_duration_exceeded")
                        yield _error_event(
                            seq,
                            504,
                            "stream_duration_exceeded",
                            "Streaming duration bound exceeded",
                            {"max_duration_s": max_duration},
                        )
                        return
                    # 4. Wait briefly for a stage or completion, then re-check.
                    getter = asyncio.create_task(queue.get())
                    await asyncio.wait({task, getter}, timeout=_POLL_S)
                    if not getter.done():
                        getter.cancel()
                        await asyncio.wait({getter})
                    if not getter.cancelled():
                        buffered.append(getter.result())
                    if time.monotonic() - last_sent >= keepalive_s:
                        last_sent = time.monotonic()
                        yield keepalive_comment()
            finally:
                # Covers a disconnect detected by the server itself (the
                # generator is closed) as well as every early return.
                _cancel(task, "stream_closed")

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return bound
