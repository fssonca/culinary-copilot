"""Versioned SSE event contract for recommendation streaming (Phase 4).

Transport: ``POST /api/v1/recommendations/stream`` with the same request
body as ``POST /api/v1/recommendations``. Served as
``text/event-stream`` via Starlette ``StreamingResponse`` (no new
dependency; FastAPI 0.141.1 / Starlette 1.6.0 installed and checked).

Contract version ``v1``:

- ``stage`` events: accepted, readiness, epicure, retrieval, evidence,
  provider_request (sent before each provider turn, turn number only),
  provider_turn (after a turn completes; turn/attempt/tool-call counts
  only), validation, revision_check.
  No model output deltas are streamed: the model returns only a label,
  refs and typed propositions, and recipe content is server-rendered
  after validation. There is no ``provisional`` event by default.
- exactly one ``final`` carrying the same validated body as the
  non-streaming endpoint (recommendation, clarification, or
  insufficient_evidence).
- exactly one ``error`` carrying the same stable status/reason/detail
  envelope, with no recipe content.

Every event carries ``seq`` (0, 1, 2, …) plus correlation IDs
(``request_id``, ``group_id``, ``request_revision``,
``group_revision``). Keep-alive comments (``: keep-alive``) carry no
sequence number.

Limits (config + .env.example): ``REC_STREAM_MAX_EVENTS`` (default
100), ``REC_STREAM_MAX_DURATION_S`` (default 120s),
``REC_STREAM_KEEPALIVE_S`` (default 10s). Buffers are bounded (single
in-flight workflow task + bounded stage queue). Exceeding duration or
event count cancels the workflow and emits a single ``error``.

Pre-stream errors (returned as HTTP errors before the stream starts):
malformed input (422 via body validation), unknown group (404), stale
revisions at entry (409), disabled generation / corpus unavailable
(503). Mid-run revision changes become a 409 ``error`` event, never a
``final``. Provider failures, timeouts, refusals and
schema/validation rejections emit exactly one ``error`` and no
``final``.

Client disconnect: the generator polls ``request.is_disconnected()``
and cancels the workflow task; asyncio cancellation propagates to the
in-flight provider await (best-effort: AsyncOpenAI has no explicit
abort API). Afterwards nothing is emitted; usage known so far is
recorded via the service telemetry path.
"""

from __future__ import annotations

import json
from typing import Any

from culinary_copilot.recommendations.service import ALLOWED_STAGES

STREAM_CONTRACT_VERSION = "v1"

# Single source of truth: the stage names the workflow may emit.
ALLOWED_STAGE_NAMES = ALLOWED_STAGES


def sse_event(event_type: str, payload: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def keepalive_comment() -> str:
    return ": keep-alive\n\n"


def stage_payload(
    *,
    seq: int,
    request_id: str,
    group_id: str,
    request_revision: int | None,
    group_revision: int | None,
    stage: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "v": STREAM_CONTRACT_VERSION,
        "type": "stage",
        "seq": seq,
        "request_id": request_id,
        "group_id": group_id,
        "request_revision": request_revision,
        "group_revision": group_revision,
        "stage": stage,
        "detail": dict(detail or {}),
    }


def final_payload(
    *,
    seq: int,
    body: dict[str, Any],
) -> dict[str, Any]:
    return {
        "v": STREAM_CONTRACT_VERSION,
        "type": "final",
        "seq": seq,
        "request_id": body.get("request_id"),
        "group_id": body.get("group_id"),
        "request_revision": body.get("request_revision"),
        "group_revision": body.get("group_revision"),
        "body": body,
    }


def error_payload(
    *,
    seq: int,
    request_id: str | None,
    group_id: str | None,
    request_revision: int | None = None,
    group_revision: int | None = None,
    status: int,
    reason: str,
    message: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "v": STREAM_CONTRACT_VERSION,
        "type": "error",
        "seq": seq,
        "request_id": request_id,
        "group_id": group_id,
        "request_revision": request_revision,
        "group_revision": group_revision,
        "status": status,
        "reason": reason,
        "message": message,
        "detail": dict(detail or {}),
    }
