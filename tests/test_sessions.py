"""Phase 1 offline session tests: phases, defaults, merge, next_action.

No database, no model calls, no network. Disposable-Postgres behaviour
(CAS, restart survival, migration 005) is covered in test_sessions_pg.py.
API 503-split and merge-overflow API mapping use fake stores (no DB).
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, ProgrammingError

from culinary_copilot.api.sessions import build_router as build_sessions_router
from culinary_copilot.config import Settings
from culinary_copilot.domain.recommendations import (
    REASON_INVALID_PHASE_TRANSITION,
    REASON_SESSION_NOT_MIGRATED,
    REASON_SESSION_UNAVAILABLE,
    REASON_UNKNOWN_SESSION,
    next_action_for,
)
from culinary_copilot.domain.sessions import (
    ALLOWED_TRANSITIONS,
    DEFAULT_STEPS_REMAINING,
    DEFAULT_TOOL_CALLS_REMAINING,
    SESSION_INVALID_TRANSITION_REASON,
    SessionPhase,
    SessionState,
    can_transition,
    merge_confirmed_answers,
    validate_transition,
)


def test_all_eight_phases_defined() -> None:
    assert {p.value for p in SessionPhase} == {
        "discover",
        "clarify",
        "research",
        "recommend",
        "select",
        "plan",
        "cook",
        "plate",
    }
    assert set(ALLOWED_TRANSITIONS) == {p.value for p in SessionPhase}


def test_linear_flow_is_allowed() -> None:
    for frm, to in [
        ("discover", "clarify"),
        ("clarify", "research"),
        ("research", "recommend"),
        ("recommend", "select"),
        ("select", "plan"),
        ("plan", "cook"),
        ("cook", "plate"),
    ]:
        assert can_transition(frm, to), f"{frm} -> {to}"


def test_direct_request_may_skip_select() -> None:
    assert can_transition("recommend", "plan")


def test_recommend_can_return_to_clarify_for_constraint_change() -> None:
    assert can_transition("recommend", "clarify")


def test_plate_is_terminal() -> None:
    assert ALLOWED_TRANSITIONS["plate"] == frozenset()
    assert not can_transition("plate", "cook")
    assert not can_transition("plate", "plan")


def test_invalid_transitions_rejected_with_stable_reason() -> None:
    for frm, to in [
        ("discover", "cook"),
        ("discover", "plate"),
        ("discover", "select"),
        ("clarify", "plate"),
        ("clarify", "cook"),
        ("recommend", "cook"),
        ("recommend", "plate"),
        ("select", "plate"),
        ("plan", "plate"),
        ("cook", "select"),
    ]:
        assert not can_transition(frm, to), f"{frm} -> {to} should be invalid"
        try:
            validate_transition(frm, to)
        except ValueError as exc:
            assert SESSION_INVALID_TRANSITION_REASON in str(exc)
        else:
            raise AssertionError(f"expected rejection for {frm} -> {to}")


def test_same_phase_update_is_not_a_transition() -> None:
    for phase in ALLOWED_TRANSITIONS:
        assert can_transition(phase, phase)
        validate_transition(phase, phase)


def test_session_defaults_match_checkpoint_budgets() -> None:
    state = SessionState(id="ses-test")
    assert state.current_phase == "discover"
    assert state.revision == 1
    assert state.internet_search_allowed is False
    assert state.tool_calls_remaining == DEFAULT_TOOL_CALLS_REMAINING == 12
    assert state.steps_remaining == DEFAULT_STEPS_REMAINING == 12


def test_confirmed_answers_merge_never_loses() -> None:
    stored = [
        {"question_id": "q1", "text": "chicken curry"},
        {"question_id": "q2", "text": "30 min"},
    ]
    # Unrelated update keeps everything.
    assert merge_confirmed_answers(stored, []) == stored
    # New answer appends.
    merged = merge_confirmed_answers(stored, [{"question_id": "q3", "text": "2 portions"}])
    assert len(merged) == 3
    assert stored[0] in merged and stored[1] in merged
    # Attempt to drop q1 by sending only q3 still keeps q1 (never lost).
    assert any(a.get("question_id") == "q1" for a in merged)
    # Explicit correction of q1 replaces it, keeping q2.
    corrected = merge_confirmed_answers(stored, [{"question_id": "q1", "text": "ramen"}])
    assert len(corrected) == 2
    assert next(a for a in corrected if a.get("question_id") == "q1")["text"] == "ramen"
    assert any(a.get("question_id") == "q2" for a in corrected)


def test_session_reasons_have_next_action_mappings() -> None:
    assert next_action_for(REASON_UNKNOWN_SESSION) == "change_request"
    assert next_action_for(REASON_INVALID_PHASE_TRANSITION) == "change_request"
    assert next_action_for(REASON_SESSION_UNAVAILABLE) == "retry"
    assert next_action_for(REASON_SESSION_NOT_MIGRATED) == "contact_operator"
    assert next_action_for("stale_revision") == "refetch_and_retry"


def test_merge_overflow_raises_instead_of_dropping() -> None:
    import pytest

    stored = [{"question_id": f"q{i}", "text": "x"} for i in range(200)]
    with pytest.raises(ValueError, match="cap"):
        merge_confirmed_answers(stored, [{"question_id": "q-new", "text": "y"}])
    # Replacing an existing key at cap is fine (no growth).
    replaced = merge_confirmed_answers(stored, [{"question_id": "q0", "text": "y"}])
    assert len(replaced) == 200


class _MissingTableStore:
    def get(self, session_id: str):  # type: ignore[no-untyped-def]
        from psycopg.errors import UndefinedTable

        raise ProgrammingError(
            "SELECT * FROM sessions", {}, UndefinedTable('relation "sessions" does not exist')
        )

    def list_events(self, session_id: str):  # type: ignore[no-untyped-def]
        return []


class _ConnFailureStore:
    def get(self, session_id: str):  # type: ignore[no-untyped-def]
        raise OperationalError("SELECT * FROM sessions", {}, Exception("connection refused"))

    def list_events(self, session_id: str):  # type: ignore[no-untyped-def]
        return []


class _MergeOverflowStore:
    def set_permission(self, session_id: str, *, expected_revision: int, allowed: bool):  # type: ignore[no-untyped-def]
        raise ValueError("malformed: confirmed answer cap (200) exceeded; nothing was dropped")

    def get(self, session_id: str):  # type: ignore[no-untyped-def]
        return None

    def list_events(self, session_id: str):  # type: ignore[no-untyped-def]
        return []


def _offline_client(store) -> TestClient:  # type: ignore[no-untyped-def]
    app = FastAPI()
    app.include_router(
        build_sessions_router(store=store, settings=Settings(_env_file=None))  # type: ignore[arg-type]
    )
    return TestClient(app, raise_server_exceptions=False)


def test_missing_table_maps_to_not_migrated_contact_operator() -> None:
    resp = _offline_client(_MissingTableStore()).get("/api/v1/sessions/ses-x")
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == REASON_SESSION_NOT_MIGRATED
    assert resp.json()["detail"]["next_action"] == "contact_operator"


def test_connection_failure_maps_to_unavailable_retry() -> None:
    resp = _offline_client(_ConnFailureStore()).get("/api/v1/sessions/ses-x")
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == REASON_SESSION_UNAVAILABLE
    assert resp.json()["detail"]["next_action"] == "retry"


def test_merge_overflow_surfaces_as_422_malformed() -> None:
    resp = _offline_client(_MergeOverflowStore()).post(
        "/api/v1/sessions/ses-x/permission", json={"revision": 1, "allowed": True}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "malformed"
    assert resp.json()["detail"]["next_action"] == "change_request"
