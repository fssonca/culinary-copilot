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
BROTH_DOC = {
    "dataset_id": "odunola/foodie",
    "source_id": "broth-9",
    "title": "Simple Vegetable Broth",
    "servings": 4.0,
    "ingredients": [
        {
            "canonical": "vegetable bouillon",
            "amount": "2",
            "unit": "cubes",
            "quantity_text": "2 cubes",
        },
    ],
}
DOCS = {
    ("odunola/foodie", "curry-1"): CURRY_DOC,
    ("odunola/foodie", "lentil-2"): LENTIL_DOC,
    ("odunola/foodie", "broth-9"): BROTH_DOC,
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
    # Consulted-Epicure behavior needs Epicure enabled (disabled
    # settings no longer offer the pairing tools at all).
    result = _run(
        run_agent(state.id, deps=_deps(store, provider, settings=_settings(epicure_enabled=True)))
    )
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
            (
                "parsed",
                _finish_options(
                    _two_opts(),
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "crisp contrast"}
                    ],
                ),
            ),
        ]
    )
    _run(run_agent(state.id, deps=_deps(store, provider, settings=_settings(epicure_enabled=True))))
    finished = [e for e in store.list_events(state.id) if e.event_type == "agent_finished"][0]
    assert finished.payload["epicure_lines"]
    assert any(line.startswith("derived:") for line in finished.payload["epicure_lines"])


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
                    ],
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "crisp contrast"}
                    ],
                ),
            ),
        ]
    )
    result = _run(
        run_agent(state.id, deps=_deps(store, provider, settings=_settings(epicure_enabled=True)))
    )
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


def test_invalid_phase_move_is_feedback_not_terminal(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    bad_finish = _finish_options(_two_opts(), move_to="plan")
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
            ("parsed", bad_finish),
            ("parsed", _finish_options(_two_opts())),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert len(rejects) == 1
    assert any("invalid phase move" in str(e) for e in rejects[0].payload["errors"])


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
            (
                "parsed",
                _finish_options(
                    _two_opts(),
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "crisp contrast"}
                    ],
                ),
            ),
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


def test_reservation_breach_propagates_unmapped(engine) -> None:
    """A spend-guard breach is never mapped to a provider error terminal."""

    class _Breach(RuntimeError):
        def __init__(self) -> None:
            super().__init__("reservation breach on model-turn-1")
            self.reservation_breach = True

    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider([("raise", _Breach())])
    with pytest.raises(_Breach):
        _run(run_agent(state.id, deps=_deps(store, provider)))


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
            (
                "parsed",
                _finish_options(
                    [_opt()],
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "crisp contrast"}
                    ],
                ),
            ),
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
            (
                "parsed",
                _finish_options(
                    [_opt()],
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "crisp contrast"}
                    ],
                ),
            ),
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
                "tools",
                [("c9", "search_techniques", {"query": "safe internal temperatures"})],
            ),
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "plan",
                    "result": {
                        "plan": {
                            **plan,
                            "technique_refs": [{"doc_id": "tech-fda-safe-32", "chunk_id": 0}],
                        }
                    },
                    "constraints_honored": [],
                    "note": "plan from selected source",
                },
            ),
        ]
    )
    deps2 = _deps(
        store,
        provider2,
        overrides={"search_techniques": _tech_search([_safety_row()])},
    )
    deps2.technique_resolver = lambda doc_id, chunk_id: (
        dict(_safety_row()) if (doc_id, chunk_id) == ("tech-fda-safe-32", 0) else None
    )
    result2 = _run(run_agent(state.id, deps=deps2))
    assert result2.stop_reason == "agent_sufficient_evidence"
    assert result2.phase == "plan"
    assert store.get(state.id).cooking_plan["plating"] == "in a bowl"
    assert result2.final is not None
    assert result2.final["plan"]["steps_source"] == "source"


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
                    ],
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "crisp contrast"}
                    ],
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


def test_offered_search_modes_follow_embeddings_setting() -> None:
    # Phase 7 live fix: vector is absent from the offered enum without
    # embeddings, present with them; requesting it anyway fails closed.
    import json as _json

    from culinary_copilot.config import Settings
    from culinary_copilot.tools.registry import strict_parameters_schema

    state = SessionState(id="ses-m")
    off = {
        d.name: d
        for d in offered_tools(
            state=state,
            excluded=set(),
            timeout_s=10.0,
            settings=Settings(_env_file=None, embeddings_enabled=False),
        )
    }
    off_mode = strict_parameters_schema(off["search_recipes"].args_model)["properties"]["mode"]
    assert "vector" not in _json.dumps(off_mode)
    with pytest.raises(Exception):
        off["search_recipes"].args_model.model_validate({"query": "soup", "mode": "vector"})
    on = {
        d.name: d
        for d in offered_tools(
            state=state,
            excluded=set(),
            timeout_s=10.0,
            settings=Settings(_env_file=None, embeddings_enabled=True),
        )
    }
    assert (
        on["search_recipes"].args_model.model_validate({"query": "soup", "mode": "vector"}).mode
        == "vector"
    )


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


def test_one_not_configured_excludes_whole_pairing_family(engine) -> None:
    from culinary_copilot.agent.loop import _PAIRING_TOOLS
    from culinary_copilot.domain.recommendations import REASON_TOOL_NOT_CONFIGURED

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
            ("tools", [("c1", "find_balanced_pairings", {"ingredient": "chicken"})]),
            (
                "tools",
                [
                    ("c2", "find_conventional_pairings", {"ingredient": "chicken"}),
                    ("c3", "search_recipes", {"query": "hearty vegetarian"}),
                ],
            ),
            (
                "tools",
                [
                    ("c4", "find_flavor_pairings", {"ingredient": "chicken"}),
                    (
                        "c5",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                    ),
                ],
            ),
            (
                "tools",
                [
                    ("c6", "find_substitutions", {"ingredient": "chicken"}),
                    (
                        "c7",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                    ),
                ],
            ),
            ("parsed", _finish_options(_two_opts())),
        ]
    )
    deps = _deps(store, provider, settings=_settings(epicure_enabled=True), epicure_core=core)
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    # Exactly one tool_not_configured: the first pairing call reaches the
    # backend; the siblings are unoffered from the next turn on.
    not_configured = [
        e
        for e in store.list_events(state.id)
        if e.event_type == "tool_call" and e.payload.get("reason") == REASON_TOOL_NOT_CONFIGURED
    ]
    assert len(not_configured) == 1
    assert not_configured[0].payload["tool"] == "find_balanced_pairings"
    assert core.calls == 1
    for seen in provider.seen_tools[1:5]:
        assert not (set(seen) & set(_PAIRING_TOOLS)), f"pairing tools re-offered: {seen}"


