"""H4 budget-exhaustion recovery (2026-10-08).

When a batch asks for more tool calls than remain, the affordable prefix
runs and the model gets one tool-less finishing turn with no tools — but
only when the remaining steps, input/output token ceilings and wall clock
allow one more model call (checked with the same code that enforces them).
Otherwise, or when the finishing turn is rejected, the run stops with
``agent_tool_budget_exhausted`` and a deterministic message listing the
useful results already in the session. Never resets or raises allowances.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.agent.loop import AgentDeps, AgentLoopError, run_agent
from culinary_copilot.config import Settings
from culinary_copilot.domain.sessions import SessionState
from culinary_copilot.llm.client import NativeToolCall, NativeTurnResult
from culinary_copilot.recipes import import_data
from culinary_copilot.services.session_store import PostgresSessionStore
from culinary_copilot.tools.registry import ToolContext

TEST_DB = "culinary_test_h4"

CURRY_DOC = {
    "dataset_id": "odunola/foodie",
    "source_id": "curry-1",
    "title": "Creamy Chicken Curry",
    "servings": 4.0,
    "ingredients": [
        {"canonical": "chicken", "amount": "500", "unit": "g", "quantity_text": "500 g"},
        {"canonical": "yogurt", "amount": "1", "unit": "cup", "quantity_text": "1 cup"},
    ],
    "instructions": ["Brown the chicken.", "Stir in yogurt and simmer.", "Serve hot."],
}
LENTIL_DOC = {
    "dataset_id": "odunola/foodie",
    "source_id": "lentil-2",
    "title": "Red Lentil Soup",
    "servings": 4.0,
    "ingredients": [
        {"canonical": "red lentils", "amount": "200", "unit": "g", "quantity_text": "200 g"},
        {"canonical": "onion", "amount": "1", "unit": "count", "quantity_text": "1"},
    ],
    "instructions": ["Simmer the lentils with onion.", "Serve hot."],
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
        pytest.skip(f"PostgreSQL unavailable for H4 tests: {exc!r}")
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
    def __init__(self, turns: list[tuple[str, Any]]) -> None:
        self.turns: deque[tuple[str, Any]] = deque(turns)
        self.seen_tools: list[list[str]] = []
        self.seen_inputs: list[list[dict[str, Any]]] = []

    async def complete_native_tool_turn(
        self, *, input_items: Any, tools: Any, **kw: Any
    ) -> NativeTurnResult:
        self.seen_tools.append([t["name"] for t in tools])
        self.seen_inputs.append(list(input_items))
        if not self.turns:
            raise AssertionError("provider script exhausted")
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
        if kind == "parsed":
            return NativeTurnResult(tool_calls=[], parsed=dict(payload), chain_items=[])
        raise AssertionError(f"bad script kind {kind!r}")


def _opt(
    dataset_id: str = "odunola/foodie",
    source_id: str = "curry-1",
    title: str = "Creamy Chicken Curry",
    quantities: Any = "default",
) -> dict[str, Any]:
    if quantities == "default":
        quantities = [{"ingredient": "chicken", "amount": "500", "unit": "g"}]
    return {
        "dataset_id": dataset_id,
        "source_id": source_id,
        "title": title,
        "quantities": quantities,
        "adaptations": [],
    }


def _two_opts() -> list[dict[str, Any]]:
    return [
        _opt(),
        _opt(
            source_id="lentil-2",
            title="Red Lentil Soup",
            quantities=[{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
        ),
    ]


def _finish_options(options: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "decision": "finish",
        "move_to": "recommend",
        "result": {"options": options},
        "constraints_honored": [],
        "note": "test finish",
    }


class _FakeEpicureCore:
    def __init__(self, *, enabled: bool = True) -> None:
        self.settings = _settings(epicure_enabled=enabled)

    def find_balanced_pairings(self, ingredient: str, k: int = 5):  # type: ignore[no-untyped-def]
        from culinary_copilot.tools.epicure import EpicureDisabledError, Pairing

        if not self.settings.epicure_enabled:
            raise EpicureDisabledError("disabled")
        return [Pairing(ingredient="pork", score=0.5)][:k]


def _deps(
    store: PostgresSessionStore,
    provider: ScriptedProvider,
    *,
    settings: Settings | None = None,
) -> AgentDeps:
    settings = settings or _settings()

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


def _session(store: PostgresSessionStore, **kw: Any) -> SessionState:
    params: dict[str, Any] = {
        "id": f"ses-{uuid.uuid4().hex[:10]}",
        "steps_remaining": 8,
        "tool_calls_remaining": 12,
    }
    params.update(kw)
    return store.create(SessionState(**params))


def _excess_turn() -> tuple[str, Any]:
    # 5 calls with 3 remaining: the first 3 run (search + 2 full fetches),
    # the last 2 get typed budget errors.
    return (
        "tools",
        [
            ("c1", "search_recipes", {"query": "chicken"}),
            ("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
            ("c3", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
            ("c4", "search_recipes", {"query": "extra-a"}),
            ("c5", "search_recipes", {"query": "extra-b"}),
        ],
    )


def test_batch_excess_finishing_turn_completes(engine) -> None:
    """Batch larger than remaining, recipes fetched: finishing turn finishes."""
    store = PostgresSessionStore(engine)
    state = _session(store, steps_remaining=8, tool_calls_remaining=3)
    provider = ScriptedProvider([_excess_turn(), ("parsed", _finish_options(_two_opts()))])
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert len(result.final["options"]) == 2
    # Finishing turn carries no tools (same offered=[] mechanism as final_turn).
    assert provider.seen_tools[1] == []
    assert len(provider.seen_inputs) == 2
    # Allowances never reset or raised: 3 ran, 1 finishing step used.
    stored = store.get(state.id)
    assert stored is not None and stored.tool_calls_remaining == 0
    assert stored.steps_remaining == 6


def test_batch_excess_steps_exhausted_lists_results(engine) -> None:
    """Steps exhausted at run start: no model call, deterministic listed stop.

    A batch larger than the remaining calls cannot even be attempted when
    no steps remain (steps<=1 is already a tool-less final turn, so an
    excess batch is unreachable post-commit). The top max_steps stop
    therefore carries the same deterministic results list.
    """
    store = PostgresSessionStore(engine)
    seeded_opts = [_opt(), _opt(source_id="lentil-2", title="Red Lentil Soup")]
    state = _session(store, steps_remaining=0, tool_calls_remaining=3, suggestions=seeded_opts)
    provider = ScriptedProvider([_excess_turn()])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_max_steps"
    assert len(provider.seen_inputs) == 0  # stopped before any provider call
    message = excinfo.value.message
    assert "Useful results so far" in message
    assert "Creamy Chicken Curry" in message
    assert "options offered" in message
    assert "start a new session" in message


def test_batch_excess_token_exhausted_lists_results(engine) -> None:
    """Batch excess with the output-token budget exhausted: no finishing call.

    The affordable prefix runs (one model call), then the finishing turn
    is unaffordable (output remainder below the useful minimum, checked
    with the same code that enforces it), so the run stops with the
    tool-budget reason plus the deterministic list.
    """
    store = PostgresSessionStore(engine)
    state = _session(store, steps_remaining=8, tool_calls_remaining=3)
    settings = _settings(agent_output_token_ceiling=600)
    provider = ScriptedProvider([_excess_turn()])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider, settings=settings)))
    assert excinfo.value.reason == "agent_tool_budget_exhausted"
    assert len(provider.seen_inputs) == 1  # tool turn ran, finishing turn did not
    assert excinfo.value.message == (
        "batch exceeded the tool-call budget (2 excess not run); tool-call budget "
        'exhausted. Useful results so far: fetched 2 recipe(s): "Creamy Chicken Curry" '
        '(odunola/foodie:curry-1), "Red Lentil Soup" (odunola/foodie:lentil-2); '
        "options offered: none; selected dish: none; plan: none; start a new session"
    )


def test_finishing_turn_rejected_stops_with_list_once(engine) -> None:
    """A rejected finishing turn stops with the list; no second turn."""
    store = PostgresSessionStore(engine)
    state = _session(store, steps_remaining=8, tool_calls_remaining=3)
    bad_finish = {
        "decision": "finish",
        "move_to": "recommend",
        "constraints_honored": [],
        "note": "no result",
    }
    provider = ScriptedProvider([_excess_turn(), ("parsed", bad_finish)])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    # Tools are exhausted, so the final-turn rejection ends with the tool stop.
    assert excinfo.value.reason == "agent_tool_budget_exhausted"
    assert "final turn rejected" in excinfo.value.message
    assert "Useful results so far" in excinfo.value.message
    assert "Creamy Chicken Curry" in excinfo.value.message
    assert len(provider.seen_inputs) == 2  # tool turn + one finishing turn, never a third
    assert provider.seen_tools[1] == []


def test_finishing_turn_offered_at_most_once(engine) -> None:
    """Tool calls on the finishing turn are rejected; no second recovery turn."""
    store = PostgresSessionStore(engine)
    state = _session(store, steps_remaining=8, tool_calls_remaining=3)
    again = ("tools", [("c6", "search_recipes", {"query": "more"})])
    provider = ScriptedProvider([_excess_turn(), again])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_tool_budget_exhausted"
    assert "Useful results so far" in excinfo.value.message
    assert len(provider.seen_inputs) == 2
    assert provider.seen_tools[1] == []
    stored = store.get(state.id)
    assert stored is not None
    assert stored.tool_calls_remaining == 0
    assert stored.steps_remaining == 6


def test_batch_excess_wall_clock_spent_lists_results(engine) -> None:
    """The wall clock runs out during the batch: no finishing call, listed stop.

    Checked by the same top-of-loop wall-clock check as every turn; the
    stop reports the tool budget it was recovering from (422, not 408).
    """
    import time as _time

    def _slow(args: Any, context: Any) -> dict[str, Any]:
        _time.sleep(0.6)
        return {"ok": True, "recipe": dict(DOCS[(args.dataset_id, args.source_id)])}

    store = PostgresSessionStore(engine)
    state = _session(store, steps_remaining=8, tool_calls_remaining=3)
    provider = ScriptedProvider([_excess_turn()])
    deps = _deps(store, provider, settings=_settings(agent_wall_clock_s=0.3, tool_timeout_s=10.0))
    deps.tool_context.impl_overrides["get_recipe"] = _slow
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=deps))
    assert excinfo.value.reason == "agent_tool_budget_exhausted"
    assert excinfo.value.http_status == 422
    assert len(provider.seen_inputs) == 1
    assert "Useful results so far" in excinfo.value.message
    assert "Creamy Chicken Curry" in excinfo.value.message
