"""Typed tool registry (Milestone 3, Phase 2, reviewed).

Each tool has a name, pydantic argument and result schemas
(``extra="forbid"``), a timeout from ``Settings.tool_timeout_s``
(Checkpoint 0: 10 s per tool, server-set, never caller-set), an
idempotency flag, and a cost class (free / paid / network).

``run_tool`` validates arguments, enforces the timeout, and returns
typed error results (``timeout`` / ``invalid_arguments`` /
``unavailable`` / ``permission_denied``) with a stable ``reason`` and
``next_action`` — never an exception into the caller. Sync vs async is
decided before calling (``inspect.iscoroutinefunction``, unwrapping
``__wrapped__`` and callable-object ``__call__``): async
implementations run once under ``wait_for``; sync ones run once via
``to_thread`` under ``wait_for``. A timed-out thread is abandoned, not
killed: it keeps running in the background and its late result is
discarded, so implementations must be side-effect free or idempotent
(or the tool marked non-idempotent). Every call records a structured
event (session id, call id, tool, args digest, outcome/error, latency,
cost); with a session id the event is appended to ``session_events``
via ``PostgresSessionStore.append_event``, otherwise it goes to the
logger. Raw arguments are never logged, only a sha256 digest of the
canonical JSON. The event cost is the mode that actually ran when the
result carries a valid ``cost_class`` (e.g. ``search_recipes``
fulltext=free vs vector=paid); otherwise the tool's static class.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ValidationError

from culinary_copilot.domain.recommendations import (
    REASON_TOOL_INVALID_ARGUMENTS,
    REASON_TOOL_TIMEOUT,
    next_action_for,
)
from culinary_copilot.services.store import new_id

logger = logging.getLogger(__name__)

CostClass = Literal["free", "paid", "network"]

TOOL_CALL_EVENT_TYPE = "tool_call"


def args_digest(args: dict[str, Any]) -> str:
    """sha256 of canonical JSON (sorted keys, compact separators)."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ToolDefinition:
    """Static tool metadata (schemas live on the arg/result models)."""

    name: str
    description: str
    args_model: type[BaseModel]
    timeout_s: float
    idempotent: bool
    cost_class: CostClass


@dataclass
class ToolContext:
    """Runtime dependencies injected per tool call (fakes in tests)."""

    settings: Any = None
    engine: Any = None
    session_store: Any = None
    embed_provider: Any = None
    epicure_core: Any = None
    epicure_cooc: Any = None
    epicure_chem: Any = None
    # Test hook: wrap the raw implementation (e.g. inject a sleeping fake).
    impl_overrides: dict[str, Callable[..., Any]] = field(default_factory=dict)


def _timeout_for(context: ToolContext, tool: ToolDefinition) -> float:
    settings = getattr(context, "settings", None)
    if settings is not None and hasattr(settings, "tool_timeout_s"):
        try:
            value = float(getattr(settings, "tool_timeout_s"))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return float(tool.timeout_s)


def _as_opt_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _is_async_callable(fn: Callable[..., Any]) -> bool:
    """True when calling ``fn`` returns a coroutine (decided before calling).

    Unwraps ``functools.wraps`` chains (``__wrapped__``) and checks a
    callable object's ``__call__`` so wrapped functions and callable
    objects are classified correctly.
    """
    if inspect.iscoroutinefunction(fn):
        return True
    unwrapped: Any = fn
    seen = 0
    while seen < 10:
        nxt = getattr(unwrapped, "__wrapped__", None)
        if nxt is None:
            break
        unwrapped = nxt
        seen += 1
        if inspect.iscoroutinefunction(unwrapped):
            return True
    if not inspect.isfunction(unwrapped) and not inspect.ismethod(unwrapped):
        call = getattr(unwrapped, "__call__", None)
        if call is not None and inspect.iscoroutinefunction(call):
            return True
    return False


def _event_cost(tool: ToolDefinition, result: dict[str, Any]) -> CostClass:
    """Cost of the mode that actually ran (result override wins)."""
    raw = result.get("cost_class")
    if raw == "free":
        return "free"
    if raw == "paid":
        return "paid"
    if raw == "network":
        return "network"
    return tool.cost_class


def _record_event(
    *,
    context: ToolContext,
    session_id: str | None,
    call_id: str,
    tool_name: str,
    digest: str,
    outcome: str,
    error_type: str | None,
    reason: str | None,
    latency_ms: float,
    cost_class: CostClass,
    mode_ran: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "call_id": call_id,
        "tool": tool_name,
        "args_digest": digest,
        "outcome": outcome,
        "error_type": error_type,
        "reason": reason,
        "latency_ms": round(latency_ms, 2),
        "cost_class": cost_class,
    }
    if mode_ran is not None:
        payload["mode_ran"] = mode_ran
    if session_id and getattr(context, "session_store", None) is not None:
        try:
            context.session_store.append_event(session_id, TOOL_CALL_EVENT_TYPE, payload)
        except Exception:
            logger.warning(
                "tool event append failed",
                extra={"tool": tool_name, "call_id": call_id},
            )
    else:
        logger.info("tool_call", extra={"tool_event": payload})


