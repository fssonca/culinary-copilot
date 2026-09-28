"""Agent restart survival (disposable Postgres, new connections per run).

Run 1 asks (needs_user_input); the process "restarts" (fresh engine and
store objects on the same database); the answer is recorded; run 2
resumes to sufficient_evidence with confirmed answers kept. Uses
``culinary_test_agent_restart`` only. Skipped when PostgreSQL is
unreachable.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from test_agent_loop import DOCS, SEARCH_ROWS, ScriptedProvider, _ask_yogurt, _finish_options

from culinary_copilot.agent.loop import AgentDeps, record_answer, run_agent
from culinary_copilot.config import Settings
from culinary_copilot.domain.sessions import SessionState
from culinary_copilot.recipes import import_data
from culinary_copilot.services.session_store import PostgresSessionStore
from culinary_copilot.tools.registry import ToolContext

TEST_DB = "culinary_test_agent_restart"


def _urls() -> tuple[str, str]:
    settings = Settings()
    base = settings.database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/{TEST_DB}"


@pytest.fixture(scope="module")
def test_url():
    maint_url, url = _urls()
    try:
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
        maint.dispose()
        eng = create_engine(url)
        with eng.begin() as conn:
            import_data.apply_migrations(conn)
        eng.dispose()
        yield url
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL unavailable for agent restart tests: {exc!r}")
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


def _fake_context(store: PostgresSessionStore, settings: Settings) -> ToolContext:
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

    return ToolContext(
        settings=settings,
        engine=None,
        session_store=store,
        impl_overrides={"search_recipes": _search, "get_recipe": _get},
    )


def test_restart_mid_session_survives(test_url) -> None:
    settings = _settings()
    # Run 1 (first "process"): ask about yogurt.
    engine1 = create_engine(test_url)
    store1 = PostgresSessionStore(engine1)
    state = store1.create(SessionState(id="ses-restart-1"))
    provider1 = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "chicken curry"}),
                    (
                        "c2",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                    ),
                ],
            ),
            ("parsed", _ask_yogurt()),
        ]
    )
    deps1 = AgentDeps(
        settings=settings,
        session_store=store1,
        provider=provider1,
        tool_context=_fake_context(store1, settings),
        recipe_resolver=lambda ds, sid: DOCS.get((ds, sid)),
    )
    result1 = _run(run_agent(state.id, deps=deps1))
    assert result1.stop_reason == "agent_needs_user_input"
    engine1.dispose()

    # "Restart": brand-new engine and store objects, same database.
    engine2 = create_engine(test_url)
    store2 = PostgresSessionStore(engine2)
    mid = store2.get(state.id)
    assert mid is not None
    assert any(q.get("question_id") == "q-yogurt" for q in mid.unresolved_questions)

    answered = record_answer(
        store2, state.id, expected_revision=mid.revision, question_id="q-yogurt", answer="no"
    )
    assert answered.unresolved_questions == []

    provider2 = ScriptedProvider(
        [
            (
                "parsed",
                _finish_options(
                    [
                        {
                            "dataset_id": "odunola/foodie",
                            "source_id": "curry-1",
                            "title": "Creamy Chicken Curry",
                            "quantities": [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
                            "adaptations": [
                                {
                                    "description": "replace yogurt with coconut milk",
                                    "label": "adaptation",
                                }
                            ],
                        },
                        {
                            "dataset_id": "odunola/foodie",
                            "source_id": "lentil-2",
                            "title": "Red Lentil Soup",
                            "quantities": [
                                {
                                    "ingredient": "red lentils",
                                    "amount": "200",
                                    "unit": "g",
                                }
                            ],
                            "adaptations": [],
                        },
                    ]
                ),
            )
        ]
    )
    deps2 = AgentDeps(
        settings=settings,
        session_store=store2,
        provider=provider2,
        tool_context=_fake_context(store2, settings),
        recipe_resolver=lambda ds, sid: DOCS.get((ds, sid)),
    )
    result2 = _run(run_agent(state.id, deps=deps2))
    assert result2.stop_reason == "agent_sufficient_evidence"
    final = store2.get(state.id)
    assert final is not None
    assert any(a.get("question_id") == "q-yogurt" for a in final.confirmed_answers)
    assert len(final.suggestions) == 2
    engine2.dispose()
