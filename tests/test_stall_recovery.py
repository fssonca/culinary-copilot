"""Stall recovery (2026-10-08, after the first H8 attempt).

The third identical call with an identical result used to stop the run
at once (``agent_no_progress``), even with fetched recipes in hand. Now
the stall gets one tool-less finishing turn, the same mechanism as the
H4 budget recovery: the model finishes from the evidence or asks one
question. Tool calls on that turn, a rejected answer, or a limit that
prevents the turn all stop with the stall reason and the results list.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from test_h4_budget_recovery import (
    ScriptedProvider,
    _deps,
    _finish_options,
    _run,
    _session,
    _settings,
    _two_opts,
)

from culinary_copilot.agent.loop import AgentLoopError, run_agent
from culinary_copilot.config import Settings
from culinary_copilot.recipes import import_data
from culinary_copilot.services.session_store import PostgresSessionStore

TEST_DB = "culinary_test_stall_recovery"


@pytest.fixture(scope="module")
def engine():
    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    maint_url, test_url = f"{head}/postgres", f"{head}/{TEST_DB}"
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
        pytest.skip(f"PostgreSQL unavailable for stall recovery tests: {exc!r}")
    finally:
        try:
            maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
            with maint.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            maint.dispose()
        except Exception:
            pass


def _fetch_both() -> tuple[str, Any]:
    return (
        "tools",
        [
            ("f1", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
            ("f2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
        ],
    )


def _stalling_turns() -> list[tuple[str, Any]]:
    # Both recipes fetched, then the same search four times: the second
    # earns the wrap-up (search withheld, so the third is not run), the
    # fourth is the third identical run and stalls.
    same = [("tools", [(f"s{i}", "search_recipes", {"query": "same"})]) for i in range(4)]
    return [_fetch_both(), *same]


def test_stall_finishing_turn_finishes_with_options(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider([*_stalling_turns(), ("parsed", _finish_options(_two_opts()))])
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert len(result.final["options"]) == 2
    assert provider.seen_tools[5] == []
    framing = str(provider.seen_inputs[5][-1].get("content"))
    assert "Final step: no tools remain" in framing
    assert "the plan for the selected dish" in framing
    turns = [e.payload for e in store.list_events(state.id) if e.event_type == "agent_turn"]
    assert turns[-1]["final_turn"] is True and turns[-1]["offered"] == []
    stored = store.get(state.id)
    assert stored is not None
    # 2 fetches + 3 searches ran; 5 tool steps + the finishing turn.
    assert stored.tool_calls_remaining == 12 - 5
    assert stored.steps_remaining == 8 - 6


def test_stall_finishing_turn_rejected_reports_the_stall(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    bad_finish = {
        "decision": "finish",
        "move_to": "recommend",
        "constraints_honored": [],
        "note": "no result",
    }
    provider = ScriptedProvider([*_stalling_turns(), ("parsed", bad_finish)])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_no_progress"
    message = excinfo.value.message
    assert message.startswith("finishing turn rejected")
    assert "repeated identical search_recipes calls without new information" in message
    assert "Creamy Chicken Curry" in message
    assert len(provider.seen_inputs) == 6  # never a second finishing turn


def test_stall_finishing_turn_tool_call_stops(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    again = ("tools", [("s9", "search_recipes", {"query": "same"})])
    provider = ScriptedProvider([*_stalling_turns(), again])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_no_progress"
    assert excinfo.value.message.startswith(
        "repeated identical search_recipes calls without new information"
    )
    assert "Useful results so far" in excinfo.value.message
    assert len(provider.seen_inputs) == 6
    stored = store.get(state.id)
    assert stored is not None
    assert stored.tool_calls_remaining == 12 - 5  # the unoffered call never ran


def test_stall_with_no_time_left_stops_with_the_stall(engine) -> None:
    # The wall clock runs out during the stalling step: the finishing
    # turn is refused by the same top-of-loop check, and the stop still
    # reports the stall (as before this change), not the wall clock.
    calls = {"n": 0}

    def _search(args: Any, context: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 3:
            time.sleep(0.6)
        return {
            "ok": True,
            "mode_ran": "fulltext",
            "cost_class": "free",
            "results": [{"dataset_id": "odunola/foodie", "source_id": "curry-1", "title": "x"}],
        }

    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(_stalling_turns())
    deps = _deps(store, provider, settings=_settings(agent_wall_clock_s=0.4, tool_timeout_s=10.0))
    deps.tool_context.impl_overrides["search_recipes"] = _search
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=deps))
    assert excinfo.value.reason == "agent_no_progress"
    assert excinfo.value.http_status == 422
    assert len(provider.seen_inputs) == 5
    assert "Useful results so far" in excinfo.value.message
    assert "Creamy Chicken Curry" in excinfo.value.message
