"""Postgres-backed agent session store (Milestone 3, Phase 1).

Same compare-and-set semantics as clarification: reads return detached
copies, writes commit only when the stored revision still matches the
caller's snapshot (one same-revision writer wins, the other gets a stale
error surfaced as HTTP 409). No lock is held across network calls: callers
build the new state outside the transaction; this module validates the
phase transition, merges confirmed answers (never lost) and commits
atomically with the event-log append.

Sessions link to clarification requests/groups by ID only; no
clarification state is copied here (see docs/sessions.md).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy import text

from culinary_copilot.domain.sessions import (
    ALLOWED_TRANSITIONS,
    SESSION_INVALID_TRANSITION_REASON,
    SESSION_NOT_FOUND_REASON,
    SESSION_STALE_REASON,
    SessionEvent,
    SessionState,
    merge_confirmed_answers,
    validate_transition,
)


class SessionNotFoundError(Exception):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"{SESSION_NOT_FOUND_REASON}: session {session_id!r} not found")
        self.reason = SESSION_NOT_FOUND_REASON
        self.session_id = session_id


class SessionStaleError(Exception):
    def __init__(self, session_id: str, expected: int, actual: int | None) -> None:
        super().__init__(
            f"{SESSION_STALE_REASON}: session {session_id!r} has revision {actual}, "
            f"got {expected}; refetch and retry"
        )
        self.reason = SESSION_STALE_REASON
        self.session_id = session_id
        self.expected = expected
        self.actual = actual


class SessionTransitionError(Exception):
    def __init__(self, from_phase: str, to_phase: str) -> None:
        super().__init__(
            f"{SESSION_INVALID_TRANSITION_REASON}: "
            f"{from_phase!r} -> {to_phase!r} is not an allowed phase transition"
        )
        self.reason = SESSION_INVALID_TRANSITION_REASON
        self.from_phase = from_phase
        self.to_phase = to_phase


_COLUMNS = (
    "id, revision, current_phase, clarification_request_id, clarification_group_id, "
    "constraints, confirmed_answers, unresolved_questions, epicure_outcome, "
    "epicure_skip_reason, suggestions, selected_dish, cooking_plan, evidence, "
    "internet_search_allowed, tool_calls_remaining, steps_remaining"
)


def _row_to_state(row: Any) -> SessionState:
    mapping = dict(row._mapping)
    return SessionState(
        id=str(mapping["id"]),
        revision=int(mapping["revision"]),
        current_phase=str(mapping["current_phase"]),
        clarification_request_id=mapping.get("clarification_request_id"),
        clarification_group_id=mapping.get("clarification_group_id"),
        constraints=dict(mapping.get("constraints") or {}),
        confirmed_answers=list(mapping.get("confirmed_answers") or []),
        unresolved_questions=list(mapping.get("unresolved_questions") or []),
        epicure_outcome=mapping.get("epicure_outcome"),
        epicure_skip_reason=mapping.get("epicure_skip_reason"),
        suggestions=list(mapping.get("suggestions") or []),
        selected_dish=mapping.get("selected_dish"),
        cooking_plan=dict(mapping.get("cooking_plan") or {}),
        evidence=list(mapping.get("evidence") or []),
        internet_search_allowed=bool(mapping.get("internet_search_allowed", False)),
        tool_calls_remaining=int(mapping.get("tool_calls_remaining", 0)),
        steps_remaining=int(mapping.get("steps_remaining", 0)),
    )


def _check_phase(phase: str) -> None:
    if phase not in ALLOWED_TRANSITIONS:
        raise ValueError(f"{SESSION_INVALID_TRANSITION_REASON}: unknown phase {phase!r}")


class PostgresSessionStore:
    """CAS session store over the ``sessions`` / ``session_events`` tables."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def create(
        self,
        state: SessionState,
        *,
        event_type: str = "created",
        event_payload: dict[str, Any] | None = None,
    ) -> SessionState:
        _check_phase(state.current_phase)
        payload = json.dumps(event_payload or {"phase": state.current_phase})
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sessions (id, revision, current_phase, "
                    "clarification_request_id, clarification_group_id, constraints, "
                    "confirmed_answers, unresolved_questions, epicure_outcome, "
                    "epicure_skip_reason, suggestions, selected_dish, cooking_plan, "
                    "evidence, internet_search_allowed, tool_calls_remaining, "
                    "steps_remaining) VALUES (:id, :revision, :phase, :req, :grp, "
                    "CAST(:constraints AS jsonb), CAST(:confirmed AS jsonb), "
                    "CAST(:unresolved AS jsonb), :epicure, :skip, "
                    "CAST(:suggestions AS jsonb), CAST(:dish AS jsonb), "
                    "CAST(:plan AS jsonb), CAST(:evidence AS jsonb), "
                    ":search, :tools, :steps)"
                ),
                {
                    "id": state.id,
                    "revision": state.revision,
                    "phase": state.current_phase,
                    "req": state.clarification_request_id,
                    "grp": state.clarification_group_id,
                    "constraints": json.dumps(state.constraints),
                    "confirmed": json.dumps(state.confirmed_answers),
                    "unresolved": json.dumps(state.unresolved_questions),
                    "epicure": state.epicure_outcome,
                    "skip": state.epicure_skip_reason,
                    "suggestions": json.dumps(state.suggestions),
                    "dish": json.dumps(state.selected_dish),
                    "plan": json.dumps(state.cooking_plan),
                    "evidence": json.dumps(state.evidence),
                    "search": state.internet_search_allowed,
                    "tools": state.tool_calls_remaining,
                    "steps": state.steps_remaining,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO session_events (session_id, seq, event_type, payload) "
                    "VALUES (:sid, 1, :etype, CAST(:payload AS jsonb))"
                ),
                {"sid": state.id, "etype": event_type, "payload": payload},
            )
        return state.model_copy(deep=True)

    def get(self, session_id: str) -> SessionState | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {_COLUMNS} FROM sessions WHERE id=:sid"),
                {"sid": session_id},
            ).first()
            if row is None:
                return None
            return _row_to_state(row)

    def list_events(self, session_id: str) -> list[SessionEvent]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT session_id, seq, event_type, payload FROM session_events "
                    "WHERE session_id=:sid ORDER BY seq"
                ),
                {"sid": session_id},
            ).all()
            return [
                SessionEvent(
                    session_id=str(r._mapping["session_id"]),
                    seq=int(r._mapping["seq"]),
                    event_type=str(r._mapping["event_type"]),
                    payload=dict(r._mapping["payload"] or {}),
                )
                for r in rows
            ]

    def update(
        self,
        session_id: str,
        *,
        expected_revision: int,
        new_state: SessionState,
        event_type: str | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> SessionState:
        """CAS update: fails with SessionStaleError / SessionTransitionError.

        Confirmed answers are merged (stored answers never lost); the stored
        revision must still equal ``expected_revision`` or nothing is written.
        """
        _check_phase(new_state.current_phase)
        with self._engine.begin() as conn:
            current_row = conn.execute(
                text(f"SELECT {_COLUMNS} FROM sessions WHERE id=:sid"),
                {"sid": session_id},
            ).first()
            if current_row is None:
                raise SessionNotFoundError(session_id)
            current = _row_to_state(current_row)
            if current.revision != expected_revision:
                raise SessionStaleError(session_id, expected_revision, current.revision)
            try:
                validate_transition(current.current_phase, new_state.current_phase)
            except ValueError as exc:
                raise SessionTransitionError(
                    current.current_phase, new_state.current_phase
                ) from exc
            merged = merge_confirmed_answers(current.confirmed_answers, new_state.confirmed_answers)
            committed = new_state.model_copy(deep=True)
            committed.id = session_id
            committed.confirmed_answers = merged
            committed.revision = expected_revision + 1
            result = conn.execute(
                text(
                    "UPDATE sessions SET revision=:rev, current_phase=:phase, "
                    "clarification_request_id=:req, clarification_group_id=:grp, "
                    "constraints=CAST(:constraints AS jsonb), "
                    "confirmed_answers=CAST(:confirmed AS jsonb), "
                    "unresolved_questions=CAST(:unresolved AS jsonb), "
                    "epicure_outcome=:epicure, epicure_skip_reason=:skip, "
                    "suggestions=CAST(:suggestions AS jsonb), "
                    "selected_dish=CAST(:dish AS jsonb), "
                    "cooking_plan=CAST(:plan AS jsonb), "
                    "evidence=CAST(:evidence AS jsonb), "
                    "internet_search_allowed=:search, tool_calls_remaining=:tools, "
                    "steps_remaining=:steps, updated_at=now() "
                    "WHERE id=:sid AND revision=:expected"
                ),
                {
                    "sid": session_id,
                    "rev": committed.revision,
                    "phase": committed.current_phase,
                    "req": committed.clarification_request_id,
                    "grp": committed.clarification_group_id,
                    "constraints": json.dumps(committed.constraints),
                    "confirmed": json.dumps(committed.confirmed_answers),
                    "unresolved": json.dumps(committed.unresolved_questions),
                    "epicure": committed.epicure_outcome,
                    "skip": committed.epicure_skip_reason,
                    "suggestions": json.dumps(committed.suggestions),
                    "dish": json.dumps(committed.selected_dish),
                    "plan": json.dumps(committed.cooking_plan),
                    "evidence": json.dumps(committed.evidence),
                    "search": committed.internet_search_allowed,
                    "tools": committed.tool_calls_remaining,
                    "steps": committed.steps_remaining,
                    "expected": expected_revision,
                },
            )
            if result.rowcount != 1:
                raise SessionStaleError(session_id, expected_revision, None)
            if event_type is not None:
                next_seq = conn.execute(
                    text(
                        "SELECT COALESCE(MAX(seq), 0) + 1 FROM session_events WHERE session_id=:sid"
                    ),
                    {"sid": session_id},
                ).scalar_one()
                conn.execute(
                    text(
                        "INSERT INTO session_events (session_id, seq, event_type, payload) "
                        "VALUES (:sid, :seq, :etype, CAST(:payload AS jsonb))"
                    ),
                    {
                        "sid": session_id,
                        "seq": int(next_seq),
                        "etype": event_type,
                        "payload": json.dumps(event_payload or {}),
                    },
                )
            return committed.model_copy(deep=True)

    def mutate(
        self,
        session_id: str,
        *,
        expected_revision: int,
        fn: Callable[[SessionState], SessionState],
        event_type: str | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> SessionState:
        """Read-modify-CAS helper: ``fn`` runs outside any lock on a copy."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {_COLUMNS} FROM sessions WHERE id=:sid"),
                {"sid": session_id},
            ).first()
            if row is None:
                raise SessionNotFoundError(session_id)
            snapshot = _row_to_state(row)
        if snapshot.revision != expected_revision:
            raise SessionStaleError(session_id, expected_revision, snapshot.revision)
        updated = fn(snapshot.model_copy(deep=True))
        return self.update(
            session_id,
            expected_revision=expected_revision,
            new_state=updated,
            event_type=event_type,
            event_payload=event_payload,
        )

    def set_permission(
        self, session_id: str, *, expected_revision: int, allowed: bool
    ) -> SessionState:
        """CAS permission update; logs a ``permission_updated`` event."""

        def _apply(snapshot: SessionState) -> SessionState:
            snapshot.internet_search_allowed = bool(allowed)
            return snapshot

        return self.mutate(
            session_id,
            expected_revision=expected_revision,
            fn=_apply,
            event_type="permission_updated",
            event_payload={"allowed": bool(allowed)},
        )

    def append_event(
        self, session_id: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> SessionEvent:
        """Standalone append (does not bump the session revision)."""
        with self._engine.begin() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM sessions WHERE id=:sid"),
                {"sid": session_id},
            ).scalar_one_or_none()
            if exists is None:
                raise SessionNotFoundError(session_id)
            next_seq = conn.execute(
                text("SELECT COALESCE(MAX(seq), 0) + 1 FROM session_events WHERE session_id=:sid"),
                {"sid": session_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO session_events (session_id, seq, event_type, payload) "
                    "VALUES (:sid, :seq, :etype, CAST(:payload AS jsonb))"
                ),
                {
                    "sid": session_id,
                    "seq": int(next_seq),
                    "etype": event_type,
                    "payload": json.dumps(payload or {}),
                },
            )
        return SessionEvent(
            session_id=session_id,
            seq=int(next_seq),
            event_type=event_type,
            payload=dict(payload or {}),
        )
