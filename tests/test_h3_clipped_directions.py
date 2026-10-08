"""H3 part 1: access to clipped directions (offline, disposable DB where needed).

2026-10-08: get_recipe shows up to 12 directions clipped at 600 chars.
A ranged call (directions_from/to, 0-based, to exclusive, max 12 per
call) returns its slice full so the model can read omitted text. A plan
for a recipe with omitted directions needs every omitted index read in
this run, or an adaptation naming each unread index plus
model_adaptation.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.agent.validate import (
    omitted_direction_indices,
    unread_directions_listed,
    validate_omitted_directions,
)
from culinary_copilot.config import Settings
from culinary_copilot.domain.sessions import SessionState
from culinary_copilot.recipes import import_data
from culinary_copilot.services.session_store import PostgresSessionStore

TEST_DB = "culinary_test_h3"


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
        pytest.skip(f"PostgreSQL unavailable for H3 tests: {exc!r}")
    finally:
        try:
            maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
            with maint.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            maint.dispose()
        except Exception:
            pass


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _long_direction() -> str:
    # >600 chars of real words so attribution word-overlap can pass.
    return ("Fold the dough gently and rest. " * 25).strip()


def _doc_700() -> dict[str, Any]:
    return {
        "dataset_id": "odunola/foodie",
        "source_id": "h3-700",
        "title": "H3 Long Direction",
        "servings": 2.0,
        "ingredients": [{"canonical": "flour", "amount": None, "unit": None}],
        "instructions": ["Chop the onion.", _long_direction(), "Serve hot."],
    }


def _doc_14() -> dict[str, Any]:
    return {
        "dataset_id": "odunola/foodie",
        "source_id": "h3-14",
        "title": "H3 Fourteen Steps",
        "servings": 2.0,
        "ingredients": [{"canonical": "rice", "amount": None, "unit": None}],
        "instructions": [f"Do step {i} with rice." for i in range(14)],
    }


def _doc_3() -> dict[str, Any]:
    return {
        "dataset_id": "odunola/foodie",
        "source_id": "h3-3",
        "title": "H3 Short",
        "servings": 2.0,
        "ingredients": [{"canonical": "rice", "amount": None, "unit": None}],
        "instructions": ["Rinse the rice.", "Boil the rice.", "Serve hot."],
    }


def _plan_for(
    doc: dict[str, Any], steps: list[str], sources: list[int], adaptations: Any = None
) -> dict[str, Any]:
    return {
        "source": {"dataset_id": doc["dataset_id"], "source_id": doc["source_id"]},
        "mise_en_place": ["prep rice"],
        "steps": steps,
        "step_sources": sources,
        "plating": "in a bowl",
        "adaptations": adaptations if adaptations is not None else [],
    }


def test_h3_700char_direction_blocked_until_read_then_passes() -> None:
    doc = _doc_700()
    assert omitted_direction_indices(doc) == [1]
    steps = ["Chop the onion", "Fold the dough gently", "Serve hot"]
    plan = _plan_for(doc, steps, [0, 1, 2])
    # Nothing read yet: blocked, names the direction and the call.
    errors = validate_omitted_directions(plan, doc, set(), "source")
    assert len(errors) == 1
    assert "[1]" in errors[0] and "directions_from=1" in errors[0]
    assert "directions_to=2" in errors[0]
    # Standard fetch covers short 0 and 2, but not the clipped tail.
    errors = validate_omitted_directions(plan, doc, {0, 2}, "source")
    assert any("[1]" in e for e in errors)
    # Ranged read of 1-2 covers the tail: passes.
    assert validate_omitted_directions(plan, doc, {0, 1, 2}, "source") == []


def test_h3_14_directions_blocked_until_read_then_passes() -> None:
    doc = _doc_14()
    assert omitted_direction_indices(doc) == [12, 13]
    steps = [f"Do step {i} with rice" for i in range(14)]
    plan = _plan_for(doc, steps, list(range(14)))
    errors = validate_omitted_directions(plan, doc, set(range(12)), "source")
    assert len(errors) == 1
    assert "[12, 13]" in errors[0]
    assert "directions_from=12" in errors[0] and "directions_to=14" in errors[0]
    assert validate_omitted_directions(plan, doc, set(range(14)), "source") == []


def test_h3_no_clipping_needs_no_extra_call() -> None:
    doc = _doc_3()
    assert omitted_direction_indices(doc) == []
    plan = _plan_for(doc, ["Rinse the rice", "Boil the rice", "Serve hot"], [0, 1, 2])
    # No omitted: passes with nothing read and without adaptation.
    assert validate_omitted_directions(plan, doc, set(), "source") == []
    assert validate_omitted_directions(plan, doc, set(), "model_adaptation") == []


def test_h3_range_after_full_is_not_repeat_same_range_is() -> None:
    from culinary_copilot.tools.registry import args_digest

    full = {"dataset_id": "odunola/foodie", "source_id": "h3-14"}
    first_range = {
        "dataset_id": "odunola/foodie",
        "source_id": "h3-14",
        "directions_from": 12,
        "directions_to": 14,
    }
    same_range = dict(first_range)
    other_range = {
        "dataset_id": "odunola/foodie",
        "source_id": "h3-14",
        "directions_from": 0,
        "directions_to": 2,
    }
    # A ranged call is new evidence, not a repeat of the full fetch;
    # the same range twice shares a digest (repeat), a different range
    # does not.
    assert args_digest(full) != args_digest(first_range)
    assert args_digest(first_range) == args_digest(same_range)
    assert args_digest(first_range) != args_digest(other_range)


def test_h3_range_never_returns_pointer_but_same_range_counts_repeat(engine) -> None:
    import json as _json

    from culinary_copilot.tools import all_tool_definitions, all_tool_impls, run_tool
    from culinary_copilot.tools.registry import ToolContext

    doc = {
        "title": "H3 Range Pointer",
        "provenance": {"dataset_id": "odunola/foodie", "source_id": "h3-range"},
        "ingredients": [{"canonical": "rice"}],
        "instructions": [f"Do step {i}." for i in range(14)],
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
                "id": "test-h3-1",
                "dataset_id": "odunola/foodie",
                "revision": "test-rev",
                "checksum": "test-h3",
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
                "s": "h3-range",
                "i": "test-h3-1",
                "t": "H3 Range Pointer",
                "n": ["rice"],
                "doc": _json.dumps(doc),
                "st": "rice steps",
            },
        )
    settings = Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    session_id = f"ses-{uuid.uuid4().hex[:10]}"
    store.create(SessionState(id=session_id))
    defs = {d.name: d for d in all_tool_definitions(timeout_s=10.0)}
    impls = all_tool_impls()
    ctx = ToolContext(settings=settings, engine=engine, session_store=store)
    ctx.bound_session_id = session_id
    pair = {"dataset_id": "odunola/foodie", "source_id": "h3-range"}
    first = _run(
        run_tool(
            defs["get_recipe"],
            impls["get_recipe"],
            dict(pair),
            ctx,
            session_id=session_id,
            call_id="c1",
        )
    )
    assert first["ok"] is True and "recipe" in first
    ctx.visible_full_recipes = {("odunola/foodie", "h3-range")}
    # Standard repeat while visible: short pointer.
    second = _run(
        run_tool(
            defs["get_recipe"],
            impls["get_recipe"],
            dict(pair),
            ctx,
            session_id=session_id,
            call_id="c2",
        )
    )
    assert second.get("duplicate_of_session_evidence") is True
    # Ranged call after the full fetch: full slice, never the pointer.
    ranged = _run(
        run_tool(
            defs["get_recipe"],
            impls["get_recipe"],
            {**pair, "directions_from": 12, "directions_to": 14},
            ctx,
            session_id=session_id,
            call_id="c3",
        )
    )
    assert ranged["ok"] is True
    assert ranged.get("duplicate_of_session_evidence") is not True
    assert ranged.get("directions_from") == 12 and ranged.get("directions_to") == 14
    # Oversized window rejected without raising global limits.
    too_wide = _run(
        run_tool(
            defs["get_recipe"],
            impls["get_recipe"],
            {**pair, "directions_from": 0, "directions_to": 13},
            ctx,
            session_id=session_id,
            call_id="c4",
        )
    )
    assert too_wide["ok"] is False
    assert "max 12" in too_wide.get("message", "")


def test_h3_unread_route_passes_only_with_adaptation_label() -> None:
    doc = _doc_14()
    # 12 steps citing 0-11: model_adaptation (12, 13 uncovered).
    steps12 = [f"Do step {i} with rice" for i in range(12)]
    listed = _plan_for(
        doc,
        steps12,
        list(range(12)),
        adaptations=[
            {
                "description": "directions 12, 13 not read; steps cover 0-11 only",
                "label": "adaptation",
            }
        ],
    )
    assert validate_omitted_directions(listed, doc, set(range(12)), "model_adaptation") == []
    assert unread_directions_listed(listed, [12, 13]) is True
    # Same plan without the listing: rejected.
    unlisted = _plan_for(doc, steps12, list(range(12)), adaptations=[])
    assert unread_directions_listed(unlisted, [12, 13]) is False
    assert validate_omitted_directions(unlisted, doc, set(range(12)), "model_adaptation") != []
    # Listing present but labelled source (all 14 cited): still rejected.
    steps14 = [f"Do step {i} with rice" for i in range(14)]
    claimed_source = _plan_for(
        doc,
        steps14,
        list(range(14)),
        adaptations=[{"description": "directions 12, 13 not read", "label": "adaptation"}],
    )
    assert validate_omitted_directions(claimed_source, doc, set(range(12)), "source") != []


def test_h3_range_summary_stays_within_limit_and_names_omitted() -> None:
    from culinary_copilot.agent.loop import (
        _summarize_result,
        tool_output_limit,
        truncate_tool_output,
    )

    doc = _doc_14()
    summary = _summarize_result(
        "get_recipe",
        {
            "ok": True,
            "recipe": dict(doc),
            "directions_from": 12,
            "directions_to": 14,
            "directions_total": 14,
        },
    )
    recipe = summary["recipe"]
    assert recipe["directions_from"] == 12 and recipe["directions_to"] == 14
    assert recipe["directions"] == ["Do step 12 with rice.", "Do step 13 with rice."]
    assert recipe["directions_clipped"] == []
    assert "still omitted" in summary["message"]
    out = truncate_tool_output(summary, tool_output_limit("get_recipe"))
    assert "truncated" not in out
    # Standard summary still clips and points at the ranged call.
    clipped = _summarize_result("get_recipe", {"ok": True, "recipe": dict(_doc_700())})
    assert clipped["recipe"]["directions_clipped"] == [1]
    assert "directions_from/to" in clipped["message"]


def test_h3_loop_blocks_until_ranged_read_then_passes(engine) -> None:
    # End to end through run_agent: a 14-direction plan is rejected
    # until the 12-14 slice is returned, then accepted; attribution
    # counts the ranged read (steps citing 12, 13 validate).
    import json as _json

    from culinary_copilot.agent.loop import AgentDeps, record_select, run_agent
    from culinary_copilot.config import Settings as _Settings
    from culinary_copilot.tools.registry import ToolContext

    doc14 = _doc_14()

    class _Scripted:
        def __init__(self, turns: list[tuple[str, Any]]) -> None:
            from collections import deque as _dq

            self.turns: Any = _dq(turns)
            self.seen_tools: list[list[str]] = []
            self.seen_inputs: list[list[dict[str, Any]]] = []

        async def complete_native_tool_turn(self, **kw: Any) -> Any:
            from culinary_copilot.llm.client import NativeToolCall, NativeTurnResult

            self.seen_tools.append([t["name"] for t in kw.get("tools", [])])
            self.seen_inputs.append(list(kw.get("input_items", [])))
            kind, payload = self.turns.popleft()
            if kind == "tools":
                calls = [
                    NativeToolCall(call_id=cid, name=name, arguments=_json.dumps(args))
                    for cid, name, args in payload
                ]
                chain = [
                    {
                        "type": "function_call",
                        "call_id": cid,
                        "name": name,
                        "arguments": _json.dumps(args),
                    }
                    for cid, name, args in payload
                ]
                return NativeTurnResult(tool_calls=calls, parsed=None, chain_items=chain)
            return NativeTurnResult(tool_calls=[], parsed=dict(payload), chain_items=[])

    def _range_aware_get(args: Any, context: Any) -> dict[str, Any]:
        # Mirrors production: standard returns the doc, ranged echoes
        # its slice bounds so coverage and summary see new evidence.
        from culinary_copilot.agent.validate import doc_directions as _dirs

        if (args.dataset_id, args.source_id) != ("odunola/foodie", "h3-14"):
            return {
                "ok": False,
                "error_type": "invalid_arguments",
                "reason": "tool_invalid_arguments",
                "message": "not found",
                "next_action": "change_request",
            }
        stored = _dirs(dict(doc14))
        if args.directions_from is not None or args.directions_to is not None:
            req_from = int(args.directions_from) if args.directions_from is not None else 0
            req_to = int(args.directions_to) if args.directions_to is not None else len(stored)
            return {
                "ok": True,
                "recipe": dict(doc14),
                "directions_from": req_from,
                "directions_to": min(req_to, len(stored)),
                "directions_total": len(stored),
            }
        return {"ok": True, "recipe": dict(doc14)}

    def _search(args: Any, context: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode_ran": "fulltext",
            "cost_class": "free",
            "results": [
                {"dataset_id": "odunola/foodie", "source_id": "h3-14", "title": "H3 Fourteen Steps"}
            ],
        }

    settings = _Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    session_id = f"ses-{uuid.uuid4().hex[:10]}"
    store.create(SessionState(id=session_id, steps_remaining=12, tool_calls_remaining=12))
    # Recommend phase: search + full fetch, then options.
    provider = _Scripted(
        [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "rice"}),
                    ("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "h3-14"}),
                ],
            ),
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "recommend",
                    "result": {
                        "options": [
                            {
                                "dataset_id": "odunola/foodie",
                                "source_id": "h3-14",
                                "title": "H3 Fourteen Steps",
                                "quantities": [],
                                "adaptations": [],
                            },
                            {
                                "dataset_id": "odunola/foodie",
                                "source_id": "h3-14",
                                "title": "H3 Fourteen Steps Second",
                                "quantities": [],
                                "adaptations": [],
                            },
                        ]
                    },
                    "constraints_honored": [],
                    "note": "two options",
                    "epicure_lines": [],
                    "epicure_skip_reason": "simple_technique_question",
                },
            ),
        ]
    )
    context = ToolContext(
        settings=settings,
        engine=None,
        session_store=store,
        impl_overrides={"search_recipes": _search, "get_recipe": _range_aware_get},
    )
    deps = AgentDeps(
        settings=settings,
        session_store=store,
        provider=provider,
        tool_context=context,
        recipe_resolver=lambda ds, sid: (
            dict(doc14) if (ds, sid) == ("odunola/foodie", "h3-14") else None
        ),
    )
    result = _run(run_agent(session_id, deps=deps))
    assert result.stop_reason == "agent_sufficient_evidence"
    mid = store.get(session_id)
    assert mid is not None
    selected = record_select(
        store,
        session_id,
        expected_revision=mid.revision,
        dataset_id="odunola/foodie",
        source_id="h3-14",
    )
    assert selected.current_phase == "select"

    full_plan = {
        "source": {"dataset_id": "odunola/foodie", "source_id": "h3-14"},
        "mise_en_place": ["prep rice"],
        "steps": [f"Do step {i} with rice" for i in range(14)],
        "step_sources": list(range(14)),
        "plating": "in a bowl",
    }
    # Plan run: full fetch already in session, but the 12-14 slice was
    # never returned in this run, so the first finish is rejected with
    # the directions and the ranged call named.
    provider2 = _Scripted(
        [
            (
                "tools",
                [("c9", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "h3-14"})],
            ),
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "plan",
                    "result": {"plan": dict(full_plan)},
                    "constraints_honored": [],
                    "note": "plan",
                },
            ),
            (
                "tools",
                [
                    (
                        "c10",
                        "get_recipe",
                        {
                            "dataset_id": "odunola/foodie",
                            "source_id": "h3-14",
                            "directions_from": 12,
                            "directions_to": 14,
                        },
                    )
                ],
            ),
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "plan",
                    "result": {"plan": dict(full_plan)},
                    "constraints_honored": [],
                    "note": "plan",
                },
            ),
        ]
    )
    context2 = ToolContext(
        settings=settings,
        engine=None,
        session_store=store,
        impl_overrides={"search_recipes": _search, "get_recipe": _range_aware_get},
    )
    deps2 = AgentDeps(
        settings=settings,
        session_store=store,
        provider=provider2,
        tool_context=context2,
        recipe_resolver=lambda ds, sid: (
            dict(doc14) if (ds, sid) == ("odunola/foodie", "h3-14") else None
        ),
    )
    result2 = _run(run_agent(session_id, deps=deps2))
    assert result2.stop_reason == "agent_sufficient_evidence"
    assert result2.final is not None and result2.final["plan"]["steps_source"] == "source"
    rejects = [
        e for e in store.list_events(session_id) if e.event_type == "agent_validation_reject"
    ]
    assert any("directions [12, 13]" in str(e.payload.get("errors")) for e in rejects)
    assert any("directions_from=12" in str(e.payload.get("errors")) for e in rejects)
    # The ranged call after the full fetch did not earn the wrap-up:
    # its fingerprint differs, so get_recipe stays offered.
    assert "get_recipe" in provider2.seen_tools[2]
    # 2026-10-08 review: the requirement and the exact call are in the
    # turn input before the first draft, and gone once the slice is read.
    first_turn = str(provider2.seen_inputs[0])
    assert "Plan requirement: directions [12, 13]" in first_turn
    assert "directions_from=12 directions_to=14" in first_turn
    assert "Plan requirement: directions" not in str(provider2.seen_inputs[3])


def test_h3_next_slice_covers_scattered_unread_directions_in_one_call() -> None:
    # 2026-10-08 review: unread [1, 5, 9] is one call, not three.
    from culinary_copilot.agent.validate import next_direction_slice

    assert next_direction_slice([1, 5, 9], 20) == (1, 10)
    assert next_direction_slice([12, 13], 14) == (12, 14)
    assert next_direction_slice([3, 20], 30) == (3, 4)


def test_h3_requirement_line_names_unread_directions_only() -> None:
    from culinary_copilot.agent.loop import omitted_directions_requirement

    line = omitted_directions_requirement(_doc_14(), set(range(12)))
    assert line is not None and "directions [12, 13]" in line
    assert "directions_from=12 directions_to=14" in line
    assert omitted_directions_requirement(_doc_14(), set(range(14))) is None
    assert omitted_directions_requirement(_doc_3(), set()) is None


def test_h3_same_range_twice_counts_as_repeat(engine) -> None:
    # Same range twice shares a fingerprint and digest, so the second
    # range-only step earns the wrap-up (get_recipe withheld next turn);
    # four identical ranges stall out like identical searches do.
    import json as _json

    from culinary_copilot.agent.loop import AgentDeps, AgentLoopError, run_agent
    from culinary_copilot.config import Settings as _Settings
    from culinary_copilot.tools.registry import ToolContext

    doc14 = _doc_14()

    class _Scripted:
        def __init__(self, turns: list[tuple[str, Any]]) -> None:
            from collections import deque as _dq

            self.turns: Any = _dq(turns)
            self.seen_tools: list[list[str]] = []
            self.seen_inputs: list[list[dict[str, Any]]] = []

        async def complete_native_tool_turn(self, **kw: Any) -> Any:
            from culinary_copilot.llm.client import NativeToolCall, NativeTurnResult

            self.seen_tools.append([t["name"] for t in kw.get("tools", [])])
            self.seen_inputs.append(list(kw.get("input_items", [])))
            kind, payload = self.turns.popleft()
            calls = [
                NativeToolCall(call_id=cid, name=name, arguments=_json.dumps(args))
                for cid, name, args in payload
            ]
            chain = [
                {
                    "type": "function_call",
                    "call_id": cid,
                    "name": name,
                    "arguments": _json.dumps(args),
                }
                for cid, name, args in payload
            ]
            return NativeTurnResult(tool_calls=calls, parsed=None, chain_items=chain)

    def _range_get(args: Any, context: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "recipe": dict(doc14),
            "directions_from": int(args.directions_from or 0),
            "directions_to": int(args.directions_to or 14),
            "directions_total": 14,
        }

    def _search(args: Any, context: Any) -> dict[str, Any]:
        return {"ok": True, "mode_ran": "fulltext", "cost_class": "free", "results": []}

    settings = _Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    session_id = f"ses-{uuid.uuid4().hex[:10]}"
    store.create(SessionState(id=session_id, steps_remaining=8, tool_calls_remaining=12))
    pair_range = {
        "dataset_id": "odunola/foodie",
        "source_id": "h3-14",
        "directions_from": 12,
        "directions_to": 14,
    }
    provider = _Scripted(
        [
            ("tools", [("c1", "get_recipe", dict(pair_range))]),
            ("tools", [("c2", "get_recipe", dict(pair_range))]),
            ("tools", [("c3", "search_recipes", {"query": "rice"})]),
            ("tools", [("c4", "get_recipe", dict(pair_range))]),
        ]
    )
    context = ToolContext(
        settings=settings,
        engine=None,
        session_store=store,
        impl_overrides={"search_recipes": _search, "get_recipe": _range_get},
    )
    deps = AgentDeps(
        settings=settings,
        session_store=store,
        provider=provider,
        tool_context=context,
        recipe_resolver=lambda ds, sid: dict(doc14),
    )
    try:
        _run(run_agent(session_id, deps=deps))
    except AgentLoopError as exc:
        assert exc.reason == "agent_no_progress"
    else:  # pragma: no cover - the stall must fire
        raise AssertionError("identical ranges should stall")
    # Second range repeats the first: the third turn is the wrap-up with
    # get_recipe withheld.
    assert "get_recipe" not in provider.seen_tools[2]
    assert "Wrap-up" in str(provider.seen_inputs[2][-1].get("content"))