def test_disabled_epicure_offers_no_pairing_tools_and_degrades(engine) -> None:
    from culinary_copilot.agent.loop import _PAIRING_TOOLS, offered_tools

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
            ("parsed", _finish_options(_two_opts())),
        ]
    )
    deps = _deps(store, provider, settings=_settings(epicure_enabled=False))
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    for seen in provider.seen_tools:
        assert not (set(seen) & set(_PAIRING_TOOLS)), f"pairing tools offered: {seen}"
    assert (
        offered_tools(
            state=store.get(state.id), excluded=set(), timeout_s=10.0, epicure_enabled=False
        )
        is not None
    )
    assert not (
        set(
            d.name
            for d in offered_tools(
                state=store.get(state.id),
                excluded=set(),
                timeout_s=10.0,
                epicure_enabled=False,
            )
        )
        & set(_PAIRING_TOOLS)
    )
    stored = store.get(state.id)
    assert stored is not None
    assert stored.epicure_skip_reason == "epicure_not_configured"
    assert result.final is not None and result.final.get("epicure_degraded") is True


def test_enabled_epicure_offers_pairing_tools_and_marks_available(engine) -> None:
    from culinary_copilot.agent.loop import (
        _PAIRING_TOOLS,
        _TASK_FRAMING,
        build_turn_input,
        offered_tools,
    )

    store = PostgresSessionStore(engine)
    state = _session(store)
    names = {
        d.name
        for d in offered_tools(state=state, excluded=set(), timeout_s=10.0, epicure_enabled=True)
    }
    assert set(_PAIRING_TOOLS) <= names
    items = build_turn_input(state=state, history=[], last_outcome=None, epicure_available=True)
    assert items[-1]["content"] and "epicure_available" in items[-1]["content"]
    assert '"epicure_available": true' in items[-1]["content"]
    items_down = build_turn_input(
        state=state, history=[], last_outcome=None, epicure_available=False
    )
    assert '"epicure_available": false' in items_down[-1]["content"]
    assert "simple_technique_question (a technique-only question with no pairing cue)" in (
        _TASK_FRAMING
    )
    assert "epicure_not_configured (Epicure is unavailable, and the answer is marked degraded)" in (
        _TASK_FRAMING
    )


def _retrieval_turn() -> tuple[str, Any]:
    return (
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
    )


def test_final_turn_sends_no_tools_and_accepts_finish(engine) -> None:
    from culinary_copilot.agent.loop import REASON_AGENT_SUFFICIENT

    store = PostgresSessionStore(engine)
    state = _session(store, steps_remaining=2)
    provider = ScriptedProvider([_retrieval_turn(), ("parsed", _finish_options(_two_opts()))])
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == REASON_AGENT_SUFFICIENT
    assert provider.seen_tools[1] == []
    assert "Final step: no tools remain" in provider.seen_inputs[1][-1]["content"]


def test_final_turn_rejection_ends_with_budget_stop(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store, steps_remaining=2)
    bad_finish = {
        "decision": "finish",
        "move_to": "recommend",
        "constraints_honored": [],
        "note": "no result",
    }
    provider = ScriptedProvider([_retrieval_turn(), ("parsed", bad_finish)])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_max_steps"
    assert provider.seen_tools[1] == []


def test_final_turn_exhausted_tool_budget_reports_tool_stop(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store, steps_remaining=8, tool_calls_remaining=3)
    bad_finish = {
        "decision": "finish",
        "move_to": "recommend",
        "constraints_honored": [],
        "note": "no result",
    }
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "a"}),
                    ("c2", "search_recipes", {"query": "b"}),
                    ("c3", "search_recipes", {"query": "c"}),
                ],
            ),
            ("parsed", bad_finish),
        ]
    )
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_tool_budget_exhausted"
    assert provider.seen_tools[1] == []


def test_invalid_ask_phase_move_is_feedback(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    bad_ask = {
        "decision": "ask_user",
        "move_to": "plan",
        "question": {
            "question_id": "q1",
            "question_text": "Which dish?",
            "options": ["a", "b"],
        },
        "note": "bad move",
    }
    good_ask = {
        "decision": "ask_user",
        "question": {
            "question_id": "q1",
            "question_text": "Which dish?",
            "options": ["a", "b"],
        },
        "note": "good move",
    }
    provider = ScriptedProvider([("parsed", bad_ask), ("parsed", good_ask)])
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_needs_user_input"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert len(rejects) == 1
    assert any("invalid phase move" in str(e) for e in rejects[0].payload["errors"])


def test_evidence_digest_lists_searches_fetches_pairings_and_techniques(engine) -> None:
    from culinary_copilot.agent.loop import session_evidence_digest

    store = PostgresSessionStore(engine)
    state = _session(store)

    def _techniques(args: Any, context: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode_ran": "fulltext",
            "match": "all",
            "cost_class": "free",
            "results": [{"doc_id": "tech-egg-1", "chunk_id": 0, "title": "Boiled egg"}],
        }

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
                    ("c2", "find_balanced_pairings", {"ingredient": "chicken"}),
                    ("c3", "search_techniques", {"query": "how to boil an egg"}),
                ],
            ),
            (
                "parsed",
                _finish_options(_two_opts(), epicure_skip_reason="simple_technique_question"),
            ),
        ]
    )
    deps = _deps(
        store,
        provider,
        settings=_settings(epicure_enabled=True),
        overrides={"search_techniques": _techniques},
    )
    deps.tool_context.record_tool_args = True
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    digest = session_evidence_digest(store, state.id)
    assert "search search_recipes query='hearty vegetarian' mode=fulltext results=2" in digest
    assert "fetched odunola/foodie:curry-1 'Creamy Chicken Curry'" in digest
    assert (
        "epicure find_balanced_pairings requested='chicken' as='chicken' top=[pork, beef]" in digest
    )
    assert "technique tech-egg-1#0 'Boiled egg'" in digest
    assert len(digest) <= 1500


def test_evidence_digest_bounded_and_newest_first(engine) -> None:
    from culinary_copilot.agent.loop import session_evidence_digest

    store = PostgresSessionStore(engine)
    state = _session(store)
    for i in range(120):
        store.append_event(
            state.id,
            "tool_call",
            {
                "call_id": f"c{i}",
                "tool": "search_recipes",
                "outcome": "ok",
                "mode_ran": "fulltext",
                "result_count": 2,
                "args": f'{{"query": "dish number {i:03d} padding padding"}}',
            },
        )
    digest = session_evidence_digest(store, state.id)
    assert len(digest) <= 1500
    assert "dish number 119" in digest
    assert "dish number 000" not in digest


def _tech_answer(text: str, refs: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    directive: dict[str, Any] = {
        "decision": "finish",
        "move_to": "recommend",
        "result": {"technique_answer": {"text": text, "technique_refs": refs}},
        "constraints_honored": [],
        "note": "test technique answer",
    }
    directive.update(kw)
    return directive


def _tech_row(doc_id: str = "tech-egg-1", chunk_id: int = 0) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "title": "Boiled egg",
        "url": "https://example.test/egg",
        "licence": "CC-BY-SA-4.0",
        "licence_url": "https://example.test/licence",
        "attribution_text": "Egg corpus (CC BY-SA 4.0)",
        "chunk_text": "Simmer eggs 6 minutes, then plunge into an ice bath.",
    }


