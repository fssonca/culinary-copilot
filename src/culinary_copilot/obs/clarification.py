"""Structured observability for clarification planning.

Records request/group id, planner + schema versions, rule vs model question
counts, model usage/latency when available, provider failures, and replan
counts. Never logs API keys, full conversations, message text, or unnecessary
personal information. Unknown usage/cost stays unknown (None), never zero.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("culinary_copilot.clarification")


def emit_planning_event(
    *,
    request_id: str,
    group_id: str,
    planner_version: str,
    schema_version: str,
    rule_count: int,
    llm_count: int,
    planning_mode: str,
    outcome: str,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    attempts: int = 1,
    provider_error: str | None = None,
    replan_count: int = 0,
    message_chars: int = 0,
    blockers: int = 0,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "request_id": request_id,
        "group_id": group_id,
        "planner_version": planner_version,
        "schema_version": schema_version,
        "rule_questions": rule_count,
        "llm_questions": llm_count,
        "planning_mode": planning_mode,
        "outcome": outcome,
        "model": model,
        "input_tokens": input_tokens,  # None = unknown, never 0-by-default
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
        "attempts": attempts,
        "provider_error": provider_error,
        "replan_count": replan_count,
        "message_chars": message_chars,
        "blockers": blockers,
        "at": int(time.time()),
    }
    logger.info("clarification_planned", extra={"clarification": event})
    return event