async def run_tool(
    tool: ToolDefinition,
    impl: Callable[..., Awaitable[dict[str, Any]] | dict[str, Any]],
    raw_args: dict[str, Any],
    context: ToolContext,
    *,
    session_id: str | None = None,
    call_id: str | None = None,
) -> dict[str, Any]:
    """Validate, execute with timeout, and return a typed result dict.

    Argument validation failures return ``invalid_arguments``
    (reason ``tool_invalid_arguments``); timeouts return ``timeout``
    (reason ``tool_timeout``). Implementation-typed errors
    (``unavailable`` / ``permission_denied`` / ``invalid_arguments``
    with a specific reason) pass through with their ``next_action``.
    An exception escaping an implementation is a defect and becomes
    ``tool_internal_error`` (``contact_operator``), never raised.
    """
    cid = call_id or new_id("call")
    try:
        parsed = tool.args_model.model_validate(raw_args)
        validated_args = parsed.model_dump()
    except ValidationError as exc:
        reason = REASON_TOOL_INVALID_ARGUMENTS
        result = {
            "ok": False,
            "error_type": "invalid_arguments",
            "reason": reason,
            "message": f"invalid arguments for {tool.name}: {exc.errors()[:3]}",
            "next_action": next_action_for(reason),
        }
        _record_event(
            context=context,
            session_id=session_id,
            call_id=cid,
            tool_name=tool.name,
            digest=args_digest(raw_args if isinstance(raw_args, dict) else {}),
            outcome="error",
            error_type="invalid_arguments",
            reason=reason,
            latency_ms=0.0,
            cost_class=tool.cost_class,
        )
        return result

    digest = args_digest(validated_args)
    timeout = _timeout_for(context, tool)
    start = time.monotonic()
    override = context.impl_overrides.get(tool.name)
    target: Callable[..., Any] = override if override is not None else impl
    try:
        if _is_async_callable(target):
            outcome_raw = await asyncio.wait_for(target(parsed, context), timeout=timeout)
        else:
            outcome_raw = await asyncio.wait_for(
                asyncio.to_thread(target, parsed, context), timeout=timeout
            )
        latency_ms = (time.monotonic() - start) * 1000.0
        typed: dict[str, Any]
        if not isinstance(outcome_raw, dict):
            reason = REASON_TOOL_INVALID_ARGUMENTS
            typed = {
                "ok": False,
                "error_type": "invalid_arguments",
                "reason": reason,
                "message": f"{tool.name} returned a non-mapping result",
                "next_action": next_action_for(reason),
            }
        else:
            typed = dict(outcome_raw)
        result = typed
        _record_event(
            context=context,
            session_id=session_id,
            call_id=cid,
            tool_name=tool.name,
            digest=digest,
            outcome="ok" if result.get("ok") else "error",
            error_type=_as_opt_str(result.get("error_type")),
            reason=_as_opt_str(result.get("reason")),
            latency_ms=latency_ms,
            cost_class=_event_cost(tool, result),
            mode_ran=_as_opt_str(result.get("mode_ran")),
        )
        return result
    except (asyncio.TimeoutError, TimeoutError):
        latency_ms = (time.monotonic() - start) * 1000.0
        reason = REASON_TOOL_TIMEOUT
        result = {
            "ok": False,
            "error_type": "timeout",
            "reason": reason,
            "message": f"{tool.name} timed out after {timeout:g}s",
            "next_action": next_action_for(reason),
        }
        _record_event(
            context=context,
            session_id=session_id,
            call_id=cid,
            tool_name=tool.name,
            digest=digest,
            outcome="error",
            error_type="timeout",
            reason=reason,
            latency_ms=latency_ms,
            cost_class=tool.cost_class,
        )
        return result
    except Exception as exc:
        latency_ms = (time.monotonic() - start) * 1000.0
        # An escaping exception is a defect, surfaced as
        # tool_internal_error (never raised, never truncated).
        from culinary_copilot.domain.recommendations import REASON_TOOL_INTERNAL_ERROR

        reason = REASON_TOOL_INTERNAL_ERROR
        result = {
            "ok": False,
            "error_type": "unavailable",
            "reason": reason,
            "message": f"{tool.name} internal error: {type(exc).__name__}",
            "next_action": next_action_for(reason),
        }
        _record_event(
            context=context,
            session_id=session_id,
            call_id=cid,
            tool_name=tool.name,
            digest=digest,
            outcome="error",
            error_type="unavailable",
            reason=reason,
            latency_ms=latency_ms,
            cost_class=tool.cost_class,
        )
        return result


__all__ = [
    "CostClass",
    "TOOL_CALL_EVENT_TYPE",
    "ToolContext",
    "ToolDefinition",
    "args_digest",
    "run_tool",
]