def _safety_row(chunk_id: int = 0) -> dict[str, Any]:
    """Food-safety technique chunk (P3-L-09 raw-protein support)."""
    return {
        "doc_id": "tech-fda-safe-32",
        "chunk_id": chunk_id,
        "section": "Safe Food Handling",
        "title": "Safe Food Handling",
        "url": "https://example.test/safe",
        "licence": "CC-BY-SA-4.0",
        "licence_url": "https://example.test/licence",
        "attribution_text": "Safe corpus (CC BY-SA 4.0)",
        "chunk_text": "Cook poultry to a safe internal temperature of 165°F (74°C).",
    }


class _VocabEpicureCore(_FakeEpicureCore):
    """Fake core with a vocabulary hook for claim-grounding tests."""

    def __init__(self, vocabulary: set[str], **kw: Any) -> None:
        super().__init__(**kw)
        self._vocabulary = set(vocabulary)

    def vocabulary(self) -> set[str]:
        return set(self._vocabulary)


def _tech_search(rows: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    def _impl(args: Any, context: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode_ran": "fulltext",
            "match": "all",
            "cost_class": "free",
            "results": list(rows),
        }

    return _impl


def test_options_after_select_rejected_then_plan_accepted(engine) -> None:
    from culinary_copilot.agent.loop import record_select

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
            (
                "parsed",
                _finish_options(
                    [_opt()],
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "crisp contrast"}
                    ],
                ),
            ),
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
            ("parsed", _finish_options([_opt()])),
            (
                "tools",
                [("c9", "search_techniques", {"query": "safe internal temperatures"})],
            ),
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "plan",
                    "result": {
                        "plan": {
                            **plan,
                            "technique_refs": [{"doc_id": "tech-fda-safe-32", "chunk_id": 0}],
                        }
                    },
                    "constraints_honored": [],
                    "note": "plan from selected source",
                },
            ),
        ]
    )
    deps2 = _deps(
        store,
        provider2,
        overrides={"search_techniques": _tech_search([_safety_row()])},
    )
    deps2.technique_resolver = lambda doc_id, chunk_id: (
        dict(_safety_row()) if (doc_id, chunk_id) == ("tech-fda-safe-32", 0) else None
    )
    result2 = _run(run_agent(state.id, deps=deps2))
    assert result2.stop_reason == "agent_sufficient_evidence"
    assert result2.phase == "plan"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert len(rejects) == 1
    assert rejects[0].payload["errors"] == [
        "A dish is selected (odunola/foodie/curry-1): return the cooking plan for it."
    ]


def test_constraints_honored_rejects_free_text_and_names_missing_key(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store, constraints={"dietary_constraints": ["vegetarian"]})
    provider = ScriptedProvider(
        [
            _retrieval_turn(),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(),
                    constraints_honored=["Avoided yogurt-based chicken styles"],
                ),
            ),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(), constraints_honored=["dietary_constraints"]
                ),
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert result.final is not None
    assert result.final.get("constraints_honored") == ["dietary_constraints"]
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert len(rejects) == 1
    errors = rejects[0].payload["errors"]
    assert any("not a session constraint" in e and "note instead" in e for e in errors)
    assert any(
        "dropped hard constraint: dietary_constraints" in e and "every option must satisfy it" in e
        for e in errors
    )


def test_pairing_cue_requires_three_lines(engine) -> None:
    class _ThreePairCore:
        def __init__(self) -> None:
            self.settings = _settings(epicure_enabled=True)

        def find_balanced_pairings(self, ingredient: str, k: int = 5):  # type: ignore[no-untyped-def]
            from culinary_copilot.tools.epicure import Pairing

            pairs = [("pork", 0.5), ("beef", 0.4), ("garlic", 0.3)]
            return [Pairing(ingredient=n, score=s) for n, s in pairs[:k]]

    def _lines(*names: str) -> list[dict[str, Any]]:
        return [{"ingredient": n, "decision": "used", "reason": "roast match"} for n in names]

    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "roast chicken"}),
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
            ),
            (
                "parsed",
                _finish_options(_two_opts_no_quantities(), epicure_lines=_lines("pork", "beef")),
            ),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(), epicure_lines=_lines("pork", "beef", "garlic")
                ),
            ),
        ]
    )
    deps = _deps(
        store,
        provider,
        settings=_settings(epicure_enabled=True),
        epicure_core=_ThreePairCore(),
        request_text="Chicken pairings for a roast.",
    )
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert len(rejects) == 1
    assert any("at least 3 epicure_lines" in e for e in rejects[0].payload["errors"])


def test_unknown_epicure_line_ingredient_rejected(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "roast chicken"}),
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
            ),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(),
                    epicure_lines=[
                        {"ingredient": "unicorn", "decision": "used", "reason": "mythic"}
                    ],
                ),
            ),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(),
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "roast match"}
                    ],
                ),
            ),
        ]
    )
    deps = _deps(store, provider, settings=_settings(epicure_enabled=True))
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert len(rejects) == 1
    assert any("unicorn" in e and "was not returned" in e for e in rejects[0].payload["errors"])


