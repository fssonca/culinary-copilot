"""Phase 1 disposable-Postgres session tests (no app DB writes).

Creates/drops only ``culinary_test_sessions`` with synthetic fixtures.
Never touches the application database (localhost:5432/culinary_copilot).
Skipped when PostgreSQL is unreachable so offline runs stay green.

Covers: migration 005 applying cleanly on top of 001-003 (and 004 where
the vector extension exists) + idempotent rerun; revision CAS conflicts;
permission defaults/updates; confirmed answers never lost; restart
survival (new engine reads back); append-only event log; invalid
transitions; session API create/read/permission with reason+next_action.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.api.sessions import build_router as build_sessions_router
from culinary_copilot.config import Settings
from culinary_copilot.domain.sessions import SessionState
from culinary_copilot.recipes import import_data
from culinary_copilot.services.session_store import (
    PostgresSessionStore,
    SessionNotFoundError,
    SessionStaleError,
    SessionTransitionError,
)

TEST_DB = "culinary_test_sessions"


def _urls() -> tuple[str, str]:
    settings = Settings()
    base = settings.database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/{TEST_DB}"


def _unique_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module")
def engine():
    maint_url, test_url = _urls()
    if "culinary_copilot" in test_url and "test" not in test_url.split("/")[-1]:
        pytest.fail("refusing to use a non-disposable database name")
    try:
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
        maint.dispose()
        eng = create_engine(test_url)
        with eng.begin() as conn:
            applied = import_data.apply_migrations(conn)
            assert {"001", "002", "003"} <= set(applied) | _applied_versions(conn)
            assert "005" in _applied_versions(conn)
        yield eng
        eng.dispose()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL unavailable for session tests: {exc!r}")
    finally:
        try:
            maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
            with maint.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            maint.dispose()
        except Exception:
            pass


def _applied_versions(conn) -> set[str]:
    return {row[0] for row in conn.execute(text("SELECT version FROM recipe_schema_migrations"))}


def _store(engine) -> PostgresSessionStore:
    return PostgresSessionStore(engine)


def test_005_applies_on_top_of_001_003_and_reruns_idempotently(engine) -> None:
    with engine.begin() as conn:
        versions = _applied_versions(conn)
        assert {"001", "002", "003", "005"} <= versions
        pending = import_data.pending_vector_migrations(conn)
        if "004" in versions:
            assert "004" not in pending
        else:
            # Stock postgres without the vector extension: 004 stays pending,
            # 005 still applies (plain Postgres, no extension requirement).
            assert "004" in pending
        tables = conn.execute(
            text("SELECT to_regclass('sessions'), to_regclass('session_events')")
        ).first()
        assert tables is not None and all(t is not None for t in tables)
        # Idempotent rerun: nothing new to apply.
        assert import_data.apply_migrations(conn) == []


def test_005_tables_have_expected_shape(engine) -> None:
    with engine.connect() as conn:
        cols = {
            row._mapping["column_name"]: row._mapping["data_type"]
            for row in conn.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name='sessions' ORDER BY ordinal_position"
                )
            )
        }
        for expected in (
            "id",
            "revision",
            "current_phase",
            "clarification_request_id",
            "clarification_group_id",
            "constraints",
            "confirmed_answers",
            "unresolved_questions",
            "epicure_outcome",
            "epicure_skip_reason",
            "suggestions",
            "selected_dish",
            "cooking_plan",
            "evidence",
            "internet_search_allowed",
            "tool_calls_remaining",
            "steps_remaining",
            "created_at",
            "updated_at",
        ):
            assert expected in cols, f"sessions.{expected} missing"
        assert cols["internet_search_allowed"] == "boolean"
        default_off = conn.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name='sessions' AND column_name='internet_search_allowed'"
            )
        ).scalar_one()
        assert "false" in default_off.lower()


def test_create_defaults_permission_off_and_links_clarification(engine) -> None:
    store = _store(engine)
    state = SessionState(
        id=_unique_id("ses"),
        clarification_request_id="req-abc",
        clarification_group_id="grp-abc",
    )
    created = store.create(state)
    assert created.revision == 1
    assert created.internet_search_allowed is False
    assert created.tool_calls_remaining == 12
    assert created.steps_remaining == 8
    assert created.clarification_request_id == "req-abc"
    assert created.clarification_group_id == "grp-abc"
    reread = store.get(created.id)
    assert reread is not None and reread.clarification_request_id == "req-abc"
    assert reread.internet_search_allowed is False


def test_revision_conflict_same_semantics_as_clarification(engine) -> None:
    store = _store(engine)
    created = store.create(SessionState(id=_unique_id("ses")))
    first = created.model_copy(deep=True)
    first.constraints = {"diet": "veg"}
    updated = store.update(created.id, expected_revision=1, new_state=first, event_type="updated")
    assert updated.revision == 2
    stale = created.model_copy(deep=True)
    stale.constraints = {"diet": "other"}
    with pytest.raises(SessionStaleError):
        store.update(created.id, expected_revision=1, new_state=stale)
    # Same-revision double submit: exactly one wins (second fails).
    with pytest.raises(SessionStaleError):
        store.update(created.id, expected_revision=1, new_state=stale)


def test_permission_update_flips_and_logs_event(engine) -> None:
    store = _store(engine)
    created = store.create(SessionState(id=_unique_id("ses")))
    assert created.internet_search_allowed is False
    allowed = store.set_permission(created.id, expected_revision=1, allowed=True)
    assert allowed.internet_search_allowed is True
    assert allowed.revision == 2
    events = store.list_events(created.id)
    assert [e.event_type for e in events] == ["created", "permission_updated"]
    assert events[-1].payload == {"allowed": True}
    denied = store.set_permission(created.id, expected_revision=2, allowed=False)
    assert denied.internet_search_allowed is False
    assert denied.revision == 3


def test_confirmed_answers_never_lost_across_updates(engine) -> None:
    store = _store(engine)
    created = store.create(
        SessionState(
            id=_unique_id("ses"),
            confirmed_answers=[{"question_id": "q1", "text": "chicken curry"}],
        )
    )
    nxt = store.get(created.id)
    assert nxt is not None
    nxt.confirmed_answers = [{"question_id": "q2", "text": "30 min"}]
    merged = store.update(created.id, expected_revision=1, new_state=nxt)
    keys = {a.get("question_id") for a in merged.confirmed_answers}
    assert {"q1", "q2"} <= keys  # q1 survived even though the update omitted it


def test_invalid_transition_rejected_with_stable_reason(engine) -> None:
    store = _store(engine)
    created = store.create(SessionState(id=_unique_id("ses"), current_phase="discover"))
    bad = store.get(created.id)
    assert bad is not None
    bad.current_phase = "cook"  # discover -> cook is not allowed
    with pytest.raises(SessionTransitionError) as excinfo:
        store.update(created.id, expected_revision=1, new_state=bad)
    assert excinfo.value.reason == "invalid_phase_transition"
    # Valid skip-select path works.
    good = store.get(created.id)
    assert good is not None
    good.current_phase = "clarify"
    moved = store.update(created.id, expected_revision=1, new_state=good)
    assert moved.current_phase == "clarify" and moved.revision == 2


def test_restart_survival_new_engine_reads_back(engine) -> None:
    _, test_url = _urls()
    store = _store(engine)
    created = store.create(
        SessionState(
            id=_unique_id("ses"),
            current_phase="clarify",
            confirmed_answers=[{"question_id": "q9", "text": "ramen"}],
            internet_search_allowed=True,
        )
    )
    fresh_engine = create_engine(test_url)
    try:
        fresh = PostgresSessionStore(fresh_engine).get(created.id)
        assert fresh is not None
        assert fresh.current_phase == "clarify"
        assert fresh.internet_search_allowed is True
        assert any(a.get("question_id") == "q9" for a in fresh.confirmed_answers)
    finally:
        fresh_engine.dispose()


def test_event_log_is_append_only(engine) -> None:
    store = _store(engine)
    created = store.create(SessionState(id=_unique_id("ses")))
    store.append_event(created.id, "note", {"text": "hello"})
    events = store.list_events(created.id)
    assert [e.seq for e in events] == [1, 2]
    with engine.begin() as conn:
        with pytest.raises(Exception):
            conn.execute(
                text("UPDATE session_events SET event_type='rewritten' WHERE session_id=:sid"),
                {"sid": created.id},
            )
    with pytest.raises(SessionNotFoundError):
        store.append_event("ses-does-not-exist", "note", {})


def _api_client(engine, settings=None) -> TestClient:  # type: ignore[no-untyped-def]
    from culinary_copilot.config import Settings as _Settings

    app = FastAPI()
    app.include_router(
        build_sessions_router(
            store=PostgresSessionStore(engine),
            settings=settings if settings is not None else _Settings(_env_file=None),
        )
    )
    return TestClient(app)


def test_session_api_create_read_permission_carry_next_action(engine) -> None:
    client = _api_client(engine)
    created = client.post(
        "/api/v1/sessions",
        json={"clarification_request_id": "req-1", "clarification_group_id": "grp-1"},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["internet_search_allowed"] is False
    assert body["revision"] == 1
    sid = body["id"]

    fetched = client.get(f"/api/v1/sessions/{sid}")
    assert fetched.status_code == 200
    assert fetched.json()["clarification_request_id"] == "req-1"

    flipped = client.post(
        f"/api/v1/sessions/{sid}/permission", json={"revision": 1, "allowed": True}
    )
    assert flipped.status_code == 200
    assert flipped.json()["internet_search_allowed"] is True
    assert flipped.json()["revision"] == 2

    stale = client.post(
        f"/api/v1/sessions/{sid}/permission", json={"revision": 1, "allowed": False}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["reason"] == "stale_revision"
    assert stale.json()["detail"]["next_action"] == "refetch_and_retry"

    missing = client.get("/api/v1/sessions/ses-does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["detail"]["reason"] == "unknown_session"
    assert missing.json()["detail"]["next_action"] == "change_request"

    # Server-set create: client budgets and phases are rejected with 422.
    for payload in (
        {"tool_calls_remaining": 5},
        {"steps_remaining": 3},
        {"current_phase": "cook"},
    ):
        rejected = client.post("/api/v1/sessions", json=payload)
        assert rejected.status_code == 422, payload


def test_create_uses_configured_budgets_and_always_discover(engine) -> None:
    from culinary_copilot.config import Settings as _Settings

    client = _api_client(engine)
    body = client.post("/api/v1/sessions", json={}).json()
    assert body["current_phase"] == "discover"
    assert body["tool_calls_remaining"] == 12
    assert body["steps_remaining"] == 8

    custom = _api_client(
        engine, settings=_Settings(_env_file=None, session_max_tool_calls=5, session_max_steps=3)
    )
    custom_body = custom.post("/api/v1/sessions", json={}).json()
    assert custom_body["tool_calls_remaining"] == 5
    assert custom_body["steps_remaining"] == 3
    assert custom_body["current_phase"] == "discover"


def test_recommend_can_return_to_clarify_pg(engine) -> None:
    store = _store(engine)
    created = store.create(SessionState(id=_unique_id("ses"), current_phase="recommend"))
    nxt = store.get(created.id)
    assert nxt is not None
    nxt.current_phase = "clarify"
    moved = store.update(created.id, expected_revision=1, new_state=nxt)
    assert moved.current_phase == "clarify"


def test_merge_overflow_raises_and_writes_nothing_pg(engine) -> None:
    import pytest as _pytest

    store = _store(engine)
    stored = [{"question_id": f"q{i}", "text": "x"} for i in range(200)]
    created = store.create(SessionState(id=_unique_id("ses"), confirmed_answers=stored))
    nxt = store.get(created.id)
    assert nxt is not None
    nxt.confirmed_answers = stored + [{"question_id": "q-overflow", "text": "y"}]
    with _pytest.raises(ValueError, match="cap"):
        store.update(created.id, expected_revision=1, new_state=nxt)
    reread = store.get(created.id)
    assert reread is not None and len(reread.confirmed_answers) == 200
