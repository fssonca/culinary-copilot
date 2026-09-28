"""Phase 2 disposable-Postgres tool tests (no app DB writes).

Uses ``culinary_test_tools`` only. Verifies the search_web permission
gate against a real ``PostgresSessionStore`` (denied while off, then
allowed-but-unavailable after the permission update) and that tool
calls append structured ``tool_call`` events to ``session_events``.
Skipped when PostgreSQL is unreachable so offline runs stay green.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.config import Settings
from culinary_copilot.domain.sessions import SessionState
from culinary_copilot.recipes import import_data
from culinary_copilot.services.session_store import PostgresSessionStore
from culinary_copilot.tools import all_tool_definitions, all_tool_impls, run_tool
from culinary_copilot.tools.registry import ToolContext

TEST_DB = "culinary_test_tools"


def _urls() -> tuple[str, str]:
    settings = Settings()
    base = settings.database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/{TEST_DB}"


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
            import_data.apply_migrations(conn)
        yield eng
        eng.dispose()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL unavailable for tool tests: {exc!r}")
    finally:
        try:
            maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
            with maint.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            maint.dispose()
        except Exception:
            pass


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_search_web_permission_gate_and_tool_event_log(engine) -> None:
    settings = Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    session_id = f"ses-{uuid.uuid4().hex[:10]}"
    store.create(
        SessionState(id=session_id, internet_search_allowed=False),
        event_payload={"phase": "discover"},
    )
    defs = {d.name: d for d in all_tool_definitions(timeout_s=10.0)}
    impls = all_tool_impls()
    ctx = ToolContext(settings=settings, engine=None, session_store=store)

    denied = _run(
        run_tool(
            defs["search_web"],
            impls["search_web"],
            {"session_id": session_id, "query": "ramen broth"},
            ctx,
            session_id=session_id,
            call_id="call-web-denied",
        )
    )
    assert denied["ok"] is False
    assert denied["error_type"] == "permission_denied"
    assert denied["reason"] == "tool_permission_denied"

    current = store.get(session_id)
    assert current is not None
    store.set_permission(session_id, expected_revision=current.revision, allowed=True)

    allowed = _run(
        run_tool(
            defs["search_web"],
            impls["search_web"],
            {"session_id": session_id, "query": "ramen broth"},
            ctx,
            session_id=session_id,
            call_id="call-web-allowed",
        )
    )
    assert allowed["ok"] is False
    assert allowed["error_type"] == "unavailable"
    assert allowed["reason"] == "tool_not_configured"

    events = store.list_events(session_id)
    tool_events = [e for e in events if e.event_type == "tool_call"]
    assert len(tool_events) >= 2
    by_call = {e.payload.get("call_id"): e.payload for e in tool_events}
    assert by_call["call-web-denied"]["error_type"] == "permission_denied"
    assert by_call["call-web-allowed"]["error_type"] == "unavailable"
    assert by_call["call-web-allowed"]["reason"] == "tool_not_configured"
    for payload in by_call.values():
        assert payload["tool"] == "search_web"
        assert "args_digest" in payload and "latency_ms" in payload
        assert "ramen broth" not in str(payload)  # digest only, never raw args