def test_technique_answer_accepted_with_attribution(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            ("tools", [("c1", "search_techniques", {"query": "how to boil an egg"})]),
            (
                "parsed",
                _tech_answer(
                    "Simmer eggs 6 minutes, then ice bath.",
                    [{"doc_id": "tech-egg-1", "chunk_id": 0}],
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
        ]
    )
    deps = _deps(
        store,
        provider,
        settings=_settings(epicure_enabled=True),
        overrides={"search_techniques": _tech_search([_tech_row()])},
        request_text="How do I boil an egg?",
    )
    deps.technique_resolver = lambda doc_id, chunk_id: dict(_tech_row(doc_id, chunk_id))
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert result.final is not None
    answered = result.final.get("technique_answer")
    assert answered is not None
    assert answered["text"] == "Simmer eggs 6 minutes, then ice bath."
    assert answered["technique_refs"] == [{"doc_id": "tech-egg-1", "chunk_id": 0}]
    assert answered["attribution"] == [
        {
            "doc_id": "tech-egg-1",
            "chunk_id": 0,
            "attribution_text": "Egg corpus (CC BY-SA 4.0)",
            "licence_url": "https://example.test/licence",
        }
    ]
    assert result.final.get("epicure_skip_reason") == "simple_technique_question"
    assert "options" not in result.final and "plan" not in result.final


def test_technique_answer_unreturned_chunk_rejected(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            ("tools", [("c1", "search_techniques", {"query": "how to boil an egg"})]),
            (
                "parsed",
                _tech_answer(
                    "Mystery method.",
                    [{"doc_id": "tech-other-9", "chunk_id": 3}],
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
            (
                "parsed",
                _tech_answer(
                    "Simmer eggs 6 minutes, then ice bath.",
                    [{"doc_id": "tech-egg-1", "chunk_id": 0}],
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
        ]
    )
    deps = _deps(
        store,
        provider,
        settings=_settings(epicure_enabled=True),
        overrides={"search_techniques": _tech_search([_tech_row()])},
        request_text="How do I boil an egg?",
    )
    deps.technique_resolver = lambda doc_id, chunk_id: dict(_tech_row(doc_id, chunk_id))
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert len(rejects) == 1
    assert any("was not returned in this session" in e for e in rejects[0].payload["errors"])


def test_technique_answer_pairing_cue_refused(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            ("tools", [("c1", "search_techniques", {"query": "egg sides"})]),
            (
                "parsed",
                _tech_answer(
                    "Serve with toast.",
                    [{"doc_id": "tech-egg-1", "chunk_id": 0}],
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
            ("parsed", _finish_options(_two_opts_no_quantities())),
        ]
    )
    deps = _deps(
        store,
        provider,
        settings=_settings(epicure_enabled=True),
        overrides={"search_techniques": _tech_search([_tech_row()])},
        request_text="What goes with roast chicken?",
    )
    deps.technique_resolver = lambda doc_id, chunk_id: dict(_tech_row(doc_id, chunk_id))
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=deps))
    assert excinfo.value.reason == "agent_validation_failed"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert any("simple_technique_question refused" in e for e in rejects[0].payload["errors"])


def test_dropped_option_errors_are_readable(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    bad_lentil = _opt(
        source_id="lentil-2",
        title="Red Lentil Soup",
        quantities=[{"ingredient": "red lentils", "amount": "999", "unit": "g"}],
    )
    ghost = _opt(dataset_id="evil", source_id="666", title="Ghost", quantities=[])
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
            ("parsed", _finish_options([_opt(), ghost, bad_lentil], note="Two options for you")),
            ("tools", [("c4", "find_balanced_pairings", {"ingredient": "chicken"})]),
            (
                "parsed",
                _finish_options(
                    [_opt()],
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "crisp contrast"}
                    ],
                ),
            ),
        ]
    )
    result = _run(
        run_agent(state.id, deps=_deps(store, provider, settings=_settings(epicure_enabled=True)))
    )
    assert result.stop_reason == "agent_sufficient_evidence"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert len(rejects) == 1
    errors = rejects[0].payload["errors"]
    assert any(e.startswith("option 1 (") and "'Ghost'" in e and "evil/666" in e for e in errors), (
        errors
    )
    assert any(e.startswith("option 2 (") and "lentil-2" in e for e in errors), errors
    assert not any("option index:" in e for e in errors)


def test_two_survivors_accepted_with_drops_reported(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    ghost = _opt(dataset_id="evil", source_id="666", title="Ghost", quantities=[])
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
                    ("c4", "find_balanced_pairings", {"ingredient": "chicken"}),
                ],
            ),
            (
                "parsed",
                _finish_options(
                    [_opt(), _two_opts()[1], ghost],
                    note="Two options for you",
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "crisp contrast"}
                    ],
                ),
            ),
        ]
    )
    deps = _deps(store, provider, settings=_settings(epicure_enabled=True))
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert result.final is not None
    assert len(result.final["options"]) == 2
    assert result.final.get("single_option_reason") is None
    dropped = result.final["dropped_options"]
    assert dropped == [
        {
            "index": 2,
            "title": "Ghost",
            "source_id": "666",
            "error": "(evil, 666) not in corpus (unsourced ID)",
        }
    ]
    assert (
        result.final["note"] == "1 option(s) were removed because they failed source checks: Ghost."
    )
    assert result.final["epicure_lines"]
    finished = [e for e in store.list_events(state.id) if e.event_type == "agent_finished"][0]
    assert finished.payload["model_note"] == "Two options for you"
    assert finished.payload["note"] == result.final["note"]


def test_lone_survivor_among_two_drops_accepted(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    ghost = _opt(dataset_id="evil", source_id="666", title="Ghost", quantities=[])
    bad_lentil = _opt(
        source_id="lentil-2",
        title="Red Lentil Soup",
        quantities=[{"ingredient": "red lentils", "amount": "999", "unit": "g"}],
    )
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
                    ("c4", "find_balanced_pairings", {"ingredient": "chicken"}),
                ],
            ),
            (
                "parsed",
                _finish_options(
                    [_opt(), ghost, bad_lentil],
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "crisp contrast"}
                    ],
                ),
            ),
        ]
    )
    deps = _deps(store, provider, settings=_settings(epicure_enabled=True))
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert result.final is not None
    assert result.final.get("single_option_reason") == "only_one_valid_candidate"
    assert len(result.final["options"]) == 1
    assert len(result.final["dropped_options"]) == 2
    assert result.final["note"].startswith("2 option(s) were removed")


def test_technique_final_carries_note_and_deduped_attribution(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    rows = [_tech_row(chunk_id=0), _tech_row(chunk_id=1)]
    provider = ScriptedProvider(
        [
            ("tools", [("c1", "search_techniques", {"query": "how to boil an egg"})]),
            (
                "parsed",
                _tech_answer(
                    "Simmer eggs 6 minutes, then ice bath.",
                    [
                        {"doc_id": "tech-egg-1", "chunk_id": 0},
                        {"doc_id": "tech-egg-1", "chunk_id": 1},
                    ],
                    epicure_skip_reason="simple_technique_question",
                    note="model note here",
                ),
            ),
        ]
    )
    deps = _deps(
        store,
        provider,
        settings=_settings(epicure_enabled=True),
        overrides={"search_techniques": _tech_search(rows)},
        request_text="How do I boil an egg?",
    )
    by_chunk = {(r["doc_id"], r["chunk_id"]): r for r in rows}
    deps.technique_resolver = lambda doc_id, chunk_id: dict(by_chunk[(doc_id, chunk_id)])
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert result.final is not None
    assert result.final.get("note") == "model note here"
    answered = result.final["technique_answer"]
    assert answered["technique_refs"] == [
        {"doc_id": "tech-egg-1", "chunk_id": 0},
        {"doc_id": "tech-egg-1", "chunk_id": 1},
    ]
    assert answered["attribution"] == [
        {
            "doc_id": "tech-egg-1",
            "chunk_id": 0,
            "attribution_text": "Egg corpus (CC BY-SA 4.0)",
            "licence_url": "https://example.test/licence",
        }
    ]


# --- P3-L-07 minimum hard-constraint control ---------------------------------


def test_vegetarian_chicken_stock_option_dropped(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store, constraints={"dietary_constraints": ["vegetarian"]})
    provider = ScriptedProvider(
        [
            _retrieval_turn(),
            (
                "parsed",
                _finish_options(
                    _two_opts(),
                    constraints_honored=["dietary_constraints"],
                    note="Two options, one dropped for chicken.",
                ),
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert result.final is not None
    assert [o["source_id"] for o in result.final["options"]] == ["lentil-2"]
    assert result.final.get("single_option_reason") == "only_one_valid_candidate"
    assert result.final.get("note_source") == "server"
    checks = result.final.get("constraint_check")
    assert checks == [
        {
            "index": 0,
            "source_id": "curry-1",
            "status": "violated",
            "value": "vegetarian",
            "terms": ["chicken"],
        },
        {"index": 1, "source_id": "lentil-2", "status": "checked", "value": "vegetarian"},
    ]


def test_vegetable_bouillon_passes_vegetarian(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store, constraints={"dietary_constraints": ["vegetarian"]})
    broth = {
        "dataset_id": "odunola/foodie",
        "source_id": "broth-9",
        "title": "Simple Vegetable Broth",
        "quantities": [],
        "adaptations": [],
    }
    lentil = _opt(source_id="lentil-2", title="Red Lentil Soup", quantities=[])
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c0", "search_recipes", {"query": "vegetarian soup"}),
                    (
                        "c1",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                    ),
                    (
                        "c2",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "broth-9"},
                    ),
                ],
            ),
            (
                "parsed",
                _finish_options([broth, lentil], constraints_honored=["dietary_constraints"]),
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert result.final is not None
    assert len(result.final["options"]) == 2
    assert result.final.get("dropped_options") == []
    assert result.final.get("note_source") == "model"
    assert result.final.get("note_claims") == "unverified"
    assert result.final.get("constraint_check") == [
        {"index": 0, "source_id": "broth-9", "status": "checked", "value": "vegetarian"},
        {"index": 1, "source_id": "lentil-2", "status": "checked", "value": "vegetarian"},
    ]


def test_unknown_dietary_value_not_checked(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store, constraints={"dietary_constraints": ["gluten-free"]})
    provider = ScriptedProvider(
        [
            _retrieval_turn(),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(), constraints_honored=["dietary_constraints"]
                ),
            ),
        ]
    )
    result = _run(run_agent(state.id, deps=_deps(store, provider)))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert result.final is not None
    assert len(result.final["options"]) == 2
    assert result.final.get("constraint_check") == [
        {"index": 0, "source_id": "curry-1", "status": "not_checked", "value": "gluten-free"},
        {"index": 1, "source_id": "lentil-2", "status": "not_checked", "value": "gluten-free"},
    ]


