"""Agent endpoints: answers, select, SSE stream (disposable Postgres).

Scripted provider + fake tools; sessions in ``culinary_test_agent_api``.
Skipped when PostgreSQL is unreachable.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from test_agent_loop import (
    DOCS,
    SEARCH_ROWS,
    ScriptedProvider,
    _FakeEpicureCore,
    _finish_options,
    _opt,
)

from culinary_copilot.api.agent import build_router
from culinary_copilot.config import Settings
from culinary_copilot.domain.sessions import SessionState
from culinary_copilot.recipes import import_data
from culinary_copilot.services.session_store import PostgresSessionStore
from culinary_copilot.tools.registry import ToolContext

TEST_DB = "culinary_test_agent_api"


def _urls() -> tuple[str, str]:
    settings = Settings()
    base = settings.database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/{TEST_DB}"


@pytest.fixture(scope="module")
def engine():
    maint_url, test_url = _urls()
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
        pytest.skip(f"PostgreSQL unavailable for agent API tests: {exc!r}")
    finally:
        try:
            maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
            with maint.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            maint.dispose()
        except Exception:
            pass


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _fake_context(store: PostgresSessionStore, settings: Settings, **kw: Any) -> ToolContext:
    def _search(args: Any, context: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode_ran": "fulltext",
            "cost_class": "free",
            "results": list(SEARCH_ROWS),
        }

    def _get(args: Any, context: Any) -> dict[str, Any]:
        doc = DOCS.get((args.dataset_id, args.source_id))
        if doc is None:
            return {
                "ok": False,
                "error_type": "invalid_arguments",
                "reason": "tool_invalid_arguments",
                "message": "not found",
                "next_action": "change_request",
            }
        return {"ok": True, "recipe": dict(doc)}

    core = kw.pop("epicure_core", _FakeEpicureCore())
    return ToolContext(
        settings=settings,
        engine=None,
        session_store=store,
        impl_overrides={"search_recipes": _search, "get_recipe": _get},
        epicure_core=core,
        epicure_cooc=core,
        epicure_chem=core,
        **kw,
    )


def _client(
    engine: Any,
    store: PostgresSessionStore,
    provider: ScriptedProvider,
    context: ToolContext,
    settings: Settings | None = None,
) -> TestClient:
    from test_agent_loop import DOCS as _DOCS

    settings = settings or _settings()
    app = FastAPI()
    app.include_router(
        build_router(
            settings=settings,
            engine=engine,
            session_store=store,
            provider=provider,
            tool_context=context,
            recipe_resolver=lambda ds, sid: _DOCS.get((ds, sid)),
        )
    )
    return TestClient(app)


def _session(store: PostgresSessionStore, **kw: Any) -> SessionState:
    params: dict[str, Any] = {"id": f"ses-{uuid.uuid4().hex[:10]}"}
    params.update(kw)
    return store.create(SessionState(**params))


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.strip().split("\n\n"):
        kind: str | None = None
        payload: dict[str, Any] | None = None
        for line in block.splitlines():
            if line.startswith("event:"):
                kind = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1].strip())
        if kind is not None and payload is not None:
            events.append((kind, payload))
    return events


def _finish_two() -> dict[str, Any]:
    return _finish_options(
        [
            _opt(),
            _opt(
                source_id="lentil-2",
                title="Red Lentil Soup",
                quantities=[{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
            ),
        ]
    )


# --- answers -----------------------------------------------------------------------


def test_answers_records_and_removes(engine) -> None:
    store = PostgresSessionStore(engine)
    settings = _settings()
    state = _session(
        store,
        unresolved_questions=[{"question_id": "q1", "question_text": "yogurt?", "options": []}],
    )
    client = _client(engine, store, ScriptedProvider([]), _fake_context(store, settings))
    resp = client.post(
        f"/api/v1/sessions/{state.id}/answers",
        json={"revision": 1, "question_id": "q1", "answer": "no"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == 2
    assert body["unresolved_questions"] == []
    assert any(a.get("question_id") == "q1" for a in body["confirmed_answers"])
    stored = store.get(state.id)
    assert stored is not None and stored.confirmed_answers[-1]["answer"] == "no"


def test_answers_unknown_question_404(engine) -> None:
    store = PostgresSessionStore(engine)
    settings = _settings()
    state = _session(store)
    client = _client(engine, store, ScriptedProvider([]), _fake_context(store, settings))
    resp = client.post(
        f"/api/v1/sessions/{state.id}/answers",
        json={"revision": 1, "question_id": "nope", "answer": "x"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "unknown_question"
    assert "next_action" in resp.json()["detail"]


def test_answers_stale_409(engine) -> None:
    store = PostgresSessionStore(engine)
    settings = _settings()
    state = _session(
        store,
        unresolved_questions=[{"question_id": "q1", "question_text": "t", "options": []}],
    )
    client = _client(engine, store, ScriptedProvider([]), _fake_context(store, settings))
    resp = client.post(
        f"/api/v1/sessions/{state.id}/answers",
        json={"revision": 99, "question_id": "q1", "answer": "x"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "stale_revision"


def test_answers_unknown_session_404(engine) -> None:
    store = PostgresSessionStore(engine)
    settings = _settings()
    client = _client(engine, store, ScriptedProvider([]), _fake_context(store, settings))
    resp = client.post(
        "/api/v1/sessions/ses-missing/answers",
        json={"revision": 1, "question_id": "q", "answer": "x"},
    )
    assert resp.status_code == 404


# --- select --------------------------------------------------------------------------


def test_select_picks_offered_option(engine) -> None:
    store = PostgresSessionStore(engine)
    settings = _settings()
    state = _session(
        store,
        current_phase="recommend",
        suggestions=[_opt(), _opt(source_id="lentil-2")],
    )
    client = _client(engine, store, ScriptedProvider([]), _fake_context(store, settings))
    resp = client.post(
        f"/api/v1/sessions/{state.id}/select",
        json={"revision": 1, "dataset_id": "odunola/foodie", "source_id": "curry-1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["current_phase"] == "select"
    assert resp.json()["selected_dish"]["source_id"] == "curry-1"


def test_select_unknown_option_422(engine) -> None:
    store = PostgresSessionStore(engine)
    settings = _settings()
    state = _session(store, current_phase="recommend", suggestions=[_opt()])
    client = _client(engine, store, ScriptedProvider([]), _fake_context(store, settings))
    resp = client.post(
        f"/api/v1/sessions/{state.id}/select",
        json={"revision": 1, "dataset_id": "evil", "source_id": "666"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "unknown_option"


def test_select_stale_409(engine) -> None:
    store = PostgresSessionStore(engine)
    settings = _settings()
    state = _session(store, current_phase="recommend", suggestions=[_opt()])
    client = _client(engine, store, ScriptedProvider([]), _fake_context(store, settings))
    resp = client.post(
        f"/api/v1/sessions/{state.id}/select",
        json={"revision": 7, "dataset_id": "odunola/foodie", "source_id": "curry-1"},
    )
    assert resp.status_code == 409


# --- SSE stream ----------------------------------------------------------------------------


def test_stream_success_shape(engine) -> None:
    store = PostgresSessionStore(engine)
    settings = _settings()
    state = _session(store)
    provider = ScriptedProvider(
        [
            ("tools", [("c1", "search_recipes", {"query": "curry"})]),
            ("parsed", _finish_two()),
        ]
    )
    client = _client(engine, store, provider, _fake_context(store, settings))
    resp = client.post(f"/api/v1/sessions/{state.id}/agent/stream", json={})
    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers["content-type"]
    events = _parse_sse(resp.text)
    kinds = [k for k, _ in events]
    assert "stage" in kinds
    finals = [p for k, p in events if k == "final"]
    errors = [p for k, p in events if k == "error"]
    assert len(finals) == 1 and not errors
    final = finals[0]
    assert final["stop_reason"] == "agent_sufficient_evidence"
    assert final["result"]["options"][0]["source_id"] == "curry-1"
    assert "next_action" not in final  # finals carry no next_action
    seqs = [p["seq"] for _, p in events]
    assert seqs == sorted(seqs)
    # Stage events carry outcomes, never recipe text.
    blob = json.dumps([p for k, p in events if k == "stage"])
    assert "Creamy Chicken Curry" not in blob


def test_stream_error_shape(engine) -> None:
    store = PostgresSessionStore(engine)
    settings = _settings()
    state = _session(store, steps_remaining=0)
    provider = ScriptedProvider([])
    client = _client(engine, store, provider, _fake_context(store, settings))
    resp = client.post(f"/api/v1/sessions/{state.id}/agent/stream", json={})
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    errors = [p for k, p in events if k == "error"]
    finals = [p for k, p in events if k == "final"]
    assert len(errors) == 1 and not finals
    assert errors[0]["reason"] == "agent_max_steps"
    assert errors[0]["status"] == 422
    assert errors[0]["next_action"] == "change_request"


def test_stream_unknown_session_404(engine) -> None:
    store = PostgresSessionStore(engine)
    settings = _settings()
    client = _client(engine, store, ScriptedProvider([]), _fake_context(store, settings))
    resp = client.post("/api/v1/sessions/ses-missing/agent/stream", json={})
    assert resp.status_code == 404


def test_stream_expected_revision_guard_409(engine) -> None:
    store = PostgresSessionStore(engine)
    settings = _settings()
    state = _session(store)
    provider = ScriptedProvider([])
    client = _client(engine, store, provider, _fake_context(store, settings))
    resp = client.post(f"/api/v1/sessions/{state.id}/agent/stream", json={"expected_revision": 42})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "stale_revision"


def test_stream_concurrent_run_409(engine) -> None:
    store = PostgresSessionStore(engine)
    settings = _settings()
    state = _session(store)
    sid = state.id

    def _bumping_search(args: Any, context: Any) -> dict[str, Any]:
        current = store.get(sid)
        assert current is not None
        # A concurrent writer wins the CAS race mid-run.
        store.mutate(
            sid,
            expected_revision=current.revision,
            fn=lambda snapshot: snapshot,
            event_type="concurrent_bump",
            event_payload={},
        )
        return {"ok": True, "mode_ran": "fulltext", "cost_class": "free", "results": []}

    context = _fake_context(store, settings)
    context.impl_overrides["search_recipes"] = _bumping_search
    provider = ScriptedProvider([("tools", [("c1", "search_recipes", {"query": "x"})])])
    client = _client(engine, store, provider, context)
    resp = client.post(f"/api/v1/sessions/{sid}/agent/stream", json={})
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    errors = [p for k, p in events if k == "error"]
    assert len(errors) == 1
    assert errors[0]["status"] == 409
    assert errors[0]["reason"] == "stale_revision"
    assert errors[0]["next_action"] == "refetch_and_retry"
