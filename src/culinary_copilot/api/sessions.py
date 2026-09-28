"""Agent session API: create, read, internet-search permission (Phase 1).

Sessions live in Postgres (migration 005). The in-memory clarification
store stays for the existing endpoints; sessions link to clarification
requests/groups by ID rather than copying state.

Create is server-set: every session starts in ``discover`` with budgets
from Settings (``SESSION_MAX_TOOL_CALLS`` / ``SESSION_MAX_STEPS``);
client-supplied budgets or phases are rejected with 422.

Error contract: every failure body with a ``reason`` also carries
``next_action`` via ``domain/recommendations.py::next_action_for``.
- 404 ``unknown_session`` -> ``change_request``;
- 409 ``stale_revision`` -> ``refetch_and_retry``;
- 422 ``malformed`` / ``invalid_phase_transition`` -> ``change_request``;
- 503 ``session_store_not_migrated`` (missing 005 tables) -> ``contact_operator``;
- 503 ``session_unavailable`` (other DB errors) -> ``retry``.

Internet search is off by default and enforced in the backend (Phase 5
wires the tool; Phase 1 only stores the permission).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from culinary_copilot.domain.recommendations import (
    REASON_SESSION_NOT_MIGRATED,
    REASON_SESSION_UNAVAILABLE,
    REASON_UNKNOWN_SESSION,
    next_action_for,
)
from culinary_copilot.domain.sessions import (
    DEFAULT_PHASE,
    SessionState,
)
from culinary_copilot.services.session_store import (
    PostgresSessionStore,
    SessionNotFoundError,
    SessionStaleError,
    SessionTransitionError,
)
from culinary_copilot.services.store import new_id


class CreateSessionRequest(BaseModel):
    """Create body: clarification links + common state. Budgets and phase are server-set."""

    model_config = ConfigDict(extra="forbid")

    clarification_request_id: str | None = Field(default=None, max_length=100)
    clarification_group_id: str | None = Field(default=None, max_length=100)
    constraints: dict[str, Any] = Field(default_factory=dict)
    confirmed_answers: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    unresolved_questions: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    internet_search_allowed: bool = False


class PermissionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    allowed: bool


def _session_body(
    state: SessionState, *, events: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    body = state.model_dump()
    if events is not None:
        body["events"] = events
    return body


def _failure(status: int, reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={
            "reason": reason,
            "message": message,
            "next_action": next_action_for(reason),
        },
    )


def _is_missing_table_error(exc: BaseException) -> bool:
    """True when ``exc`` means the 005 tables are absent.

    Detects ``sqlalchemy.exc.ProgrammingError`` wrapping
    ``psycopg.errors.UndefinedTable`` (SQLSTATE 42P01), or the equivalent
    ``relation "sessions"/"session_events" does not exist`` text. A
    ``to_regclass('sessions') IS NULL`` probe is the offline equivalent
    used in rehearsal checks (see docs/sessions.md).
    """
    orig = getattr(exc, "orig", None)
    if orig is not None:
        if type(orig).__name__ == "UndefinedTable" or "UndefinedTable" in type(orig).__name__:
            return True
        if getattr(orig, "sqlstate", None) == "42P01":
            return True
    msg = str(exc).lower()
    if "does not exist" in msg and ("sessions" in msg or "session_events" in msg):
        return True
    condensed = msg.replace(" ", "").replace("_", "")
    return "undefinedtable" in condensed


def _store_error(exc: Exception) -> HTTPException:
    if isinstance(exc, Exception) and _is_missing_table_error(exc):
        return _failure(
            503,
            REASON_SESSION_NOT_MIGRATED,
            "Session store not migrated; migration 005 is pending",
        )
    return _failure(503, REASON_SESSION_UNAVAILABLE, "Session store unavailable")


def build_router(*, store: PostgresSessionStore, settings: Any | None = None) -> APIRouter:
    """Session routes. Budgets and initial phase are server-set from ``settings``."""
    from culinary_copilot.domain.sessions import (
        DEFAULT_STEPS_REMAINING,
        DEFAULT_TOOL_CALLS_REMAINING,
    )

    tool_budget = (
        int(settings.session_max_tool_calls)
        if settings is not None and hasattr(settings, "session_max_tool_calls")
        else DEFAULT_TOOL_CALLS_REMAINING
    )
    step_budget = (
        int(settings.session_max_steps)
        if settings is not None and hasattr(settings, "session_max_steps")
        else DEFAULT_STEPS_REMAINING
    )
    bound = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])

    @bound.post("")
    def create_bound(body: CreateSessionRequest) -> dict[str, Any]:
        state = SessionState(
            id=new_id("ses"),
            revision=1,
            current_phase=DEFAULT_PHASE,
            clarification_request_id=body.clarification_request_id,
            clarification_group_id=body.clarification_group_id,
            constraints=dict(body.constraints),
            confirmed_answers=list(body.confirmed_answers),
            unresolved_questions=list(body.unresolved_questions),
            internet_search_allowed=bool(body.internet_search_allowed),
            tool_calls_remaining=tool_budget,
            steps_remaining=step_budget,
        )
        try:
            created = store.create(state)
        except (IntegrityError, ValueError) as exc:
            raise _failure(422, "malformed", f"invalid session payload: {exc}") from None
        except SQLAlchemyError as exc:
            raise _store_error(exc) from None
        return _session_body(created, events=[])

    @bound.get("/{session_id}")
    def read_bound(session_id: str) -> dict[str, Any]:
        try:
            found = store.get(session_id)
        except SQLAlchemyError as exc:
            raise _store_error(exc) from None
        if found is None:
            raise _failure(404, REASON_UNKNOWN_SESSION, f"session {session_id!r} not found")
        try:
            events = [e.model_dump() for e in store.list_events(session_id)]
        except SQLAlchemyError as exc:
            raise _store_error(exc) from None
        return _session_body(found, events=events)

    @bound.post("/{session_id}/permission")
    def permission_bound(session_id: str, body: PermissionUpdateRequest) -> dict[str, Any]:
        try:
            updated = store.set_permission(
                session_id, expected_revision=body.revision, allowed=body.allowed
            )
        except SessionNotFoundError:
            raise _failure(
                404, REASON_UNKNOWN_SESSION, f"session {session_id!r} not found"
            ) from None
        except SessionStaleError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "stale_revision",
                    "message": str(exc),
                    "next_action": next_action_for("stale_revision"),
                },
            ) from None
        except ValueError as exc:
            raise _failure(422, "malformed", str(exc)) from None
        except SQLAlchemyError as exc:
            raise _store_error(exc) from None
        try:
            events = [e.model_dump() for e in store.list_events(session_id)]
        except SQLAlchemyError as exc:
            raise _store_error(exc) from None
        return _session_body(updated, events=events)

    return bound


__all__ = [
    "CreateSessionRequest",
    "PermissionUpdateRequest",
    "SessionNotFoundError",
    "SessionStaleError",
    "SessionTransitionError",
    "build_router",
]