# --- P3-L-08 minimum claim grounding ------------------------------------------


def test_note_unsupported_pairing_rejected_then_returned_passes(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    core = _VocabEpicureCore({"lemon", "rosemary", "pork", "garlic", "chicken", "beef"})
    turns: list[tuple[str, Any]] = [
        (
            "tools",
            [
                ("c1", "search_recipes", {"query": "roast chicken"}),
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
        ),
        (
            "parsed",
            _finish_options(
                _two_opts_no_quantities(),
                epicure_lines=[{"ingredient": "pork", "decision": "used", "reason": "roast match"}],
                note="classic companions lemon, rosemary",
            ),
        ),
        (
            "parsed",
            _finish_options(
                _two_opts_no_quantities(),
                epicure_lines=[{"ingredient": "pork", "decision": "used", "reason": "roast match"}],
                note="Two options with pork",
            ),
        ),
    ]
    provider = ScriptedProvider(turns)
    deps = _deps(store, provider, settings=_settings(epicure_enabled=True), epicure_core=core)
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert len(rejects) == 1
    errors = rejects[0].payload["errors"]
    assert any("unsupported pairing" in e and "lemon" in e for e in errors)
    assert any("unsupported pairing" in e and "rosemary" in e for e in errors)
    assert result.final is not None
    assert result.final.get("note") == "Two options with pork"
    assert result.final.get("note_source") == "model"
    assert result.final.get("note_claims") == "verified"


def test_note_dish_name_from_request_and_titles_supported(engine) -> None:
    # Phase 7 live fix (plan-safety): "curry" was flagged although the
    # user asked for it and the option titles named it. Request text
    # and selected option titles are support sources now.
    store = PostgresSessionStore(engine)
    state = _session(store)
    core = _VocabEpicureCore({"curry", "saffron", "pork", "chicken"})
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "creamy chicken curry"}),
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
            ),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(),
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "roast match"}
                    ],
                    note="creamy chicken curry for tonight",
                ),
            ),
        ]
    )
    deps = _deps(
        store,
        provider,
        settings=_settings(epicure_enabled=True),
        epicure_core=core,
        request_text="Give me the creamy chicken curry recipe.",
    )
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert rejects == []
    assert result.final is not None
    assert result.final.get("note_claims") == "verified"


def test_note_genuinely_unsupported_pairing_still_rejected(engine) -> None:
    # Same support sources, but "saffron" and "curry powder" appear in
    # neither pairings, ingredients, request nor titles: still rejected.
    # Phrase matching stays narrow: "curry" in the request does not
    # cover "curry powder".
    store = PostgresSessionStore(engine)
    state = _session(store)
    core = _VocabEpicureCore({"curry", "curry powder", "saffron", "pork", "chicken"})
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "creamy chicken curry"}),
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
            ),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(),
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "roast match"}
                    ],
                    note="creamy chicken curry with saffron and curry powder",
                ),
            ),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(),
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "roast match"}
                    ],
                    note="creamy chicken curry with pork",
                ),
            ),
        ]
    )
    deps = _deps(
        store,
        provider,
        settings=_settings(epicure_enabled=True),
        epicure_core=core,
        request_text="Give me the creamy chicken curry recipe.",
    )
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert len(rejects) == 1
    errors = rejects[0].payload["errors"]
    assert any("unsupported pairing" in e and "saffron" in e for e in errors)
    assert any("unsupported pairing" in e and "curry powder" in e for e in errors)
    assert not any("'curry'" in e for e in errors)


def _saffron_title_run(engine, *, stored_request: str | None) -> tuple[Any, list[Any]]:
    from culinary_copilot.agent.loop import record_user_message

    store = PostgresSessionStore(engine)
    state = _session(store)
    if stored_request is not None:
        record_user_message(store, state.id, text=stored_request)
    core = _VocabEpicureCore({"curry", "saffron", "pork", "chicken"})
    titled = [
        _opt(title="Creamy Chicken Curry with Saffron", quantities=[]),
        _opt(source_id="lentil-2", title="Red Lentil Soup", quantities=[]),
    ]
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "creamy chicken curry"}),
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
            ),
            (
                "parsed",
                _finish_options(
                    titled,
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "roast match"}
                    ],
                    note="creamy chicken curry with saffron",
                ),
            ),
            (
                "parsed",
                _finish_options(
                    _two_opts_no_quantities(),
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "roast match"}
                    ],
                    note="creamy chicken curry for tonight",
                ),
            ),
        ]
    )
    deps = _deps(store, provider, settings=_settings(epicure_enabled=True), epicure_core=core)
    result = _run(run_agent(state.id, deps=deps))
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    return result, rejects


def test_note_pairing_not_supported_by_model_written_title(engine) -> None:
    # Option titles are model-supplied and unvalidated: a title naming
    # "saffron" must not vouch for a saffron pairing. Only the resolved
    # source document's title counts.
    result, rejects = _saffron_title_run(engine, stored_request="creamy chicken curry")
    assert result.stop_reason == "agent_sufficient_evidence"
    assert len(rejects) == 1
    errors = rejects[0].payload["errors"]
    assert any("unsupported pairing" in e and "saffron" in e for e in errors)
    assert not any("'curry'" in e for e in errors)


