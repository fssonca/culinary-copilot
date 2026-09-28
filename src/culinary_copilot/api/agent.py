"""Agent loop API (Milestone 3, Phase 3): answers, select, SSE stream.

Existing contract style (like ``api/sessions.py`` and the
recommendations stream): every failure body with a ``reason`` also
carries ``next_action``; the stream emits stage events, then exactly
one final or one error, and the error carries ``next_action``.

- ``POST /api/v1/sessions/{id}/answers`` records one answer
  (``merge_confirmed_answers``, CAS), removes the question, and leaves
  the phase for the next run to continue.
- ``POST /api/v1/sessions/{id}/select`` stores the user's pick from the
  offered options (CAS update to ``selected_dish``, phase ``select``).
- ``POST /api/v1/sessions/{id}/agent/stream`` runs the bounded loop as
  ``text/event-stream``: ``stage`` events carry concise step and tool
  outcomes (never recipe text or reasoning), then exactly one ``final``
  (options/plan/question with source IDs) or one ``error``. A
  concurrent run on the same session gets 409 ``stale_revision``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import StreamingResponse

from culinary_copilot.agent.loop import (
    AgentConcurrentError,
    AgentDeps,
    AgentLoopError,
    record_answer,
    record_select,
    run_agent,
)
from culinary_copilot.domain.recommendations import (
    REASON_SESSION_UNAVAILABLE,
    REASON_UNKNOWN_SESSION,
    next_action_for,
)
from culinary_copilot.services.session_store import (
    PostgresSessionStore,
    SessionNotFoundError,
    SessionStaleError,
    SessionTransitionError,
)

STREAM_CONTRACT_VERSION = "v1"


class AgentStreamRequest(BaseModel):
    """Stream body: optional CAS guard for the run."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int | None = Field(default=None, ge=1)


class AnswerRequest(BaseModel):
    """One answer to an unresolved question."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    question_id: str = Field(min_length=1, max_length=100)
    answer: Any = Field()


class SelectRequest(BaseModel):
    """Pick one offered option by its exact pair."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    dataset_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)


def _failure(status: int, reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={
            "reason": reason,
            "message": message,
            "next_action": next_action_for(reason),
        },
    )


def _loop_http_error(exc: AgentLoopError) -> HTTPException:
    return _failure(exc.http_status, exc.reason, exc.message)


