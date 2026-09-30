"""Bounded agent loop tests (scripted fake provider, disposable Postgres).

Provider: a local scripted fake (no credentials, no network). Tools: fake
implementations via ``ToolContext.impl_overrides`` plus a fake Epicure
core; no corpus, no embeddings. Sessions live in the disposable
``culinary_test_agent`` database (created/dropped by the fixture).
Skipped when PostgreSQL is unreachable so offline runs stay green.
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

from culinary_copilot.agent.loop import (
    AgentConcurrentError,
    AgentDeps,
    AgentLoopError,
    AgentRunResult,
    offered_tools,
    record_answer,
    record_select,
    run_agent,
)
from culinary_copilot.config import Settings
from culinary_copilot.domain.recommendations import next_action_for
from culinary_copilot.domain.sessions import SessionState
from culinary_copilot.llm.client import (
    NativeToolCall,
    NativeTurnResult,
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderTimeoutError,
)
from culinary_copilot.recipes import import_data
from culinary_copilot.services.session_store import PostgresSessionStore
from culinary_copilot.tools.registry import ToolContext

TEST_DB = "culinary_test_agent"

CURRY_DOC = {
    "dataset_id": "odunola/foodie",
    "source_id": "curry-1",
    "title": "Creamy Chicken Curry",
    "servings": 4.0,
    "ingredients": [
        {"canonical": "chicken", "amount": "500", "unit": "g", "quantity_text": "500 g"},
        {"canonical": "yogurt", "amount": "1", "unit": "cup", "quantity_text": "1 cup"},
        {"canonical": "salt", "amount": None, "unit": None, "quantity_text": None},
    ],
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
        pytest.skip(f"PostgreSQL unavailable for agent tests: {exc!r}")
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


# --- scripted provider ---------------------------------------------------------


class ScriptedProvider:
    """Pops scripted turns; records offered tools and inputs per turn."""

    def __init__(self, turns: list[tuple[str, Any]]) -> None:
        self.turns: deque[tuple[str, Any]] = deque(turns)
        self.seen_tools: list[list[str]] = []
        self.seen_inputs: list[list[dict[str, Any]]] = []
        self.seen_caps: list[Any] = []

    async def complete_native_tool_turn(
        self,
        *,
        input_items: Any,
        tools: Any,
        tool_choice: Any = None,
        response_model: Any = None,
        timeout_s: Any = None,
        max_output_tokens: Any = None,
    ) -> NativeTurnResult:
        self.seen_tools.append([t["name"] for t in tools])
        self.seen_inputs.append(list(input_items))
        self.seen_caps.append(max_output_tokens)
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
        if kind == "empty":
            return NativeTurnResult(tool_calls=[], parsed=None, chain_items=[])
        if kind == "raise":
            raise payload
        if kind == "raw_tools":
            # Pre-built (call_id, name, arguments-string) triples, e.g.
            # malformed JSON the script format cannot express.
            calls = [
                NativeToolCall(call_id=cid, name=name, arguments=args)
                for cid, name, args in payload
            ]
            chain = [
                {
                    "type": "function_call",
                    "call_id": cid,
                    "name": name,
                    "arguments": args,
                }
                for cid, name, args in payload
            ]
            return NativeTurnResult(tool_calls=calls, parsed=None, chain_items=chain)
        raise AssertionError(f"bad script kind {kind!r}")


def _finish_options(options: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    directive: dict[str, Any] = {
        "decision": "finish",
        "move_to": "recommend",
        "result": {"options": options},
        "constraints_honored": [],
        "note": "test finish",
    }
    directive.update(kw)
    return directive


def _opt(
    dataset_id: str = "odunola/foodie",
    source_id: str = "curry-1",
    quantities: Any = "default",
    **kw: Any,
) -> dict[str, Any]:
    if quantities == "default":
        quantities = [{"ingredient": "chicken", "amount": "500", "unit": "g"}]
    option: dict[str, Any] = {
        "dataset_id": dataset_id,
        "source_id": source_id,
        "title": "Creamy Chicken Curry",
        "quantities": quantities,
        "adaptations": [],
    }
    option.update(kw)
    return option


class _FakeEpicureCore:
    def __init__(self, *, enabled: bool = True) -> None:
        self.settings = _settings(epicure_enabled=enabled)
        self.calls = 0

    def find_balanced_pairings(self, ingredient: str, k: int = 5):  # type: ignore[no-untyped-def]
        from culinary_copilot.tools.epicure import EpicureDisabledError, Pairing

        self.calls += 1
        if not self.settings.epicure_enabled:
            raise EpicureDisabledError("disabled")
        return [Pairing(ingredient="pork", score=0.5), Pairing(ingredient="beef", score=0.4)][:k]


def _deps(
    store: PostgresSessionStore,
    provider: ScriptedProvider,
    *,
    settings: Settings | None = None,
    overrides: dict[str, Any] | None = None,
    epicure_core: Any = "default",
    recorded_calls: list[str] | None = None,
    request_text: str | None = None,
) -> AgentDeps:
    settings = settings or _settings()

    def _search(args: Any, context: Any) -> dict[str, Any]:
        if recorded_calls is not None:
            recorded_calls.append(f"search:{args.query}")
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

    impls: dict[str, Any] = {"search_recipes": _search, "get_recipe": _get}
    impls.update(overrides or {})
    core = _FakeEpicureCore() if epicure_core == "default" else epicure_core
    context = ToolContext(
        settings=settings,
        engine=None,
        session_store=store,
        impl_overrides=impls,
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
        request_text=request_text,
    )


def _session(store: PostgresSessionStore, **kw: Any) -> SessionState:
    params: dict[str, Any] = {
        "id": f"ses-{uuid.uuid4().hex[:10]}",
        "steps_remaining": 8,
        "tool_calls_remaining": 12,
    }
    params.update(kw)
    return store.create(SessionState(**params))


# --- stop reasons -----------------------------------------------------------------


def test_max_steps_stop(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store, steps_remaining=2)
    provider = ScriptedProvider(
        [
            ("tools", [("c1", "search_recipes", {"query": "chicken"})]),
            ("tools", [("c2", "search_recipes", {"query": "chicken"})]),
        ]
    )
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_max_steps"
    assert excinfo.value.next_action == "change_request"
    assert excinfo.value.http_status == 422
    assert "new session" in excinfo.value.message
    assert store.get(state.id).steps_remaining == 0


def test_second_run_after_budget_stop(engine) -> None:
    """Exhausted budgets never reset: a rerun stops at once, no LLM call."""
    store = PostgresSessionStore(engine)
    state = _session(store, steps_remaining=1)
    provider = ScriptedProvider([("tools", [("c1", "search_recipes", {"query": "x"})])])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_max_steps"
    assert store.get(state.id).steps_remaining == 0

    provider2 = ScriptedProvider([("tools", [("c1", "search_recipes", {"query": "x"})])])
    with pytest.raises(AgentLoopError) as excinfo2:
        _run(run_agent(state.id, deps=_deps(store, provider2)))
    assert excinfo2.value.reason == "agent_max_steps"
    assert excinfo2.value.next_action == "change_request"
    assert provider2.seen_inputs == []  # no provider call made
    assert len(provider2.turns) == 1  # script untouched


def test_tool_budget_batch_excess(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store, tool_calls_remaining=3)
    recorded: list[str] = []
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "a"}),
                    ("c2", "search_recipes", {"query": "b"}),
                    ("c3", "search_recipes", {"query": "c"}),
                    ("c4", "search_recipes", {"query": "d"}),
                    ("c5", "search_recipes", {"query": "e"}),
                ],
            )
        ]
    )
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider, recorded_calls=recorded)))
    assert excinfo.value.reason == "agent_tool_budget_exhausted"
    assert excinfo.value.http_status == 422
    assert "new session" in excinfo.value.message
    # Affordable prefix ran (thread order varies); none of the excess ran.
    assert sorted(recorded) == ["search:a", "search:b", "search:c"]
    assert store.get(state.id).tool_calls_remaining == 0


def test_wall_clock_stop(engine) -> None:
    import time as _time

    store = PostgresSessionStore(engine)
    state = _session(store)

    def _slow(args: Any, context: Any) -> dict[str, Any]:
        _time.sleep(0.6)
        return {"ok": True, "mode_ran": "fulltext", "cost_class": "free", "results": []}

    provider = ScriptedProvider([("tools", [("c1", "search_recipes", {"query": "slow"})])])
    deps = _deps(
        store,
        provider,
        settings=_settings(agent_wall_clock_s=0.3, tool_timeout_s=10.0),
        overrides={"search_recipes": _slow},
    )
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=deps))
    assert excinfo.value.reason == "agent_wall_clock_exceeded"
    assert excinfo.value.http_status == 408
    assert excinfo.value.next_action == "retry"


def test_token_budget_stop_before_turn(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider([("tools", [("c1", "search_recipes", {"query": "x"})])])
    deps = _deps(
        store,
        provider,
        settings=_settings(agent_input_token_ceiling=100, agent_output_token_ceiling=100),
    )
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=deps))
    assert excinfo.value.reason == "agent_token_budget_exhausted"
    assert excinfo.value.next_action == "change_request"
    assert excinfo.value.http_status == 422
    assert "new session" in excinfo.value.message
    assert provider.seen_inputs == []  # stopped before any provider call


def test_token_usage_recorded_and_output_ceiling(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            ("tools", [("c1", "search_recipes", {"query": "x"})]),
            ("tools", [("c2", "search_recipes", {"query": "y"})]),
        ]
    )
    deps = _deps(
        store,
        provider,
        settings=_settings(agent_input_token_ceiling=100_000, agent_output_token_ceiling=10),
    )
    # Only 10 output tokens remain (< 500 useful minimum): stop at once.
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=deps))
    assert excinfo.value.reason == "agent_token_budget_exhausted"
    assert provider.seen_inputs == []


def test_output_ceiling_counts_prior_runs(engine) -> None:
    from culinary_copilot.agent.loop import session_token_usage

    store = PostgresSessionStore(engine)
    state = _session(store)
    # Prior-run usage counts: the ceiling is per session, not per run.
    store.append_event(
        state.id,
        "agent_step",
        {"note": "prior run", "input_tokens": 100, "output_tokens": 900},
    )
    assert session_token_usage(store, state.id) == (100, 900)
    settings = _settings(agent_input_token_ceiling=100_000, agent_output_token_ceiling=1000)
    provider = ScriptedProvider([("tools", [("c1", "search_recipes", {"query": "x"})])])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider, settings=settings)))
    assert excinfo.value.reason == "agent_token_budget_exhausted"
    assert provider.seen_inputs == []
    # Past the ceiling entirely: same stop.
    store.append_event(
        state.id, "agent_step", {"note": "prior run 2", "input_tokens": 0, "output_tokens": 200}
    )
    provider2 = ScriptedProvider([("tools", [("c1", "search_recipes", {"query": "x"})])])
    with pytest.raises(AgentLoopError) as excinfo2:
        _run(run_agent(state.id, deps=_deps(store, provider2, settings=settings)))
    assert excinfo2.value.reason == "agent_token_budget_exhausted"
    assert provider2.seen_inputs == []


def test_token_usage_recorded_in_step_events(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider([("tools", [("c1", "search_recipes", {"query": "x"})])])
    with pytest.raises(AgentLoopError):
        _run(run_agent(state.id, deps=_deps(store, provider)))
    steps = [e for e in store.list_events(state.id) if e.event_type == "agent_step"]
    assert steps and all(e.payload.get("input_tokens", 0) > 0 for e in steps)
    assert all("input_tokens_estimated" in e.payload for e in steps)
    from culinary_copilot.agent.loop import session_token_usage

    used_in, used_out = session_token_usage(store, state.id)
    assert used_in > 0 and used_out > 0


def test_reported_usage_preferred_over_estimate(engine) -> None:
    from culinary_copilot.llm.client import NativeTurnResult

    store = PostgresSessionStore(engine)
    state = _session(store)

    class _UsageProvider(ScriptedProvider):
        async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
            await super().complete_native_tool_turn(**kwargs)
            return NativeTurnResult(
                tool_calls=[],
                parsed=None,
                chain_items=[],
                input_tokens=111,
                output_tokens=22,
            )

    provider = _UsageProvider([("empty", None)] * 3)
    deps = _deps(store, provider)
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=deps))
    assert excinfo.value.reason == "agent_no_progress"
    steps = [e for e in store.list_events(state.id) if e.event_type == "agent_step"]
    assert len(steps) == 3
    assert all(s.payload["input_tokens"] == 111 for s in steps)
    assert all(s.payload["output_tokens"] == 22 for s in steps)
    assert all(s.payload["input_tokens_estimated"] is False for s in steps)


def test_epicure_lines_from_model_recorded(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "find_balanced_pairings", {"ingredient": "chicken"}),
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
                ],
            ),
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "recommend",
                    "result": {"options": _two_opts()},
                    "constraints_honored": [],
                    "note": "with lines",
                    "epicure_lines": [
                        {
                            "ingredient": "pork",
                            "decision": "used",
                            "reason": "crisp contrast for the curry",
                        }
                    ],
                },
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    finished = [e for e in store.list_events(state.id) if e.event_type == "agent_finished"][0]
    assert "used pork: crisp contrast for the curry" in finished.payload["epicure_lines"]
    assert any(
        line.startswith("derived:") for line in finished.payload["epicure_lines"]
    )  # beef has no model line


def test_epicure_lines_derived_prefix(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "find_balanced_pairings", {"ingredient": "chicken"}),
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
                ],
            ),
            ("parsed", _finish_options(_two_opts())),
        ]
    )
    _run(run_agent(state.id, deps=_deps(store, provider)))
    finished = [e for e in store.list_events(state.id) if e.event_type == "agent_finished"][0]
    assert finished.payload["epicure_lines"]
    assert all(line.startswith("derived:") for line in finished.payload["epicure_lines"])


def test_single_option_survivor_recorded(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "find_balanced_pairings", {"ingredient": "chicken"}),
                    ("c2", "search_recipes", {"query": "curry"}),
                    (
                        "c3",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                    ),
                ],
            ),
            (
                "parsed",
                _finish_options([_opt(), _opt(dataset_id="evil", source_id="666")]),
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert result.final is not None
    assert result.final.get("single_option_reason") == "only_one_valid_candidate"
    assert len(result.final["options"]) == 1
    finished = [e for e in store.list_events(state.id) if e.event_type == "agent_finished"][0]
    assert finished.payload["single_option_reason"] == "only_one_valid_candidate"
    assert finished.payload["dropped_options"] != []


def test_single_option_rejected_without_direct_lookup(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    finish = _finish_options([_opt()], epicure_skip_reason="simple_technique_question")
    provider = ScriptedProvider([("parsed", finish), ("parsed", finish)])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_validation_failed"
    assert store.get(state.id).suggestions == []


def test_turn_estimate_covers_tools_and_schema() -> None:
    from culinary_copilot.agent.loop import (
        AgentDirective,
        estimate_tokens,
        estimate_turn_input,
        function_defs_for,
        offered_tools,
    )
    from culinary_copilot.domain.sessions import SessionState as _S

    state = _S(id="ses-x")
    defs = offered_tools(state=state, excluded=set(), timeout_s=10.0)
    tool_defs = function_defs_for(defs)
    schema = AgentDirective.model_json_schema()
    items = [{"role": "user", "content": "hi"}]
    combined = estimate_turn_input(items, tool_defs)
    assert combined >= estimate_tokens(tool_defs) + estimate_tokens(schema)


def test_provider_bad_request_keeps_code_param_message(engine) -> None:
    err = ProviderBadRequestError(
        "provider rejected the request: 400",
        error_message="Invalid schema for function 'search_recipes'",
        error_code="invalid_schema",
        error_param="tools.0.parameters",
    )
    events: list[tuple[str, dict[str, Any]]] = []

    async def _on_stage(stage: str, detail: dict[str, Any]) -> None:
        events.append((stage, detail))

    provider = ScriptedProvider([("raise", err)])
    store = PostgresSessionStore(engine)
    state = _session(store)
    deps = _deps(store, provider)
    deps.on_stage = _on_stage
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=deps))
    assert excinfo.value.reason == "provider_bad_request"
    assert "invalid_schema" in excinfo.value.message
    assert "tools.0.parameters" in excinfo.value.message
    assert "Invalid schema for function" in excinfo.value.message
    payload = next(detail for stage, detail in events if stage == "provider_error")
    assert payload["reason"] == "provider_bad_request"
    assert payload["error_code"] == "invalid_schema"
    assert payload["error_param"] == "tools.0.parameters"
    assert payload["error_message"] == "Invalid schema for function 'search_recipes'"
    assert payload["attempts"] == 0
    assert payload["request_sent"] is False


def test_output_cap_is_min_of_max_and_remaining(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider([("tools", [("c1", "search_recipes", {"query": "x"})])])
    settings = _settings(llm_rec_max_output_tokens=1000)
    with pytest.raises(AgentLoopError):
        _run(run_agent(state.id, deps=_deps(store, provider, settings=settings)))
    assert provider.seen_caps[0] == min(1000, settings.agent_output_token_ceiling)

    store2 = PostgresSessionStore(engine)
    state2 = _session(store2)
    provider2 = ScriptedProvider([("tools", [("c1", "search_recipes", {"query": "x"})])])
    defaults = _settings()
    with pytest.raises(AgentLoopError):
        _run(run_agent(state2.id, deps=_deps(store2, provider2)))
    assert provider2.seen_caps[0] == min(
        defaults.llm_rec_max_output_tokens, defaults.agent_output_token_ceiling
    )


def test_output_minimum_stops_before_turn(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider([("tools", [("c1", "search_recipes", {"query": "x"})])])
    deps = _deps(
        store,
        provider,
        settings=_settings(agent_output_token_ceiling=400),
    )
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=deps))
    assert excinfo.value.reason == "agent_token_budget_exhausted"
    assert excinfo.value.http_status == 422
    assert provider.seen_inputs == []  # stopped before any provider call


def test_truncation_by_cap_stops_as_token_budget(engine) -> None:
    from culinary_copilot.llm.client import ProviderIncompleteError

    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "raise",
                ProviderIncompleteError(
                    "cut off",
                    attempts=1,
                    attempt_details=[],
                    request_sent=True,
                    input_tokens=100,
                    output_tokens=200,
                    incomplete_reason="max_output_tokens",
                ),
            )
        ]
    )
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_token_budget_exhausted"
    assert excinfo.value.http_status == 422
    assert excinfo.value.next_action == "change_request"


def test_sufficient_evidence_flow(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "curry"}),
                    ("c2", "find_balanced_pairings", {"ingredient": "chicken"}),
                    (
                        "c3",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                    ),
                    (
                        "c4",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                    ),
                ],
            ),
            (
                "parsed",
                _finish_options(
                    [
                        _opt(),
                        _opt(
                            source_id="lentil-2",
                            title="Red Lentil Soup",
                            quantities=[
                                {
                                    "ingredient": "red lentils",
                                    "amount": "200",
                                    "unit": "g",
                                }
                            ],
                        ),
                    ]
                ),
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert isinstance(result, AgentRunResult)
    assert result.stop_reason == "agent_sufficient_evidence"
    assert result.next_action is None
    assert result.phase == "recommend"
    stored = store.get(state.id)
    assert stored is not None and len(stored.suggestions) == 2
    assert stored.epicure_outcome is not None and "consulted:" in stored.epicure_outcome
    events = store.list_events(state.id)
    finished = [e for e in events if e.event_type == "agent_finished"]
    assert len(finished) == 1
    assert finished[0].payload["epicure_lines"]  # one line per suggestion


# --- invalid calls and tool errors ---------------------------------------------------


def _two_opts_no_quantities() -> list[dict[str, Any]]:
    return [
        _opt(quantities=[]),
        _opt(source_id="lentil-2", title="Red Lentil Soup", quantities=[]),
    ]


def test_unknown_tool_then_finish(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c0", "search_recipes", {"query": "curry"}),
                    ("c1", "nope_tool", {}),
                ],
            ),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(),
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    events = store.list_events(state.id)
    assert any(e.event_type == "agent_step" for e in events)


def test_bad_arguments_continues(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "raw_tools",
                [
                    ("c0", "search_recipes", '{"query": "curry"}'),
                    ("c1", "search_recipes", "{not json"),
                ],
            ),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(),
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"


def test_not_configured_never_retried(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    core = _FakeEpicureCore(enabled=False)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "find_balanced_pairings", {"ingredient": "chicken"}),
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
                ],
            ),
        ]
    )
    deps = _deps(store, provider, epicure_core=core)
    two = [
        _opt(),
        _opt(
            source_id="lentil-2",
            title="Red Lentil Soup",
            quantities=[{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
        ),
    ]
    provider.turns.append(("parsed", _finish_options(two)))
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert core.calls == 0  # disabled short-circuit: never queried, never retried
    assert "find_balanced_pairings" not in provider.seen_tools[1]
    stored = store.get(state.id)
    assert stored is not None
    assert stored.epicure_skip_reason == "epicure_not_configured"


def test_epicure_asset_missing_not_configured(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)

    class _MissingAssetCore:
        def __init__(self) -> None:
            self.settings = _settings(epicure_enabled=True)
            self.calls = 0

        def find_balanced_pairings(self, ingredient: str, k: int = 5):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise OSError("missing asset")

    core = _MissingAssetCore()
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "find_balanced_pairings", {"ingredient": "chicken"}),
                    ("c2", "find_conventional_pairings", {"ingredient": "chicken"}),
                    ("c3", "find_flavor_pairings", {"ingredient": "chicken"}),
                    (
                        "c4",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                    ),
                    (
                        "c5",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                    ),
                ],
            )
        ]
    )
    two = [
        _opt(),
        _opt(
            source_id="lentil-2",
            title="Red Lentil Soup",
            quantities=[{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
        ),
    ]
    provider.turns.append(("parsed", _finish_options(two)))
    deps = _deps(store, provider, settings=_settings(epicure_enabled=True), epicure_core=core)
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert core.calls == 3  # each variant queried once, none retried
    for missing in (
        "find_balanced_pairings",
        "find_conventional_pairings",
        "find_flavor_pairings",
    ):
        assert missing not in provider.seen_tools[1]


def test_transient_failures_lead_to_no_progress(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)

    def _down(args: Any, context: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "error_type": "unavailable",
            "reason": "tool_unavailable",
            "message": "db down",
            "next_action": "retry",
        }

    provider = ScriptedProvider(
        [
            ("tools", [("c1", "search_recipes", {"query": "x"})]),
            ("tools", [("c2", "search_recipes", {"query": "x"})]),
            ("tools", [("c3", "search_recipes", {"query": "x"})]),
        ]
    )
    deps = _deps(store, provider, overrides={"search_recipes": _down})
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=deps))
    assert excinfo.value.reason == "agent_no_progress"


def test_identical_successful_calls_stall(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            ("tools", [("c1", "search_recipes", {"query": "same"})]),
            ("tools", [("c2", "search_recipes", {"query": "same"})]),
            ("tools", [("c3", "search_recipes", {"query": "same"})]),
        ]
    )
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_no_progress"


# --- search_web filtering ------------------------------------------------------------


def test_search_web_filtered_by_permission(engine) -> None:
    store = PostgresSessionStore(engine)
    off = _session(store, internet_search_allowed=False)
    provider = ScriptedProvider(
        [
            ("tools", [("c0", "search_recipes", {"query": "curry"})]),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(),
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
        ]
    )
    _run(run_agent(off.id, deps=_deps(store, provider)))
    assert "search_web" not in provider.seen_tools[1]

    on = _session(store, internet_search_allowed=True)
    provider2 = ScriptedProvider(
        [
            ("tools", [("c0", "search_recipes", {"query": "curry"})]),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(),
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
        ]
    )
    _run(run_agent(on.id, deps=_deps(store, provider2)))
    assert "search_web" in provider2.seen_tools[1]


# --- yogurt ask-and-resume ---------------------------------------------------------------


def _ask_yogurt() -> dict[str, Any]:
    return {
        "decision": "ask_user",
        "question": {
            "question_id": "q-yogurt",
            "question_text": "Do you have plain yogurt at home?",
            "options": ["yes", "no"],
        },
        "note": "curry needs yogurt; user has not confirmed",
    }


def test_yogurt_ask_and_resume(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "chicken curry"}),
                    ("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
                ],
            ),
            ("parsed", _ask_yogurt()),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_needs_user_input"
    assert result.next_action is None
    assert result.phase == "clarify"
    assert result.final is not None and result.final["question"]["question_id"] == "q-yogurt"
    mid = store.get(state.id)
    assert mid is not None
    assert any(q.get("question_id") == "q-yogurt" for q in mid.unresolved_questions)

    answered = record_answer(
        store, state.id, expected_revision=mid.revision, question_id="q-yogurt", answer="no"
    )
    assert not any(q.get("question_id") == "q-yogurt" for q in answered.unresolved_questions)

    provider2 = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c3", "find_substitutions", {"ingredient": "yogurt"}),
                    (
                        "c4",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                    ),
                ],
            ),
            (
                "parsed",
                _finish_options(
                    [
                        _opt(
                            adaptations=[
                                {
                                    "description": "replace yogurt with coconut milk",
                                    "label": "adaptation",
                                }
                            ]
                        ),
                        _opt(
                            source_id="lentil-2",
                            title="Red Lentil Soup",
                            quantities=[
                                {
                                    "ingredient": "red lentils",
                                    "amount": "200",
                                    "unit": "g",
                                }
                            ],
                        ),
                    ]
                ),
            ),
        ]
    )
    result2 = _run(run_agent(state.id, deps=_deps(store, provider2)))
    assert result2.stop_reason == "agent_sufficient_evidence"
    final = store.get(state.id)
    assert final is not None
    assert any(
        a.get("question_id") == "q-yogurt" for a in final.confirmed_answers
    )  # kept across runs
    assert len(final.suggestions) == 2
    adaptation = final.suggestions[0]["adaptations"][0]
    assert adaptation["label"] == "adaptation"


# --- constraints, phases, epicure ------------------------------------------------------------


def test_hard_constraint_relaxation_rejected(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store, constraints={"dietary_constraints": ["vegetarian"]})
    finish = _finish_options([_opt()])
    provider = ScriptedProvider([("parsed", finish), ("parsed", finish)])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_validation_failed"
    assert store.get(state.id).suggestions == []
    events = store.list_events(state.id)
    assert any(e.event_type == "agent_validation_reject" for e in events)


def test_invalid_phase_move_is_defect(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store, current_phase="recommend")
    provider = ScriptedProvider([("parsed", _finish_options([_opt()], move_to="cook"))])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "invalid_phase_transition"


def test_epicure_required_when_enabled(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            ("parsed", _finish_options(_two_opts())),
            (
                "tools",
                [
                    ("c1", "find_balanced_pairings", {"ingredient": "chicken"}),
                    ("c2", "search_recipes", {"query": "hearty vegetarian"}),
                    (
                        "c3",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                    ),
                    (
                        "c4",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                    ),
                ],
            ),
            ("parsed", _finish_options(_two_opts())),
        ]
    )
    deps = _deps(store, provider, settings=_settings(epicure_enabled=True))
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    events = store.list_events(state.id)
    assert any(e.event_type == "agent_validation_reject" for e in events)
    stored = store.get(state.id)
    assert stored is not None and stored.epicure_outcome is not None


def _two_opts() -> list[dict[str, Any]]:
    return [
        _opt(),
        _opt(
            source_id="lentil-2",
            title="Red Lentil Soup",
            quantities=[{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
        ),
    ]


def test_epicure_skip_allowlisted(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c0", "search_recipes", {"query": "hearty vegetarian"}),
                    (
                        "c1",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                    ),
                    (
                        "c2",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                    ),
                ],
            ),
            (
                "parsed",
                _finish_options(_two_opts(), epicure_skip_reason="simple_technique_question"),
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert store.get(state.id).epicure_skip_reason == "simple_technique_question"


def test_epicure_skip_not_allowlisted_rejected_then_fixed(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c0", "search_recipes", {"query": "hearty vegetarian"}),
                    (
                        "c1",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                    ),
                    (
                        "c2",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                    ),
                ],
            ),
            ("parsed", _finish_options(_two_opts(), epicure_skip_reason="felt_like_it")),
            (
                "parsed",
                _finish_options(_two_opts(), epicure_skip_reason="simple_technique_question"),
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert store.get(state.id).epicure_skip_reason == "simple_technique_question"


def test_validation_failure_twice_stops(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    bad = _finish_options(
        [_opt(dataset_id="evil", source_id="666")],
        epicure_skip_reason="direct_recipe_lookup",
    )
    provider = ScriptedProvider([("parsed", bad), ("parsed", bad)])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_validation_failed"
    assert excinfo.value.next_action == "change_request"


# --- ordering, provider errors, injection ------------------------------------------------------


def test_parallel_call_order_and_budget(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    started: list[str] = []
    import time as _time

    def _slow_search(args: Any, context: Any) -> dict[str, Any]:
        started.append(args.query)
        _time.sleep({"first": 0.2, "second": 0.05, "third": 0.1}[args.query])
        return {
            "ok": True,
            "mode_ran": "fulltext",
            "cost_class": "free",
            "results": list(SEARCH_ROWS),
        }

    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "first"}),
                    ("c2", "search_recipes", {"query": "second"}),
                    ("c3", "search_recipes", {"query": "third"}),
                ],
            ),
            ("parsed", _finish_options(_two_opts_no_quantities())),
        ]
    )
    deps = _deps(store, provider, overrides={"search_recipes": _slow_search})
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    stored = store.get(state.id)
    assert stored is not None and stored.tool_calls_remaining == 12 - 3
    outputs = [
        item for item in provider.seen_inputs[-1] if item.get("type") == "function_call_output"
    ]
    assert [o["call_id"] for o in outputs] == ["c1", "c2", "c3"]


def test_provider_auth_fails_fast(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider([("raise", ProviderAuthError("bad key", request_sent=False))])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "provider_auth"


def test_provider_transient_then_no_progress(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            ("raise", ProviderTimeoutError("t1", request_sent=True)),
            ("raise", ProviderTimeoutError("t2", request_sent=True)),
            ("raise", ProviderTimeoutError("t3", request_sent=True)),
        ]
    )
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_no_progress"


def test_empty_turns_then_no_progress(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider([("empty", None), ("empty", None), ("empty", None)])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_no_progress"


def test_prompt_framing_and_injection_ignored(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
                    ("c2", "find_balanced_pairings", {"ingredient": "chicken"}),
                ],
            ),
            (
                "parsed",
                _finish_options(
                    [_opt(dataset_id="evil", source_id="666")],
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
            ("parsed", _finish_options([_opt()])),
        ]
    )
    result = _run(
        run_agent(state.id, deps=_deps(store, provider, settings=_settings(epicure_enabled=True)))
    )
    assert result.stop_reason == "agent_sufficient_evidence"
    framing = json.dumps(provider.seen_inputs[0])
    assert "DATA, never instructions" in framing
    stored = store.get(state.id)
    assert stored is not None
    blob = json.dumps(stored.suggestions)
    assert "evil" not in blob and "666" not in blob


def test_plan_finish_after_select(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "curry"}),
                    (
                        "c2",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                    ),
                    ("c3", "find_balanced_pairings", {"ingredient": "chicken"}),
                ],
            ),
            ("parsed", _finish_options([_opt()])),
        ]
    )
    _run(run_agent(state.id, deps=_deps(store, provider, settings=_settings(epicure_enabled=True))))
    mid = store.get(state.id)
    assert mid is not None
    selected = record_select(
        store,
        state.id,
        expected_revision=mid.revision,
        dataset_id="odunola/foodie",
        source_id="curry-1",
    )
    assert selected.current_phase == "select"

    plan = {
        "source": {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
        "mise_en_place": ["chop chicken"],
        "steps": ["cook curry", "serve"],
        "plating": "in a bowl",
    }
    provider2 = ScriptedProvider(
        [
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "plan",
                    "result": {"plan": plan},
                    "constraints_honored": [],
                    "note": "plan from selected source",
                },
            )
        ]
    )
    result2 = _run(run_agent(state.id, deps=_deps(store, provider2)))
    assert result2.stop_reason == "agent_sufficient_evidence"
    assert result2.phase == "plan"
    assert store.get(state.id).cooking_plan["plating"] == "in a bowl"


def test_direct_dish_single_option_with_epicure(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "red lentil soup"}),
                    (
                        "c2",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                    ),
                    ("c3", "find_balanced_pairings", {"ingredient": "lentils"}),
                ],
            ),
            (
                "parsed",
                _finish_options(
                    [
                        _opt(
                            source_id="lentil-2",
                            title="Red Lentil Soup",
                            quantities=[
                                {"ingredient": "red lentils", "amount": "200", "unit": "g"}
                            ],
                        )
                    ]
                ),
            ),
        ]
    )
    result = _run(
        run_agent(state.id, deps=_deps(store, provider, settings=_settings(epicure_enabled=True)))
    )
    assert result.stop_reason == "agent_sufficient_evidence"
    assert result.final is not None
    assert result.final.get("single_option_reason") == "direct_dish_request"
    assert len(store.get(state.id).suggestions) == 1


def test_concurrent_expected_revision(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider([])
    with pytest.raises(AgentConcurrentError):
        _run(run_agent(state.id, deps=_deps(store, provider), expected_revision=state.revision + 5))


def test_offered_tools_pure() -> None:
    state = SessionState(id="ses-x")
    defs = offered_tools(state=state, excluded=set(), timeout_s=10.0)
    names = {d.name for d in defs}
    assert "search_web" not in names  # permission off by default
    assert "search_recipes" in names
    on = SessionState(id="ses-y", internet_search_allowed=True)
    assert "search_web" in {d.name for d in offered_tools(state=on, excluded=set(), timeout_s=10.0)}
    excluded = offered_tools(state=on, excluded={"search_recipes"}, timeout_s=10.0)
    assert "search_recipes" not in {d.name for d in excluded}


def test_next_actions_for_loop_reasons() -> None:
    # Budgets are per session and never reset: start a new session.
    assert next_action_for("agent_max_steps") == "change_request"
    assert next_action_for("agent_tool_budget_exhausted") == "change_request"
    assert next_action_for("agent_token_budget_exhausted") == "change_request"
    # The wall clock is per run: a fresh attempt may succeed.
    assert next_action_for("agent_wall_clock_exceeded") == "retry"
    assert next_action_for("agent_no_progress") == "change_request"
    assert next_action_for("agent_validation_failed") == "change_request"
    assert next_action_for("unknown_question") == "change_request"
    assert next_action_for("unknown_option") == "change_request"


# --- P3-A-01: session retrieval evidence -------------------------------------------


def test_unretrieved_valid_id_rejected_at_finish(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    finish = _finish_options([_opt(quantities=[])])
    provider = ScriptedProvider([("parsed", finish), ("parsed", finish)])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_validation_failed"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert any("was not retrieved in this session" in str(e.payload) for e in rejects)


def test_failed_lookup_gives_no_support(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)

    def _fail_get(args: Any, context: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "error_type": "invalid_arguments",
            "reason": "tool_invalid_arguments",
            "message": "not found",
            "next_action": "change_request",
        }

    finish = _finish_options([_opt(quantities=[])])
    provider = ScriptedProvider(
        [
            (
                "tools",
                [("c1", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"})],
            ),
            ("parsed", finish),
            ("parsed", finish),
        ]
    )
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider, overrides={"get_recipe": _fail_get})))
    assert excinfo.value.reason == "agent_validation_failed"


def test_resumed_run_reuses_earlier_retrieval_evidence(engine) -> None:
    from culinary_copilot.agent.loop import recipe_session_evidence

    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [("c1", "search_recipes", {"query": "curry"})],
            ),
            ("parsed", _ask_yogurt()),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_needs_user_input"
    retrieved, _full = recipe_session_evidence(store=store, session_id=state.id)
    assert ("odunola/foodie", "curry-1") in retrieved
    # A later run of the same session sees the earlier evidence.
    retrieved2, _ = recipe_session_evidence(store=store, session_id=state.id)
    assert retrieved2 == retrieved


# --- P3-A-02: Epicure policy --------------------------------------------------------


def test_direct_lookup_skip_rejected_for_hearty_request(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    finish = _finish_options(
        [_opt(quantities=[]), _opt(source_id="lentil-2", title="x", quantities=[])],
        epicure_skip_reason="direct_recipe_lookup",
    )
    provider = ScriptedProvider([("parsed", finish), ("parsed", finish)])
    deps = _deps(store, provider, request_text="Vegetarian dinner, something hearty")
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=deps))
    assert excinfo.value.reason == "agent_validation_failed"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert any("not allowlisted" in str(e.payload) for e in rejects)


def test_pairing_cue_refuses_technique_skip(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            ("tools", [("c0", "search_recipes", {"query": "egg soup"})]),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(),
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(),
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
        ]
    )
    deps = _deps(
        store,
        provider,
        request_text="How do I boil an egg, and what soup goes with it?",
    )
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=deps))
    assert excinfo.value.reason == "agent_validation_failed"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert any("asks for a pairing" in str(e.payload) for e in rejects)


def test_pairing_guard_ignores_repair_wording(engine) -> None:
    from culinary_copilot.agent.loop import pairing_cue_in

    assert pairing_cue_in("How do I repair a split sauce?") is None
    assert pairing_cue_in("What soup goes with it?") == "goes with"
    assert pairing_cue_in("Which wines pair with fish?") == "pair"
    assert pairing_cue_in(None) is None


def test_epicure_not_configured_needs_confirmation(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    finish = _finish_options(
        _two_opts_no_quantities(), epicure_skip_reason="epicure_not_configured"
    )
    provider = ScriptedProvider([("parsed", finish), ("parsed", finish)])
    deps = _deps(store, provider, settings=_settings(epicure_enabled=True))
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=deps))
    assert excinfo.value.reason == "agent_validation_failed"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert any("confirms it is unavailable" in str(e.payload) for e in rejects)


def test_epicure_not_configured_recorded_degraded(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c0", "search_recipes", {"query": "curry"}),
                    (
                        "c1",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                    ),
                    (
                        "c2",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                    ),
                ],
            ),
            ("parsed", _finish_options(_two_opts())),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert result.final is not None and result.final.get("epicure_degraded") is True
    finished = [e for e in store.list_events(state.id) if e.event_type == "agent_finished"][0]
    assert finished.payload["epicure_skip_reason"] == "epicure_not_configured"
    assert finished.payload["epicure_degraded"] is True


# --- user messages -----------------------------------------------------------------


def test_user_message_reaches_provider_input(engine) -> None:
    from culinary_copilot.agent.loop import record_user_message

    store = PostgresSessionStore(engine)
    state = _session(store)
    record_user_message(store, state.id, text="chicken curry for dinner")
    provider = ScriptedProvider(
        [
            (
                "tools",
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
                ],
            ),
            (
                "parsed",
                _finish_options(
                    [_opt(), _opt(source_id="lentil-2", title="Red Lentil Soup")],
                    epicure_lines=[
                        {
                            "ingredient": "pork",
                            "decision": "used",
                            "reason": "crisp contrast for the curry",
                        }
                    ],
                ),
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    first_input = provider.seen_inputs[0]
    assert {"role": "user", "content": "chicken curry for dinner"} in first_input
    # User items come before tool history; the snapshot framing stays last.
    roles = [i.get("role", i.get("type")) for i in first_input]
    assert roles[-1] == "user"
    assert first_input[-1].get("content", "").startswith("You are a cooking assistant")


def test_user_message_survives_answer_resume(engine) -> None:
    from culinary_copilot.agent.loop import record_answer, record_user_message

    store = PostgresSessionStore(engine)
    state = _session(store)
    record_user_message(store, state.id, text="plain yogurt question")
    ask = {
        "decision": "ask_user",
        "question": {
            "question_id": "q-1",
            "question_text": "Do you have plain yogurt?",
            "options": ["yes", "no"],
        },
        "note": "ask",
    }
    provider = ScriptedProvider([("parsed", ask)])
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_needs_user_input"
    current = store.get(state.id)
    record_answer(
        store, state.id, expected_revision=current.revision, question_id="q-1", answer="yes"
    )
    provider2 = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "curry"}),
                    (
                        "c2",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                    ),
                ],
            ),
            (
                "parsed",
                _finish_options(
                    [_opt(), _opt(source_id="lentil-2", title="Red Lentil Soup")],
                    epicure_lines=[
                        {
                            "ingredient": "pork",
                            "decision": "used",
                            "reason": "crisp contrast for the curry",
                        }
                    ],
                ),
            ),
        ]
    )
    result2 = _run(run_agent(state.id, deps=_deps(store, provider2)))
    assert result2.stop_reason == "agent_sufficient_evidence"
    assert {"role": "user", "content": "plain yogurt question"} in provider2.seen_inputs[0]


def test_effective_request_text_derivation() -> None:
    from culinary_copilot.agent.loop import effective_request_text

    assert effective_request_text("explicit", ["stored"]) == "explicit"
    assert effective_request_text(None, ["first", "latest"]) == "latest"
    assert effective_request_text("", ["stored"]) == "stored"
    assert effective_request_text(None, []) is None
    assert effective_request_text(None, None) is None


def test_user_message_window_and_bounds(engine) -> None:
    from culinary_copilot.agent.loop import (
        USER_MESSAGE_CHARS,
        USER_MESSAGE_KEEP,
        record_user_message,
        user_messages_from_events,
    )

    store = PostgresSessionStore(engine)
    state = _session(store)
    for i in range(8):
        record_user_message(store, state.id, text=f"message {i}")
    record_user_message(store, state.id, text="x" * (USER_MESSAGE_CHARS + 500))
    messages = user_messages_from_events(store, state.id)
    assert len(messages) == USER_MESSAGE_KEEP
    assert messages[-1] == "x" * USER_MESSAGE_CHARS
    assert messages[0] == "message 4"


# --- history cap and pairing -------------------------------------------------------


def _synthetic_history(rng: Any, turns: int) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for turn in range(turns):
        if rng.random() < 0.3:
            history.append({"type": "reasoning", "id": f"rs-{turn}"})
        width = 1 + rng.randrange(3)
        for w in range(width):
            history.append(
                {
                    "type": "function_call",
                    "call_id": f"c{turn}-{w}",
                    "name": "search_recipes",
                    "arguments": "{}",
                }
            )
        for w in range(width):
            history.append(
                {"type": "function_call_output", "call_id": f"c{turn}-{w}", "output": "{}"}
            )
        if rng.random() < 0.2:
            history.append({"role": "user", "content": f"note {turn}"})
    return history


def test_cap_history_keeps_whole_paired_groups() -> None:
    import random

    from culinary_copilot.agent.loop import (
        _HISTORY_KEEP,
        _cap_history,
        history_pairing_violations,
    )

    rng = random.Random(20260930)
    for trial in range(200):
        history = _synthetic_history(rng, turns=1 + rng.randrange(12))
        assert history_pairing_violations(history) == []
        capped = _cap_history(history)
        # Pairing invariant holds after capping.
        assert history_pairing_violations(capped) == []
        if len(history) <= _HISTORY_KEEP:
            assert capped == history
        else:
            # Newest turn group is intact (its calls survive whole).
            newest_call = next(
                i.get("call_id") for i in reversed(history) if i.get("type") == "function_call"
            )
            call_ids = [i.get("call_id") for i in capped if i.get("type") == "function_call"]
            assert newest_call in call_ids
            # Order preserved (subsequence) and never longer.
            assert len(capped) <= len(history)
            full = [json.dumps(i, sort_keys=True, default=str) for i in history]
            flat = [json.dumps(i, sort_keys=True, default=str) for i in capped]
            pos = 0
            for item in flat:
                pos = full.index(item, pos) + 1


def test_cap_history_never_pins_first_item() -> None:
    from culinary_copilot.agent.loop import _cap_history, history_pairing_violations

    history: list[dict[str, Any]] = []
    for turn in range(12):
        history.append(
            {
                "type": "function_call",
                "call_id": f"old-{turn}",
                "name": "search_recipes",
                "arguments": "{}",
            }
        )
        history.append({"type": "function_call_output", "call_id": f"old-{turn}", "output": "{}"})
    assert len(history) == 24  # over the cap: oldest whole groups must drop
    capped = _cap_history(history)
    assert history_pairing_violations(capped) == []
    kept_ids = [i.get("call_id") for i in capped if i.get("type") == "function_call"]
    assert "old-0" not in kept_ids  # the old pin-everything-first bug
    assert "old-11" in kept_ids  # the newest group survives whole


def test_duplicate_call_id_fails_locally_without_sending(engine) -> None:
    from culinary_copilot.agent.loop import _cap_history  # noqa: F401 (cap import smoke)

    sent = {"n": 0}

    class _CountingProvider(ScriptedProvider):
        async def complete_native_tool_turn(self, **kwargs: Any) -> Any:  # type: ignore[override]
            sent["n"] += 1
            return await super().complete_native_tool_turn(**kwargs)

    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = _CountingProvider(
        [
            ("tools", [("dup", "search_recipes", {"query": "a"})]),
            ("tools", [("dup", "search_recipes", {"query": "b"})]),
            ("parsed", _finish_options([_opt()])),
        ]
    )
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "history_pairing_error"
    assert sent["n"] == 2  # the paired first two turns sent; the third never was