def test_note_dish_name_supported_by_stored_user_message(engine) -> None:
    # API path: no explicit request_text; the latest stored user
    # message (same text the pairing-cue guard reads) supports "curry".
    # The model-written saffron title still supports nothing.
    result, rejects = _saffron_title_run(engine, stored_request="I'd like a curry tonight")
    assert result.stop_reason == "agent_sufficient_evidence"
    errors = [e for r in rejects for e in r.payload["errors"]]
    assert not any("'curry'" in e for e in errors)
    assert any("saffron" in e for e in errors)


def test_technique_time_claim_needs_cited_chunk(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            ("tools", [("c1", "search_techniques", {"query": "how to boil an egg"})]),
            (
                "parsed",
                _tech_answer(
                    "Boil 10-12 minutes.",
                    [{"doc_id": "tech-egg-1", "chunk_id": 0}],
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
            (
                "parsed",
                _tech_answer(
                    "Simmer eggs 6 minutes.",
                    [{"doc_id": "tech-egg-1", "chunk_id": 0}],
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
        ]
    )
    deps = _deps(
        store,
        provider,
        settings=_settings(epicure_enabled=True),
        overrides={"search_techniques": _tech_search([_tech_row()])},
        request_text="How do I boil an egg?",
    )
    deps.technique_resolver = lambda doc_id, chunk_id: dict(_tech_row(doc_id, chunk_id))
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert len(rejects) == 1
    assert any("unsupported time/temperature claim" in e for e in rejects[0].payload["errors"])
    assert result.final is not None
    assert result.final.get("note_claims") == "verified"


# --- P3-L-09 minimum plan evidence ---------------------------------------------


def _curry_plan(**overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "source": {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
        "mise_en_place": ["chop chicken"],
        "steps": ["cook curry", "serve"],
        "plating": "in a bowl",
    }
    plan.update(overrides)
    return plan


def _select_curry(store: PostgresSessionStore, state_id: str) -> None:
    from culinary_copilot.agent.loop import record_select, run_agent

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
            (
                "parsed",
                _finish_options(
                    [_opt()],
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "crisp contrast"}
                    ],
                ),
            ),
        ]
    )
    _run(
        run_agent(
            state_id,
            deps=_deps(store, provider, settings=_settings(epicure_enabled=True)),
        )
    )
    mid = store.get(state_id)
    assert mid is not None
    record_select(
        store,
        state_id,
        expected_revision=mid.revision,
        dataset_id="odunola/foodie",
        source_id="curry-1",
    )


def test_raw_chicken_plan_rejected_without_safety_ref(engine) -> None:
    store = PostgresSessionStore(engine)
    state = _session(store)
    _select_curry(store, state.id)
    bad = {
        "decision": "finish",
        "move_to": "plan",
        "result": {"plan": _curry_plan()},
        "constraints_honored": [],
        "note": "plan from selected source",
    }
    provider = ScriptedProvider([("parsed", bad), ("parsed", bad)])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_validation_failed"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert len(rejects) == 2
    assert any("safe internal temperatures" in e for e in rejects[-1].payload["errors"])


def test_ingredient_only_plan_needs_admission_then_accepted(engine) -> None:
    from culinary_copilot.agent.loop import record_select

    store = PostgresSessionStore(engine)
    state = _session(store)
    broth = {
        "dataset_id": "odunola/foodie",
        "source_id": "broth-9",
        "title": "Simple Vegetable Broth",
        "quantities": [],
        "adaptations": [],
    }
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "broth"}),
                    (
                        "c2",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "broth-9"},
                    ),
                    ("c3", "find_balanced_pairings", {"ingredient": "onion"}),
                ],
            ),
            (
                "parsed",
                _finish_options(
                    [broth],
                    epicure_lines=[
                        {"ingredient": "pork", "decision": "used", "reason": "broth match"}
                    ],
                ),
            ),
        ]
    )
    _run(run_agent(state.id, deps=_deps(store, provider, settings=_settings(epicure_enabled=True))))
    mid = store.get(state.id)
    assert mid is not None
    record_select(
        store,
        state.id,
        expected_revision=mid.revision,
        dataset_id="odunola/foodie",
        source_id="broth-9",
    )

    def _broth_plan(**overrides: Any) -> dict[str, Any]:
        plan: dict[str, Any] = {
            "source": {"dataset_id": "odunola/foodie", "source_id": "broth-9"},
            "mise_en_place": ["open cubes"],
            "steps": ["simmer broth", "serve"],
            "plating": "in bowls",
        }
        plan.update(overrides)
        return plan

    def _parsed_plan(plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "decision": "finish",
            "move_to": "plan",
            "result": {"plan": plan},
            "constraints_honored": [],
            "note": "plan from broth",
        }

    provider2 = ScriptedProvider(
        [
            ("parsed", _parsed_plan(_broth_plan())),
            (
                "parsed",
                _parsed_plan(
                    _broth_plan(
                        adaptations=[
                            {
                                "description": "Steps are model-created: not from the source.",
                                "label": "adaptation",
                            }
                        ]
                    )
                ),
            ),
        ]
    )
    result2 = _run(run_agent(state.id, deps=_deps(store, provider2)))
    assert result2.stop_reason == "agent_sufficient_evidence"
    assert result2.final is not None
    assert result2.final["plan"]["steps_source"] == "model_adaptation"
    assert result2.final.get("note_source") == "model"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert len(rejects) == 1
    assert any("model_adaptation" in e for e in rejects[0].payload["errors"])


# --- P3-L-10 truncation controls ------------------------------------------------


def test_tool_output_truncation_stays_valid_json() -> None:
    from culinary_copilot.agent.loop import truncate_tool_output

    huge = {
        "tool": "search_recipes",
        "ok": True,
        "results": [{"title": "t" * 200, "id": i} for i in range(200)],
    }
    truncated = truncate_tool_output(huge)
    import json as _json

    text = _json.dumps(truncated)
    assert len(text) <= 4000
    assert _json.loads(text) == truncated
    assert truncated["truncated"] is True
    assert truncated["dropped_items"] > 0

    small = {"tool": "search_recipes", "ok": True, "results": []}
    assert truncate_tool_output(small) == small


def test_overlong_note_rejected_by_schema_not_cut(engine) -> None:
    from pydantic import ValidationError

    from culinary_copilot.agent.loop import AgentDirective

    base: dict[str, Any] = {
        "decision": "finish",
        "move_to": "recommend",
        "result": {"options": []},
        "note": "x" * 600,
    }
    AgentDirective.model_validate(base)
    with pytest.raises(ValidationError):
        AgentDirective.model_validate({**base, "note": "x" * 601})

    store = PostgresSessionStore(engine)
    state = _session(store)
    bad = _finish_options(_two_opts_no_quantities(), note="y" * 700)
    provider = ScriptedProvider([("parsed", bad), ("parsed", bad)])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(run_agent(state.id, deps=_deps(store, provider)))
    assert excinfo.value.reason == "agent_validation_failed"
    events = store.list_events(state.id)
    assert any(e.event_type == "agent_validation_reject" for e in events)
    assert not any(e.event_type == "agent_finished" for e in events)
    rejects = [e for e in events if e.event_type == "agent_validation_reject"]
    assert any("directive malformed" in e for e in rejects[0].payload["errors"])


