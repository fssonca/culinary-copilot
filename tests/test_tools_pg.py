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
    ctx.bound_session_id = session_id

    denied = _run(
        run_tool(
            defs["search_web"],
            impls["search_web"],
            {"query": "ramen broth"},
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
            {"query": "ramen broth"},
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


def _seed_recipe(engine: object) -> None:
    import json as _json

    doc = {
        "title": "Chicken Curry",
        "provenance": {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
        "ingredients": [{"canonical": "chicken", "amount": "500", "unit": "g"}],
        "instructions": ["Cook it."],
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO recipe_imports (id, dataset_id, revision, checksum, "
                "normalizer_version, vocabulary_checksum, dataset_url, report) "
                "VALUES (:id, :dataset_id, :revision, :checksum, :normalizer_version, "
                ":vocabulary_checksum, :dataset_url, CAST(:report AS jsonb)) ON CONFLICT DO NOTHING"
            ),
            {
                "id": "test-dedupe-1",
                "dataset_id": "odunola/foodie",
                "revision": "test-rev",
                "checksum": "test-checksum",
                "normalizer_version": "3",
                "vocabulary_checksum": "test-vocab",
                "dataset_url": "https://example.invalid/test",
                "report": _json.dumps({"counts": {}}),
            },
        )
        conn.execute(
            text(
                "INSERT INTO recipes (dataset_id, source_id, import_id, title, "
                "total_minutes, servings, ingredient_names, document, search_text) "
                "VALUES (:d, :s, :i, :t, 20, 2, CAST(:n AS text[]), "
                "CAST(:doc AS jsonb), :st) ON CONFLICT DO NOTHING"
            ),
            {
                "d": "odunola/foodie",
                "s": "curry-1",
                "i": "test-dedupe-1",
                "t": "Chicken Curry",
                "n": ["chicken"],
                "doc": _json.dumps(doc),
                "st": "chicken curry dinner",
            },
        )


def test_get_recipe_repeat_returns_short_typed_result(engine) -> None:
    # Phase 7 re-run (peanut): the same get_recipe ran two or three
    # times. A repeat of an identical pair already returned in this
    # session, still visible in the run's history, comes back short,
    # pointing at the earlier evidence — and still counts against the
    # tool budget.
    _seed_recipe(engine)
    settings = Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    session_id = f"ses-{uuid.uuid4().hex[:10]}"
    store.create(SessionState(id=session_id))
    defs = {d.name: d for d in all_tool_definitions(timeout_s=10.0)}
    impls = all_tool_impls()
    ctx = ToolContext(settings=settings, engine=engine, session_store=store)
    ctx.bound_session_id = session_id
    args = {"dataset_id": "odunola/foodie", "source_id": "curry-1"}
    first = _run(
        run_tool(
            defs["get_recipe"],
            impls["get_recipe"],
            args,
            ctx,
            session_id=session_id,
            call_id="call-1",
        )
    )
    assert first["ok"] is True
    assert first["recipe"]["title"] == "Chicken Curry"
    ctx.visible_full_recipes = {("odunola/foodie", "curry-1")}
    second = _run(
        run_tool(
            defs["get_recipe"],
            impls["get_recipe"],
            args,
            ctx,
            session_id=session_id,
            call_id="call-2",
        )
    )
    assert second["ok"] is True
    assert second.get("duplicate_of_session_evidence") is True
    assert "recipe" not in second
    assert second["title"] == "Chicken Curry"
    assert "evidence digest" in second.get("message", "")
    events = [e for e in store.list_events(session_id) if e.event_type == "tool_call"]
    assert len(events) == 2  # the repeat still counts against the budget


def test_get_recipe_pointer_needs_visible_history(engine) -> None:
    # Close-out regression: the short pointer returns only when the
    # pair's full output is still visible in the run's capped history.
    # A repeat the model can no longer see (new run, capped out, or an
    # unknown context) comes back full.
    from culinary_copilot.tools.search_tools import GetRecipeArgs, get_recipe_impl

    _seed_recipe(engine)
    settings = Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    session_id = f"ses-{uuid.uuid4().hex[:10]}"
    store.create(SessionState(id=session_id))
    defs = {d.name: d for d in all_tool_definitions(timeout_s=10.0)}
    impls = all_tool_impls()
    ctx = ToolContext(settings=settings, engine=engine, session_store=store)
    ctx.bound_session_id = session_id
    args = {"dataset_id": "odunola/foodie", "source_id": "curry-1"}
    first = _run(
        run_tool(
            defs["get_recipe"],
            impls["get_recipe"],
            args,
            ctx,
            session_id=session_id,
            call_id="call-1",
        )
    )
    assert first["ok"] is True and "recipe" in first
    parsed = GetRecipeArgs(dataset_id="odunola/foodie", source_id="curry-1")
    pair = ("odunola/foodie", "curry-1")

    async def _call(visible):  # type: ignore[no-untyped-def]
        ctx.visible_full_recipes = visible
        return await get_recipe_impl(parsed, ctx)

    # Visible: short pointer.
    short = _run(_call({pair}))
    assert short["ok"] is True
    assert short.get("duplicate_of_session_evidence") is True
    assert "recipe" not in short
    # New run (empty history): full document again.
    assert "recipe" in _run(_call(set()))
    # Capped out (visible, but not this pair): full document again.
    assert "recipe" in _run(_call({("odunola/foodie", "lentil-2")}))
    # Unknown context (direct callers): full document again.
    assert "recipe" in _run(_call(None))


def test_get_recipe_malformed_id_shows_expected_format(engine) -> None:
    # Phase 7 re-run (chicken): source_id "odunola/foodie-004186"
    # carried the dataset prefix. The message shows the expected shape.
    settings = Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    session_id = f"ses-{uuid.uuid4().hex[:10]}"
    store.create(SessionState(id=session_id))
    defs = {d.name: d for d in all_tool_definitions(timeout_s=10.0)}
    impls = all_tool_impls()
    ctx = ToolContext(settings=settings, engine=engine, session_store=store)
    ctx.bound_session_id = session_id
    bad = _run(
        run_tool(
            defs["get_recipe"],
            impls["get_recipe"],
            {"dataset_id": "odunola/foodie", "source_id": "odunola/foodie-004186"},
            ctx,
            session_id=session_id,
            call_id="call-bad",
        )
    )
    assert bad["ok"] is False
    assert bad["reason"] == "tool_invalid_arguments"
    assert "separately" in bad["message"]
    assert "foodie-004186" in bad["message"]
