"""Stub tools (Milestone 3, Phase 2, reviewed).

``search_techniques`` returns ``tool_not_configured`` (permanent, until
Phase 4 ingests the licensed technique corpus). ``search_web`` is
permission-gated in the backend: the session's
``internet_search_allowed`` is re-read from ``PostgresSessionStore`` on
every call. Permission off returns ``permission_denied``; permission on
returns ``tool_not_configured`` (permanent, until Phase 5 wires the
provider). Neither stub makes a network call.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from culinary_copilot.domain.recommendations import (
    REASON_TOOL_INVALID_ARGUMENTS,
    REASON_TOOL_NOT_CONFIGURED,
    REASON_TOOL_PERMISSION_DENIED,
    REASON_TOOL_UNAVAILABLE,
    next_action_for,
)
from culinary_copilot.tools.registry import ToolContext, ToolDefinition


class SearchTechniquesArgs(BaseModel):
    """Arguments for ``search_techniques`` (stub until Phase 4)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)


class SearchWebArgs(BaseModel):
    """Arguments for ``search_web`` (stub; permission-checked)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=500)


async def search_techniques_impl(
    args: SearchTechniquesArgs, context: ToolContext
) -> dict[str, Any]:
    """Stub: technique corpus lands in Phase 4; always not-configured."""
    _ = (args, context)
    return {
        "ok": False,
        "error_type": "unavailable",
        "reason": REASON_TOOL_NOT_CONFIGURED,
        "message": "search_techniques not configured: technique corpus not ingested (Phase 4)",
        "next_action": next_action_for(REASON_TOOL_NOT_CONFIGURED),
    }


async def search_web_impl(args: SearchWebArgs, context: ToolContext) -> dict[str, Any]:
    """Stub: permission-gated; on-permission path lands in Phase 5."""
    store = getattr(context, "session_store", None)
    if store is None:
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": REASON_TOOL_NOT_CONFIGURED,
            "message": "search_web not configured: no session store",
            "next_action": next_action_for(REASON_TOOL_NOT_CONFIGURED),
        }
    try:
        import asyncio as _asyncio

        state = await _asyncio.to_thread(store.get, args.session_id)
    except Exception as exc:
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": REASON_TOOL_UNAVAILABLE,
            "message": f"search_web unavailable: {type(exc).__name__}",
            "next_action": next_action_for(REASON_TOOL_UNAVAILABLE),
        }
    if state is None:
        return {
            "ok": False,
            "error_type": "invalid_arguments",
            "reason": REASON_TOOL_INVALID_ARGUMENTS,
            "message": f"search_web: unknown session {args.session_id!r}",
            "next_action": next_action_for(REASON_TOOL_INVALID_ARGUMENTS),
        }
    if not bool(getattr(state, "internet_search_allowed", False)):
        return {
            "ok": False,
            "error_type": "permission_denied",
            "reason": REASON_TOOL_PERMISSION_DENIED,
            "message": "search_web denied: internet_search_allowed is off for this session",
            "next_action": next_action_for(REASON_TOOL_PERMISSION_DENIED),
        }
    return {
        "ok": False,
        "error_type": "unavailable",
        "reason": REASON_TOOL_NOT_CONFIGURED,
        "message": "search_web not configured: provider not implemented (Phase 5)",
        "next_action": next_action_for(REASON_TOOL_NOT_CONFIGURED),
    }


def tool_definitions(timeout_s: float = 10.0) -> list[ToolDefinition]:
    """Stub-tool definitions (timeout server-set, never caller-set)."""
    return [
        ToolDefinition(
            name="search_techniques",
            description="Stub until Phase 4: always unavailable.",
            args_model=SearchTechniquesArgs,
            timeout_s=float(timeout_s),
            idempotent=True,
            cost_class="free",
        ),
        ToolDefinition(
            name="search_web",
            description="Stub: permission-checked; allowed-but-unavailable until Phase 5.",
            args_model=SearchWebArgs,
            timeout_s=float(timeout_s),
            idempotent=False,
            cost_class="network",
        ),
    ]


__all__ = [
    "SearchTechniquesArgs",
    "SearchWebArgs",
    "search_techniques_impl",
    "search_web_impl",
    "tool_definitions",
]
