"""H7 decision logging (offline, disposable Postgres)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.agent.loop import (
    _provider_reasoning_diagnostic,
    run_agent,
)
from culinary_copilot.config import Settings
from culinary_copilot.llm.client import NativeToolCall, NativeTurnResult
from culinary_copilot.recipes import import_data
from culinary_copilot.services.session_store import PostgresSessionStore
from culinary_copilot.tools.registry import ToolContext


def _load_export_markdown() -> Any:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "sessions" / "export_session.py"
    spec = importlib.util.spec_from_file_location("h7_export_session", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.to_markdown


TEST_DB = "culinary_test_h7"

CURRY_DOC = {
    "dataset_id": "odunola/foodie",
    "source_id": "curry-1",
    "title": "Creamy Chicken Curry",
    "servings": 4.0,
    "ingredients": [
        {"canonical": "chicken", "amount": "500", "unit": "g", "quantity_text": "500 g"},
    ],
    "instructions": ["Brown the chicken.", "Serve hot."],
}
LENTIL_DOC = {
    "dataset_id": "odunola/foodie",
    "source_id": "lentil-2",
    "title": "Red Lentil Soup",
    "servings": 4.0,
    "ingredients": [
        {"canonical": "red lentils", "amount": "200", "unit": "g", "quantity_text": "200 g"},
    ],
    "instructions": ["Simmer the lentils.", "Serve hot."],
}
DOCS = {
    ("odunola/foodie", "curry-1"): CURRY_DOC,
    ("odunola/foodie", "lentil-2"): LENTIL_DOC,
}
SEARCH_ROWS = [
    {"dataset_id": "odunola/foodie", "source_id": "curry-1", "title": "Creamy Chicken Curry"},
    {"dataset_id": "odunola/foodie", "source_id": "lentil-2", "title": "Red Lentil Soup"},
]


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
        pytest.skip(f"PostgreSQL unavailable for H7 tests: {exc!r}")
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


class ScriptedProvider:
    def __init__(self, turns: list[Any]) -> None:
        from collections import deque

        self.turns = deque(turns)
        self.seen_tools: list[list[str]] = []

    async def complete_native_tool_turn(self, **kw: Any) -> NativeTurnResult:
        self.seen_tools.append([t["name"] for t in kw.get("tools", [])])
        kind, payload = self.turns.popleft()
        if kind == "tools":
            calls = [
                NativeToolCall(call_id=cid, name=name, arguments=json.dumps(args))
                for cid, name, args in payload
            ]
            chain = [
                {
                    "type": "function_call",
                    "call_id": cid,
                    "name": name,
                    "arguments": json.dumps(args),
                }
                for cid, name, args in payload
            ]
            return NativeTurnResult(tool_calls=calls, parsed=None, chain_items=chain)
        if kind == "tools_reasoning":
            calls_spec, reasoning_text = payload
            calls = [
                NativeToolCall(call_id=cid, name=name, arguments=json.dumps(args))
                for cid, name, args in calls_spec
            ]
            chain = [
                {
                    "type": "function_call",
                    "call_id": cid,
                    "name": name,
                    "arguments": json.dumps(args),
                }
                for cid, name, args in calls_spec
            ]
            reasoning = {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": reasoning_text}],
                # Raw reasoning can arrive in content; it must never be recorded.
                "content": [{"type": "reasoning_text", "text": "RAW-REASONING-MARKER"}],
                "encrypted_content": "ENCRYPTED-MARKER",
            }
            chain = [reasoning] + chain
            return NativeTurnResult(tool_calls=calls, parsed=None, chain_items=chain)
        if kind == "parsed":
            return NativeTurnResult(tool_calls=[], parsed=dict(payload), chain_items=[])
        raise AssertionError(f"bad script kind {kind!r}")


class _FakeEpicureCore:
    def find_balanced_pairings(self, ingredient: str, k: int = 5):  # type: ignore[no-untyped-def]
        from culinary_copilot.tools.epicure import Pairing

        return [Pairing(ingredient="pork", score=0.5)][:k]


def _deps(store: PostgresSessionStore, provider: Any) -> Any:
    from culinary_copilot.agent.loop import AgentDeps

    settings = _settings(epicure_enabled=True)

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

    core = _FakeEpicureCore()
    context = ToolContext(
        settings=settings,
        engine=None,
        session_store=store,
        impl_overrides={"search_recipes": _search, "get_recipe": _get},
        epicure_core=core,
        epicure_cooc=core,
        epicure_chem=core,
    )
    return AgentDeps(
        settings=settings,
        session_store=store,
        provider=provider,
        tool_context=context,
        recipe_resolver=lambda ds, sid: DOCS.get((ds, sid)),
    )


def _session(store: PostgresSessionStore, **kw: Any) -> Any:
    from culinary_copilot.domain.sessions import SessionState

    params: dict[str, Any] = {
        "id": f"ses-{uuid.uuid4().hex[:10]}",
        "steps_remaining": 8,
        "tool_calls_remaining": 12,
    }
    params.update(kw)
    return store.create(SessionState(**params))


def _opt(sid: str = "curry-1", title: str = "Creamy Chicken Curry") -> dict[str, Any]:
    return {
        "dataset_id": "odunola/foodie",
        "source_id": sid,
        "title": title,
        "quantities": [],
        "adaptations": [],
    }


def test_agent_turn_records_offered_withheld_and_budgets(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "curry"}),
                    ("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
                    ("c3", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
                    ("c4", "find_balanced_pairings", {"ingredient": "chicken"}),
                ],
            ),
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "recommend",
                    "result": {"options": [_opt(), _opt("lentil-2", "Red Lentil Soup")]},
                    "constraints_honored": [],
                    "epicure_lines": [{"ingredient": "pork", "decision": "used", "reason": "x"}],
                    "note": "packet finish",
                },
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    turns = [e for e in store.list_events(state.id) if e.event_type == "agent_turn"]
    assert turns, "every turn records one agent_turn decision event"
    first = turns[0].payload
    assert "search_recipes" in first["offered"]
    withheld = {w["tool"]: w["reason"] for w in first["withheld"]}
    assert withheld.get("search_web") == "search_permission_off"
    assert first["steps_remaining"] is not None and first["tool_calls_remaining"] is not None
    # Bounded payloads, no raw reasoning.
    assert len(json.dumps(first)) < 4000
    assert "chain_of_thought" not in json.dumps([e.payload for e in store.list_events(state.id)])


def test_agent_step_records_repeats_and_reject_carries_budgets(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "curry"}),
                    ("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
                    ("c3", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
                    ("c4", "find_balanced_pairings", {"ingredient": "chicken"}),
                ],
            ),
            ("tools", [("c5", "search_recipes", {"query": "curry"})]),
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "recommend",
                    "result": {"options": [_opt(), _opt("lentil-2", "Red Lentil Soup")]},
                    "constraints_honored": [],
                    "epicure_lines": [{"ingredient": "pork", "decision": "used", "reason": "x"}],
                    "note": "packet finish",
                },
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    steps = [e for e in store.list_events(state.id) if e.event_type == "agent_step"]
    assert any(e.payload.get("repeat_noted") for e in steps), (
        "repeated search marked in session_events"
    )


def test_validation_reject_carries_budgets(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store, constraints={"dietary_constraints": ["vegan"]})
    lines = [{"ingredient": "pork", "decision": "rejected", "reason": "not used"}]
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "x"}),
                    ("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
                    ("c3", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
                    ("c4", "find_balanced_pairings", {"ingredient": "chicken"}),
                ],
            ),
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "recommend",
                    "result": {"options": [_opt(), _opt("lentil-2", "Red Lentil Soup")]},
                    "constraints_honored": [],
                    "epicure_lines": lines,
                    "note": "packet finish",
                },
            ),
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "recommend",
                    "result": {"options": [_opt("lentil-2", "Red Lentil Soup")]},
                    "constraints_honored": ["dietary_constraints"],
                    "epicure_lines": lines,
                    "note": "packet finish",
                },
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert rejects and rejects[0].payload.get("steps_remaining") is not None
    assert rejects[0].payload.get("tool_calls_remaining") is not None


def test_reasoning_diagnostic_labelled_bounded_and_unused() -> None:
    long_text = "r " * 2000
    turn = NativeTurnResult(
        tool_calls=[],
        parsed=None,
        chain_items=[{"type": "reasoning", "summary": [{"text": long_text}]}],
    )
    diag = _provider_reasoning_diagnostic(turn)
    assert diag is not None
    assert diag.get("diagnostic") is True
    assert diag.get("kind") == "provider_reasoning_summary"
    assert len(str(diag.get("text") or "")) <= 500
    # No branch in the loop reads the diagnostic: empty and reasoning turns
    # offer the same tools (only observable decisions are recorded).
    assert (
        _provider_reasoning_diagnostic(NativeTurnResult(tool_calls=[], parsed=None, chain_items=[]))
        is None
    )


def test_reasoning_diagnostic_recorded_in_step(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools_reasoning",
                (
                    [
                        ("c1", "search_recipes", {"query": "curry"}),
                        (
                            "c2",
                            "get_recipe",
                            {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                        ),
                        (
                            "c3",
                            "get_recipe",
                            {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                        ),
                        ("c4", "find_balanced_pairings", {"ingredient": "chicken"}),
                    ],
                    "provider summary for diagnostics",
                ),
            ),
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "recommend",
                    "result": {"options": [_opt(), _opt("lentil-2", "Red Lentil Soup")]},
                    "constraints_honored": [],
                    "epicure_lines": [{"ingredient": "pork", "decision": "used", "reason": "x"}],
                    "note": "packet finish",
                },
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    events = store.list_events(state.id)
    steps = [e for e in events if e.event_type == "agent_step"]
    diag = steps[0].payload.get("reasoning_diagnostic", {}) if steps else {}
    assert diag.get("diagnostic") is True
    assert diag.get("text") == "provider summary for diagnostics"
    # Stored once, on the turn's own event; never raw or encrypted reasoning.
    assert not any(
        "reasoning_diagnostic" in e.payload for e in events if e.event_type == "agent_turn"
    )
    blob = json.dumps([e.payload for e in events])
    assert "RAW-REASONING-MARKER" not in blob
    assert "ENCRYPTED-MARKER" not in blob


def test_export_timeline_shows_decision_fields() -> None:
    to_markdown = _load_export_markdown()
    export = {
        "session": {
            "id": "ses-demo",
            "created_at": "now",
            "updated_at": "now",
            "current_phase": "recommend",
            "constraints": {},
            "steps_remaining": 5,
            "tool_calls_remaining": 6,
            "internet_search_allowed": False,
        },
        "events": [
            {
                "seq": 1,
                "event_type": "agent_turn",
                "payload": {
                    "turn": 1,
                    "phase": "discover",
                    "offered": ["get_recipe"],
                    "withheld": [{"tool": "search_recipes", "reason": "select_phase_plan_only"}],
                    "steps_remaining": 7,
                    "tool_calls_remaining": 11,
                },
            },
            {
                "seq": 2,
                "event_type": "agent_step",
                "payload": {
                    "note": "step 1",
                    "steps_remaining": 7,
                    "tool_calls_remaining": 11,
                    "repeated_tools": ["search_recipes"],
                    "repeat_noted": ["c1"],
                },
            },
            {
                "seq": 3,
                "event_type": "agent_validation_reject",
                "payload": {"errors": ["bad"], "steps_remaining": 6, "tool_calls_remaining": 11},
            },
        ],
    }
    md = to_markdown(export)
    assert "offered [get_recipe]" in md
    assert "select_phase_plan_only" in md
    assert "repeated [search_recipes]" in md
    assert "steps left" in md