# --- Canonical time/temperature claims (review fix) ------------------------------


def test_canonical_claim_forms() -> None:
    from culinary_copilot.agent.loop import _parse_time_temp_claim as parse

    assert parse("165°F") == ((165,), "fahrenheit")
    assert parse("165°F") == parse("165 °F")
    assert parse("10 to 12 minutes") == parse("10-12 minutes") == ((10, 12), "minute")
    assert parse("9 mins") == parse("9 minutes") == ((9,), "minute")
    assert parse("74°C") == parse("74 °C") == ((74,), "celsius")
    assert parse("2 hours") == ((2,), "hour")
    assert parse("165 degrees") == ((165,), "degree")
    assert parse("165 degrees F") == ((165,), "fahrenheit")
    assert parse("165 fahrenheit") == ((165,), "fahrenheit")


def test_canonical_claim_support_rules() -> None:
    from culinary_copilot.agent.loop import (
        _evidence_time_temp_claims as evidence,
    )
    from culinary_copilot.agent.loop import (
        _parse_time_temp_claim as parse,
    )
    from culinary_copilot.agent.loop import (
        _time_temp_claim_supported as supported,
    )

    chart = evidence("165 °F (74 °C)")
    assert supported(parse("165°F"), chart)
    assert supported(parse("74°C"), chart)
    assert supported(parse("165 degrees"), chart)  # bare degree matches either class
    assert not supported(parse("160°F"), chart)

    singles = evidence("4 minutes, 7 minutes and 9 minutes")
    assert not supported(parse("10-12 minutes"), singles)  # range needs the range
    assert supported(parse("10-12 minutes"), evidence("simmer 10-12 minutes"))
    assert supported(parse("7 minutes"), evidence("simmer 7-9 minutes"))  # range end
    assert not supported(parse("8 minutes"), evidence("simmer 7-9 minutes"))


def test_spaced_degree_answer_passes_against_tight_chunk(engine) -> None:
    """ "Cook poultry to 165 °F." is supported by chunk text with "165°F"."""
    store = PostgresSessionStore(engine)
    state = _session(store)
    provider = ScriptedProvider(
        [
            ("tools", [("c1", "search_techniques", {"query": "safe chicken temperature"})]),
            (
                "parsed",
                _tech_answer(
                    "Cook poultry to 165 °F.",
                    [{"doc_id": "tech-fda-safe-32", "chunk_id": 0}],
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
        ]
    )
    deps = _deps(
        store,
        provider,
        settings=_settings(epicure_enabled=True),
        overrides={"search_techniques": _tech_search([_safety_row()])},
        request_text="What temperature is safe for chicken?",
    )
    deps.technique_resolver = lambda doc_id, chunk_id: (
        dict(_safety_row()) if (doc_id, chunk_id) == ("tech-fda-safe-32", 0) else None
    )
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert result.final is not None
    assert result.final.get("note_claims") == "verified"


# --- unnamed allergy restriction (P3-L-13) ---------------------------------------


def _ask_allergy() -> dict[str, Any]:
    return {
        "decision": "ask_user",
        "question": {
            "question_id": "q-allergy",
            "question_text": "What is your friend allergic to?",
            "options": ["peanuts", "dairy", "other"],
        },
        "note": "need the named allergen before recommending",
    }


def _two_safe_opts() -> list[dict[str, Any]]:
    return [
        _opt(),
        _opt(
            source_id="lentil-2",
            title="Red Lentil Soup",
            quantities=[{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
        ),
    ]


def test_unnamed_allergy_blocks_finish_until_named_answer(engine) -> None:
    from culinary_copilot.agent.loop import record_user_message

    store = PostgresSessionStore(engine)
    state = _session(store)
    record_user_message(
        store,
        state.id,
        text=(
            "I'm cooking dinner for a friend who has a food allergy. "
            "Suggest something with chicken."
        ),
    )
    finish = _finish_options(_two_safe_opts())
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "chicken"}),
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
            ("parsed", finish),
            ("parsed", _ask_allergy()),
        ]
    )
    result = _run(
        run_agent(state.id, deps=_deps(store, provider, settings=_settings(epicure_enabled=False)))
    )
    assert result.stop_reason == "agent_needs_user_input"
    rejects = [e for e in store.list_events(state.id) if e.event_type == "agent_validation_reject"]
    assert any(
        "unnamed allergy or restriction" in " ".join(str(e) for e in r.payload.get("errors", []))
        for r in rejects
    ), "options finish rejected with the ask-first feedback"

    mid = store.get(state.id)
    assert mid is not None
    answered = record_answer(
        store,
        state.id,
        expected_revision=mid.revision,
        question_id="q-allergy",
        answer="She is allergic to peanuts.",
    )
    assert answered is not None
    provider2 = ScriptedProvider([("parsed", finish)])
    result2 = _run(
        run_agent(state.id, deps=_deps(store, provider2, settings=_settings(epicure_enabled=False)))
    )
    assert result2.stop_reason == "agent_sufficient_evidence"
    assert result2.final is not None
    checks = result2.final.get("constraint_check", [])
    assert [
        c
        for c in checks
        if c.get("value") == "peanut" and c.get("status") == "no_listed_terms_found"
    ]


def test_named_allergy_request_finishes_without_asking(engine) -> None:
    from culinary_copilot.agent.loop import record_user_message

    store = PostgresSessionStore(engine)
    state = _session(store)
    record_user_message(
        store, state.id, text="My friend has a peanut allergy. Suggest something with chicken."
    )
    finish = _finish_options(_two_safe_opts())
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "chicken"}),
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
            ("parsed", finish),
        ]
    )
    result = _run(
        run_agent(state.id, deps=_deps(store, provider, settings=_settings(epicure_enabled=False)))
    )
    assert result.stop_reason == "agent_sufficient_evidence"


# --- token-growth final turn (P3-L-13 run 1) --------------------------------------


