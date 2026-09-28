"""P4-REV-01: every failure tells the client what to do next (offline).

Fake providers and stubbed repository only; no model calls, no database.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from test_phase4_streaming_telemetry import (
    FakeApplicationProvider,
    _app,
    _patched_repo,
    _selection,
    _settings,
    _sse_events,
    _store_with_group,
)

from culinary_copilot.domain import recommendations as domain
from culinary_copilot.domain.recommendations import (
    _NEXT_ACTION_BY_REASON,
    NEXT_ACTIONS,
    NEXT_CHANGE_REQUEST,
    NEXT_CONTACT_OPERATOR,
    NEXT_REFETCH_AND_RETRY,
    NEXT_RETRY,
    REASON_VALIDATION_REJECTED,
    REJECT_BAD_REFERENCE,
    REJECT_HARD_CONSTRAINT,
    next_action_for,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "culinary_copilot"

# Terminal success codes: finals of the bounded agent loop, not errors.
# They intentionally have no next_action mapping (finals never carry
# next_action; only error terminals do). Documented here so the scanner
# below still forces a mapping for every other reason code.
TERMINAL_SUCCESS_REASONS = frozenset({"agent_sufficient_evidence", "agent_needs_user_input"})


def _failure_reasons_in_source() -> set[str]:
    """Every failure reason the workflow and API can emit."""
    reasons = {
        value
        for name, value in vars(domain).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }
    pattern = re.compile(r'reason="([a-z_]+)"')
    for path in (
        SRC / "recommendations" / "service.py",
        SRC / "api" / "recommendations.py",
        SRC / "api" / "sessions.py",
        SRC / "api" / "agent.py",
        SRC / "tools" / "search_tools.py",
        SRC / "tools" / "epicure_tools.py",
        SRC / "tools" / "measure_tools.py",
        SRC / "tools" / "stub_tools.py",
        SRC / "tools" / "registry.py",
        SRC / "agent" / "loop.py",
        SRC / "agent" / "validate.py",
    ):
        reasons |= set(pattern.findall(path.read_text(encoding="utf-8")))
    # Tool reason constants (REASON_TOOL_*, REASON_SCALE_*, REASON_CONVERT_*)
    # live in domain/recommendations.py (already covered via vars(domain))
    # and are re-used across tools/*.py; string literals above catch any
    # hardcoded reason="..." in the tool layer.
    api = (SRC / "api" / "recommendations.py").read_text(encoding="utf-8")
    # Stream-only codes: _error_fields tuples and stream-limit cancellations.
    reasons |= set(re.findall(r'return \d{3}, "([a-z_]+)"', api))
    reasons |= set(re.findall(r'"(stream_[a-z_]+_exceeded)"', api))
    # Session reason constants live in domain/sessions.py; every
    # SESSION_*_REASON there must also map in domain/recommendations.py.
    import re as _re

    sessions_src = (SRC / "domain" / "sessions.py").read_text(encoding="utf-8")
    reasons |= set(_re.findall(r'SESSION_\w+_REASON\s*=\s*"([a-z_]+)"', sessions_src))
    return reasons


def test_every_emitted_reason_has_an_explicit_next_action() -> None:
    reasons = _failure_reasons_in_source()
    assert "validation_rejected" in reasons and "stream_duration_exceeded" in reasons
    unmapped = sorted(reasons - set(_NEXT_ACTION_BY_REASON) - set(TERMINAL_SUCCESS_REASONS))
    assert unmapped == [], f"add a next_action mapping for: {unmapped}"
    assert set(_NEXT_ACTION_BY_REASON.values()) <= set(NEXT_ACTIONS)


def test_finals_are_not_errors() -> None:
    """Terminal success codes must stay out of the error mapping."""
    for reason in TERMINAL_SUCCESS_REASONS:
        assert reason not in _NEXT_ACTION_BY_REASON


def test_hard_constraint_rejection_needs_a_changed_request() -> None:
    hard = {"validation_reason": REJECT_HARD_CONSTRAINT}
    other = {"validation_reason": REJECT_BAD_REFERENCE}
    assert next_action_for(REASON_VALIDATION_REJECTED, hard) == NEXT_CHANGE_REQUEST
    assert next_action_for(REASON_VALIDATION_REJECTED, other) == NEXT_RETRY
    assert next_action_for(REASON_VALIDATION_REJECTED) == NEXT_RETRY


def test_configuration_failures_never_suggest_retry() -> None:
    for reason in ("generation_disabled", "provider_auth", "provider_not_found"):
        assert next_action_for(reason) == NEXT_CONTACT_OPERATOR
    assert next_action_for("some_future_reason") == NEXT_CONTACT_OPERATOR


def _stream(provider: Any) -> list[tuple[str, dict[str, Any]]]:
    store, state, group = _store_with_group()
    app = _app(store, _settings(), provider)
    body = {
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
    }
    search, get = _patched_repo()
    with search, get:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=body)
    return _sse_events(resp.text)


def test_stream_error_event_carries_next_action() -> None:
    events = _stream(FakeApplicationProvider(script=[_selection(source_id="no-such-id")]))
    err = [p for k, p in events if k == "error"][0]
    assert err["reason"] == "validation_rejected"
    assert err["next_action"] == NEXT_RETRY
    # Stage and final events are unchanged: next_action is error-only.
    assert all("next_action" not in p for k, p in events if k != "error")


def test_stream_mid_run_edit_says_refetch() -> None:
    store, state, group = _store_with_group()

    class EditingProvider(FakeApplicationProvider):  # type: ignore[misc]
        async def complete_recommendation(  # type: ignore[no-untyped-def]
            self, *, system, user, response_model
        ):
            fresh = store.get_snapshot(group.group_id)
            assert fresh is not None
            st, gp = fresh
            st.revision += 1
            gp.revision += 1
            store.save_state(st)
            store.save_group(gp)
            return await super().complete_recommendation(
                system=system, user=user, response_model=response_model
            )

    app = _app(store, _settings(), EditingProvider(script=[_selection()]))
    body = {
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
    }
    search, get = _patched_repo()
    with search, get:
        resp = TestClient(app).post("/api/v1/recommendations/stream", json=body)
    err = [p for k, p in _sse_events(resp.text) if k == "error"][0]
    assert err["status"] == 409 and err["next_action"] == NEXT_REFETCH_AND_RETRY


def test_json_failure_body_carries_next_action() -> None:
    store, state, group = _store_with_group()
    body = {
        "group_id": group.group_id,
        "request_revision": state.revision,
        "group_revision": group.revision,
    }
    app = _app(store, _settings(), FakeApplicationProvider(script=[_selection(source_id="x")]))
    search, get = _patched_repo()
    with search, get:
        resp = TestClient(app).post("/api/v1/recommendations", json=body)
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["reason"] == "validation_rejected"
    assert detail["next_action"] == NEXT_RETRY
    assert detail["validation_reason"] == "unknown_identity"  # existing detail kept

    disabled = _app(store, _settings(llm_recommendation_enabled=False), FakeApplicationProvider())
    resp = TestClient(disabled).post("/api/v1/recommendations", json=body)
    assert resp.status_code == 503
    assert resp.json()["detail"]["next_action"] == NEXT_CONTACT_OPERATOR
