"""Structured telemetry for grounded recommendations (Phase 4).

Follows the ``obs/clarification.py`` style: logging with structured
extras, no new backend. Never logs the user message text, prompts,
recipe content, secrets, raw model output, or constraint values such
as dietary restrictions or allergies. Counts, field statuses, and
stable codes only. Unknown usage/cost stays None, never 0.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("culinary_copilot.recommendations")


def emit_recommendation_event(
    *,
    request_id: str,
    group_id: str,
    request_revision: int | None = None,
    group_revision: int | None = None,
    endpoint: str = "recommendations",
    transport: str = "json",
    outcome: str,
    reason: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    config_version: str = "phase4-v2",
    contract_version: str = "typed-propositions-v1",
    pricing_version: str | None = None,
    stage_timings_ms: dict[str, int] | None = None,
    total_latency_ms: int | None = None,
    attempts: int = 0,
    provider_turns: int = 0,
    tool_calls: int = 0,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    response_ids: list[str] | None = None,
    estimated_cost_usd: float | None = None,
    known_cost_usd: float | None = None,
    usage_complete: bool = True,
    turns: list[dict[str, Any]] | None = None,
    stream_events: int | None = None,
    cancelled: bool = False,
) -> dict[str, Any]:
    """Log one completed, failed, or cancelled recommendation run.

    ``turns`` holds per-turn usage (status, attempts, tokens, response id,
    latency, tool calls). ``estimated_cost_usd`` is None unless every sent
    turn's usage is known; ``known_cost_usd`` sums the turns whose usage is
    known and is a lower bound when ``usage_complete`` is false.
    """
    event: dict[str, Any] = {
        "request_id": request_id,
        "group_id": group_id,
        "request_revision": request_revision,
        "group_revision": group_revision,
        "endpoint": endpoint,
        "transport": transport,
        "outcome": outcome,
        "reason": reason,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "config_version": config_version,
        "contract_version": contract_version,
        "pricing_version": pricing_version,
        "stage_timings_ms": dict(stage_timings_ms or {}),
        "total_latency_ms": total_latency_ms,
        "attempts": attempts,
        "provider_turns": provider_turns,
        "tool_calls": tool_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "response_ids": list(response_ids or []),
        "estimated_cost_usd": estimated_cost_usd,
        "known_cost_usd": known_cost_usd,
        "usage_complete": usage_complete,
        "turns": [dict(t) for t in (turns or [])],
        "stream_events": stream_events,
        "cancelled": cancelled,
        "at": int(time.time()),
    }
    logger.info("recommendation_completed", extra={"recommendation": event})
    return event