def test_token_growth_final_turn_rule() -> None:
    from culinary_copilot.agent.loop import token_growth_final_turn

    # Run-1 shape: after turn 6 the remainder cannot cover two more
    # turns of the current size -> final.
    assert (
        token_growth_final_turn(
            in_ceiling=30000,
            used_in=18363,
            est_in=6000,
            out_ceiling=12000,
            used_out=200,
            turn_cap=6500,
            last_turn_in=4790,
            last_turn_out=35,
        )
        is True
    )
    # Earlier: plenty of room -> not final.
    assert (
        token_growth_final_turn(
            in_ceiling=30000,
            used_in=13573,
            est_in=5500,
            out_ceiling=12000,
            used_out=150,
            turn_cap=6500,
            last_turn_in=4025,
            last_turn_out=35,
        )
        is False
    )
    # First turn: no current size yet -> never final on growth.
    assert (
        token_growth_final_turn(
            in_ceiling=30000,
            used_in=0,
            est_in=12000,
            out_ceiling=12000,
            used_out=0,
            turn_cap=6500,
            last_turn_in=None,
            last_turn_out=None,
        )
        is False
    )
    # Output near exhaustion with a tiny typical turn -> final.
    assert (
        token_growth_final_turn(
            in_ceiling=30000,
            used_in=5000,
            est_in=2000,
            out_ceiling=12000,
            used_out=11900,
            turn_cap=100,
            last_turn_in=2000,
            last_turn_out=30,
        )
        is True
    )


def test_growing_turns_reach_toolless_final_before_budget_stop(engine) -> None:
    from culinary_copilot.llm.client import NativeTurnResult

    store = PostgresSessionStore(engine)
    state = _session(store)
    sizes = iter([3000, 3500, 4000, 4500, 5000, 5000, 5000, 5000])
    tool_turns = iter(
        [
            # All finish evidence in turn 1, so a growth flip on any
            # later turn still validates.
            [
                ("c1", "search_recipes", {"query": "chicken"}),
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
            [("c5", "search_recipes", {"query": "chicken curry"})],
            [("c6", "search_recipes", {"query": "lentil soup"})],
            [("c7", "search_recipes", {"query": "roast chicken"})],
            [("c8", "search_recipes", {"query": "grilled chicken"})],
        ]
    )

    class _GrowingProvider:
        def __init__(self) -> None:
            self.seen_tools: list[list[str]] = []

        async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
            tools = kwargs.get("tools") or []
            self.seen_tools.append([t["name"] for t in tools])
            size = next(sizes)
            if tools:
                batch = next(tool_turns)
                calls = [
                    NativeToolCall(call_id=cid, name=name, arguments=json.dumps(args))
                    for cid, name, args in batch
                ]
                chain = [
                    {
                        "type": "function_call",
                        "call_id": cid,
                        "name": name,
                        "arguments": json.dumps(args),
                    }
                    for cid, name, args in batch
                ]
                return NativeTurnResult(
                    tool_calls=calls,
                    parsed=None,
                    chain_items=chain,
                    input_tokens=size,
                    output_tokens=30,
                )
            return NativeTurnResult(
                tool_calls=[],
                parsed=_finish_options(_two_safe_opts()),
                chain_items=[],
                input_tokens=size,
                output_tokens=40,
            )

    provider = _GrowingProvider()
    # Pinned explicitly: the scripted turn sizes are calibrated to a 30k
    # input ceiling (the default was raised to 60k on 2026-10-04).
    deps = _deps(store, provider, settings=_settings(agent_input_token_ceiling=30000))
    assert deps.settings.agent_input_token_ceiling == 30000
    assert deps.settings.agent_output_token_ceiling == 12000
    result = _run(run_agent(state.id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    assert any(tools == [] for tools in provider.seen_tools), (
        "a tool-less final turn ran before the input budget stopped the session"
    )
    assert len(provider.seen_tools) <= 8
    # Budgets themselves are unchanged by the run.
    assert deps.settings.agent_input_token_ceiling == 30000
    assert deps.settings.agent_output_token_ceiling == 12000


# --- allergen-free claim rule (P3-L-13 review) -------------------------------------


def _answered_peanut_session(engine: Any) -> Any:
    """Session with tools done, allergy asked and peanuts answered."""
    from culinary_copilot.agent.loop import record_user_message

    store = PostgresSessionStore(engine)
    state = _session(store)
    record_user_message(
        store,
        state.id,
        text=(
            "I'm cooking dinner for a friend who has a food allergy. "
            "Suggest something with chicken."
        ),
    )
    provider = ScriptedProvider(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "chicken"}),
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
            ("parsed", _ask_allergy()),
        ]
    )
    result = _run(
        run_agent(state.id, deps=_deps(store, provider, settings=_settings(epicure_enabled=False)))
    )
    assert result.stop_reason == "agent_needs_user_input"
    mid = store.get(state.id)
    assert mid is not None
    answered = record_answer(
        store,
        state.id,
        expected_revision=mid.revision,
        question_id="q-allergy",
        answer="She is allergic to peanuts.",
    )
    assert answered is not None
    return store, state


def _reject_texts(store: Any, session_id: str) -> list[str]:
    return [
        " ".join(str(e) for e in r.payload.get("errors", []))
        for r in store.list_events(session_id)
        if r.event_type == "agent_validation_reject"
    ]


def test_allergen_free_note_rejected(engine) -> None:
    store, state = _answered_peanut_session(engine)
    bad = _finish_options(_two_safe_opts(), note="These options are peanut-free.")
    provider = ScriptedProvider([("parsed", bad), ("parsed", bad)])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(
            run_agent(
                state.id, deps=_deps(store, provider, settings=_settings(epicure_enabled=False))
            )
        )
    assert excinfo.value.reason == "agent_validation_failed"
    assert any("allergen-free" in text for text in _reject_texts(store, state.id)), (
        "peanut-free note rejected with the claim feedback"
    )


def test_safe_for_allergy_note_rejected(engine) -> None:
    store, state = _answered_peanut_session(engine)
    bad = _finish_options(_two_safe_opts(), note="Safe for her allergy, enjoy.")
    provider = ScriptedProvider([("parsed", bad), ("parsed", bad)])
    with pytest.raises(AgentLoopError) as excinfo:
        _run(
            run_agent(
                state.id, deps=_deps(store, provider, settings=_settings(epicure_enabled=False))
            )
        )
    assert excinfo.value.reason == "agent_validation_failed"
    assert any("allergen-free" in text for text in _reject_texts(store, state.id)), (
        "safe-for-allergy note rejected with the claim feedback"
    )


def test_evidence_digest_lists_web_searches(engine) -> None:
    """Step-2 diagnosis: the model could not see its own web searches
    and searched again instead of answering."""
    from culinary_copilot.agent.loop import session_evidence_digest

    store = PostgresSessionStore(engine)
    state = _session(store)
    store.append_event(
        state.id,
        "tool_call",
        {
            "call_id": "call_web1",
            "tool": "search_web",
            "outcome": "ok",
            "args": '{"query": "okonomiyaki recipe"}',
            "result_facts": {
                "source_count": 3,
                "classifications": ["unclassified", "unclassified", "unclassified"],
                "titles": ["Gastronomy", "Washoku", "Recipes"],
                "hosts": ["osaka-info.jp", "maff.go.jp", "otafukusauce.com"],
            },
        },
    )
    digest = session_evidence_digest(store, state.id)
    assert "web query='okonomiyaki recipe' sources=3" in digest
    assert "titles=[Gastronomy, Washoku, Recipes]" in digest
    assert "hosts=[osaka-info.jp, maff.go.jp, otafukusauce.com]" in digest