def _sse(event_type: str, payload: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def build_router(
    *,
    settings: Any,
    engine: Any,
    session_store: PostgresSessionStore,
    provider: Any,
    tool_context: Any | None = None,
    embed_provider: Any = None,
    recipe_resolver: Any | None = None,
) -> APIRouter:
    """Agent routes (answers, select, SSE stream)."""
    from culinary_copilot.tools import build_tool_context

    bound = APIRouter(prefix="/api/v1/sessions", tags=["agent"])
    context = (
        tool_context
        if tool_context is not None
        else build_tool_context(
            settings=settings,
            engine=engine,
            session_store=session_store,
            embed_provider=embed_provider,
        )
    )

    @bound.post("/{session_id}/answers")
    def answer_bound(session_id: str, body: AnswerRequest) -> dict[str, Any]:
        try:
            updated = record_answer(
                session_store,
                session_id,
                expected_revision=body.revision,
                question_id=body.question_id,
                answer=body.answer,
            )
        except AgentLoopError as exc:
            raise _loop_http_error(exc) from None
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
        except (SessionTransitionError, ValueError) as exc:
            raise _failure(422, "malformed", str(exc)) from None
        except SQLAlchemyError:
            raise _failure(503, REASON_SESSION_UNAVAILABLE, "Session store unavailable") from None
        return {
            "session_id": session_id,
            "revision": updated.revision,
            "unresolved_questions": updated.unresolved_questions,
            "confirmed_answers": updated.confirmed_answers[-5:],
        }

    @bound.post("/{session_id}/select")
    def select_bound(session_id: str, body: SelectRequest) -> dict[str, Any]:
        try:
            updated = record_select(
                session_store,
                session_id,
                expected_revision=body.revision,
                dataset_id=body.dataset_id,
                source_id=body.source_id,
            )
        except AgentLoopError as exc:
            raise _loop_http_error(exc) from None
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
        except (SessionTransitionError, ValueError) as exc:
            raise _failure(422, "malformed", str(exc)) from None
        except SQLAlchemyError:
            raise _failure(503, REASON_SESSION_UNAVAILABLE, "Session store unavailable") from None
        return {
            "session_id": session_id,
            "revision": updated.revision,
            "current_phase": updated.current_phase,
            "selected_dish": updated.selected_dish,
        }

    @bound.post("/{session_id}/agent/stream")
    async def agent_stream_bound(
        session_id: str, body: AgentStreamRequest, request: Request
    ) -> Any:
        try:
            current = session_store.get(session_id)
        except SQLAlchemyError:
            raise _failure(503, REASON_SESSION_UNAVAILABLE, "Session store unavailable") from None
        if current is None:
            raise _failure(
                404, REASON_UNKNOWN_SESSION, f"session {session_id!r} not found"
            ) from None
        if body.expected_revision is not None and current.revision != body.expected_revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "stale_revision",
                    "message": (
                        f"expected revision {body.expected_revision}, stored {current.revision}"
                    ),
                    "next_action": next_action_for("stale_revision"),
                },
            )

        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

        async def _sink(stage: str, detail: dict[str, Any]) -> None:
            await queue.put((stage, dict(detail)))

        deps = AgentDeps(
            settings=settings,
            session_store=session_store,
            provider=provider,
            tool_context=context,
            recipe_resolver=recipe_resolver,
            on_stage=_sink,
        )
        task = asyncio.create_task(
            run_agent(
                session_id,
                deps=deps,
                expected_revision=body.expected_revision,
            )
        )

        def _stage_event(stage: str, detail: dict[str, Any], seq: int) -> str:
            return _sse(
                "stage",
                {
                    "v": STREAM_CONTRACT_VERSION,
                    "type": "stage",
                    "seq": seq,
                    "session_id": session_id,
                    "stage": stage,
                    "detail": detail,
                },
            )

        def _error_event(seq: int, status: int, reason: str, message: str, next_action: str) -> str:
            return _sse(
                "error",
                {
                    "v": STREAM_CONTRACT_VERSION,
                    "type": "error",
                    "seq": seq,
                    "session_id": session_id,
                    "status": status,
                    "reason": reason,
                    "message": message,
                    "next_action": next_action,
                },
            )

        async def _generate() -> AsyncGenerator[str, None]:
            seq = 0
            try:
                while True:
                    if task.done():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=0.5)
                    except (asyncio.TimeoutError, TimeoutError):
                        if await request.is_disconnected():
                            task.cancel("client_disconnect")
                            return
                        continue
                    stage, detail = item
                    yield _stage_event(stage, detail, seq)
                    seq += 1
                while not queue.empty():
                    stage, detail = queue.get_nowait()
                    yield _stage_event(stage, detail, seq)
                    seq += 1
            except asyncio.CancelledError:
                task.cancel("client_disconnect")
                raise
            try:
                terminal = task.result()
            except AgentConcurrentError as err_concurrent:
                yield _error_event(
                    seq,
                    err_concurrent.http_status,
                    err_concurrent.reason,
                    err_concurrent.message,
                    err_concurrent.next_action,
                )
                return
            except AgentLoopError as err_loop:
                yield _error_event(
                    seq,
                    err_loop.http_status,
                    err_loop.reason,
                    err_loop.message,
                    err_loop.next_action,
                )
                return
            except SQLAlchemyError:
                yield _error_event(
                    seq,
                    503,
                    REASON_SESSION_UNAVAILABLE,
                    "Session store unavailable",
                    next_action_for(REASON_SESSION_UNAVAILABLE),
                )
                return
            except Exception:
                yield _error_event(
                    seq,
                    500,
                    "internal_error",
                    "Internal error",
                    next_action_for("internal_error"),
                )
                return
            payload: dict[str, Any] = {
                "v": STREAM_CONTRACT_VERSION,
                "type": "final",
                "seq": seq,
                "session_id": session_id,
                "stop_reason": terminal.stop_reason,
                "phase": terminal.phase,
                "revision": terminal.revision,
            }
            if terminal.final:
                payload["result"] = terminal.final
            yield _sse("final", payload)

        return StreamingResponse(_generate(), media_type="text/event-stream")

    return bound


__all__ = [
    "STREAM_CONTRACT_VERSION",
    "AgentStreamRequest",
    "AnswerRequest",
    "SelectRequest",
    "build_router",
]
