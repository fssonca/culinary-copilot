"""Phase 3 human-checkpoint review packet generator (offline, scripted).

Runs 10 scripted trajectories against the bounded agent loop with fake
tools and a scripted provider — no model calls, no network, no paid
calls — on the disposable ``culinary_check_packet`` database, and
renders ``trajectories.json`` plus the readable ``REVIEW.md`` from the
actual run outputs (stop reasons, events, finals).

Usage (from the repo root; writes into this directory)::

    uv run python evals/phase3_agent/generate.py

Nothing here authorizes a live run; see LIVE_PLAN.md (prepared, not run).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

CURRY_DOC = {
    "dataset_id": "odunola/foodie",
    "source_id": "curry-1",
    "title": "Creamy Chicken Curry",
    "servings": 4.0,
    "ingredients": [
        {"canonical": "chicken", "amount": "500", "unit": "g", "quantity_text": "500 g"},
        {"canonical": "yogurt", "amount": "1", "unit": "cup", "quantity_text": "1 cup"},
    ],
}
LENTIL_DOC = {
    "dataset_id": "odunola/foodie",
    "source_id": "lentil-2",
    "title": "Red Lentil Soup",
    "servings": 4.0,
    "ingredients": [
        {
            "canonical": "red lentils",
            "amount": "200",
            "unit": "g",
            "quantity_text": "200 g",
        },
        {"canonical": "onion", "amount": "1", "unit": "count", "quantity_text": "1"},
    ],
}
DOCS = {
    ("odunola/foodie", "curry-1"): CURRY_DOC,
    ("odunola/foodie", "lentil-2"): LENTIL_DOC,
}
ROWS = [
    {"dataset_id": "odunola/foodie", "source_id": "curry-1", "title": "Creamy Chicken Curry"},
    {"dataset_id": "odunola/foodie", "source_id": "lentil-2", "title": "Red Lentil Soup"},
]


def main() -> None:
    from sqlalchemy import create_engine, text

    from culinary_copilot.config import Settings
    from culinary_copilot.recipes import import_data

    settings = Settings()
    base = settings.database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    db_name = "culinary_check_packet"
    maint = create_engine(f"{head}/postgres", isolation_level="AUTOCOMMIT")
    with maint.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{db_name}' AND pid <> pg_backend_pid()"
            )
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    maint.dispose()
    try:
        engine = create_engine(f"{head}/{db_name}")
        with engine.begin() as conn:
            import_data.apply_migrations(conn)
        trajectories = [run_scenario(engine, spec) for spec in SCENARIOS]
        engine.dispose()
    finally:
        maint = create_engine(f"{head}/postgres", isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname='{db_name}' AND pid <> pg_backend_pid()"
                )
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        maint.dispose()
    (HERE / "trajectories.json").write_text(
        json.dumps(trajectories, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (HERE / "REVIEW.md").write_text(render_review(trajectories), encoding="utf-8")
    print(f"wrote {len(trajectories)} trajectories to {HERE}")


# --- scripted provider + fakes (standalone; mirrors the offline tests) ---


class ScriptedProvider:
    def __init__(self, turns: list[tuple[str, Any]]) -> None:
        from culinary_copilot.llm.client import NativeToolCall, NativeTurnResult

        self._ntc = NativeToolCall
        self._ntr = NativeTurnResult
        self.turns: deque[tuple[str, Any]] = deque(turns)
        self.seen_inputs: list[list[dict[str, Any]]] = []
        self.seen_inputs_estimated: list[int] = []

    async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
        from culinary_copilot.agent.loop import AgentDirective, estimate_turn_input

        self.seen_inputs.append(list(kwargs.get("input_items") or []))
        # Full pre-turn estimate: items + offered tool defs + schema.
        self.seen_inputs_estimated.append(
            estimate_turn_input(
                list(kwargs.get("input_items") or []),
                list(kwargs.get("tools") or []),
                response_schema=AgentDirective.model_json_schema(),
            )
        )
        kind, payload = self.turns.popleft()
        if kind == "tools":
            calls = [
                self._ntc(call_id=cid, name=name, arguments=json.dumps(args))
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
            return self._ntr(tool_calls=calls, parsed=None, chain_items=chain)
        if kind == "parsed":
            return self._ntr(tool_calls=[], parsed=dict(payload), chain_items=[])
        raise AssertionError(f"bad script kind {kind!r}")


class FakeCore:
    """Epicure core fake (enabled or disabled per scenario)."""

    def __init__(self, settings: Any, enabled: bool = True) -> None:
        self.settings = settings
        self._enabled = enabled

    def find_balanced_pairings(self, ingredient: str, k: int = 5) -> Any:
        from culinary_copilot.tools.epicure import EpicureDisabledError, Pairing

        if not self._enabled:
            raise EpicureDisabledError("disabled")
        base = (
            [("pork", 0.5), ("beef", 0.4)]
            if ingredient == "chicken"
            else [("coconut milk", 0.45), ("sour cream", 0.4)]
        )
        return [Pairing(ingredient=n, score=s) for n, s in base[:k]]


def make_context(
    store: Any,
    settings: Any,
    *,
    search_rows: Any = None,
    slow: float = 0.0,
    epicure_enabled: bool = True,
) -> Any:
    from culinary_copilot.tools.registry import ToolContext

    rows = ROWS if search_rows is None else search_rows

    def _search(args: Any, context: Any) -> dict[str, Any]:
        if slow:
            time.sleep(slow)
        return {"ok": True, "mode_ran": "fulltext", "cost_class": "free", "results": list(rows)}

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

    core = FakeCore(settings, enabled=epicure_enabled)
    return ToolContext(
        settings=settings,
        engine=None,
        session_store=store,
        impl_overrides={"search_recipes": _search, "get_recipe": _get},
        epicure_core=core,
        epicure_cooc=core,
        epicure_chem=core,
    )


def opt(
    source_id: str,
    quantities: list[dict[str, Any]],
    title: str,
    adaptations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "dataset_id": "odunola/foodie",
        "source_id": source_id,
        "title": title,
        "quantities": quantities,
        "adaptations": adaptations or [],
    }


CHICKEN_Q = [{"ingredient": "chicken", "amount": "500", "unit": "g"}]
LENTIL_Q = [{"ingredient": "red lentils", "amount": "200", "unit": "g"}]


def finish(options: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    directive: dict[str, Any] = {
        "decision": "finish",
        "move_to": "recommend",
        "result": {"options": options},
        "constraints_honored": [],
        "note": "packet finish",
    }
    directive.update(kw)
    return directive


# --- scenarios ---

# Each spec: key, title, request text, session seeds, settings seeds,
# script turns, and follow-up runs (answer/select/plan).
SCENARIOS: list[dict[str, Any]] = [
    {
        "key": "normal-full-flow",
        "title": "Normal full flow (recommend, select, plan)",
        "request": "I want a chicken dinner for tonight, about 30 minutes.",
        "session": {},
        "turns": lambda: [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "chicken dinner"}),
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
                finish(
                    [
                        opt("curry-1", CHICKEN_Q, "Creamy Chicken Curry"),
                        opt("lentil-2", LENTIL_Q, "Red Lentil Soup"),
                    ]
                ),
            ),
        ],
        "select": {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
        "plan_turns": lambda: [
            (
                "tools",
                [
                    (
                        "c4",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                    )
                ],
            ),
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "plan",
                    "result": {
                        "plan": {
                            "source": {
                                "dataset_id": "odunola/foodie",
                                "source_id": "curry-1",
                            },
                            "mise_en_place": ["dice chicken", "measure yogurt"],
                            "steps": ["brown chicken", "stir in yogurt", "serve"],
                            "plating": "over rice in shallow bowls",
                            "quantities": [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
                            "adaptations": [],
                        }
                    },
                    "constraints_honored": [],
                    "note": "packet plan",
                },
            ),
        ],
    },
    {
        "key": "yogurt-ask-resume",
        "title": "Yogurt ask-and-resume",
        "request": "Something with chicken; I might have yogurt.",
        "session": {},
        "turns": lambda: [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "chicken"}),
                    ("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
                ],
            ),
            (
                "parsed",
                {
                    "decision": "ask_user",
                    "question": {
                        "question_id": "q-yogurt",
                        "question_text": "Do you have plain yogurt?",
                        "options": ["yes", "no"],
                    },
                    "note": "curry needs yogurt",
                },
            ),
        ],
        "answer": {"question_id": "q-yogurt", "answer": "no"},
        "resume_turns": lambda: [
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
                finish(
                    [
                        opt(
                            "curry-1",
                            CHICKEN_Q,
                            "Creamy Chicken Curry",
                            [
                                {
                                    "description": "unverified substitution: "
                                    "coconut milk for yogurt (Epicure candidate, "
                                    "no dietary claim)",
                                    "label": "adaptation",
                                }
                            ],
                        ),
                        opt("lentil-2", LENTIL_Q, "Red Lentil Soup"),
                    ]
                ),
            ),
        ],
    },
    {
        "key": "direct-recipe",
        "title": "Direct recipe request (Epicure consulted, one option)",
        "request": "Give me the red lentil soup recipe.",
        "session": {},
        "turns": lambda: [
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
                finish([opt("lentil-2", LENTIL_Q, "Red Lentil Soup")]),
            ),
        ],
        "select": {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
        "plan_turns": lambda: [
            (
                "parsed",
                {
                    "decision": "finish",
                    "move_to": "plan",
                    "result": {
                        "plan": {
                            "source": {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                            "mise_en_place": ["rinse lentils", "dice onion"],
                            "steps": ["simmer 25 minutes", "blend half", "serve"],
                            "plating": "in deep bowls with lemon",
                        }
                    },
                    "constraints_honored": [],
                    "note": "packet plan",
                },
            ),
        ],
    },
    {
        "key": "hard-constraint-conflict",
        "title": "Hard-constraint conflict (rejected, then honored)",
        "request": "Vegetarian dinner, something hearty.",
        "session": {"constraints": {"dietary_constraints": ["vegetarian"]}},
        "turns": lambda: [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "hearty vegetarian"}),
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
                    ("c4", "find_balanced_pairings", {"ingredient": "lentils"}),
                ],
            ),
            (
                "parsed",
                finish(
                    [
                        opt("curry-1", CHICKEN_Q, "Creamy Chicken Curry"),
                        opt("lentil-2", LENTIL_Q, "Red Lentil Soup"),
                    ]
                ),
            ),
            (
                "parsed",
                finish(
                    [opt("lentil-2", LENTIL_Q, "Red Lentil Soup")],
                    constraints_honored=["dietary_constraints"],
                ),
            ),
        ],
    },
    {
        "key": "empty-retrieval",
        "title": "Empty retrieval (asks user to rephrase)",
        "request": "Dragonfruit soufflé glacé.",
        "session": {},
        "search_rows": [],
        "turns": lambda: [
            ("tools", [("c1", "search_recipes", {"query": "dragonfruit souffle"})]),
            (
                "parsed",
                {
                    "decision": "ask_user",
                    "question": {
                        "question_id": "q-rephrase",
                        "question_text": "I found no recipes for dragonfruit "
                        "soufflé glacé. Want me to look for a lemon dessert "
                        "instead?",
                        "options": ["yes, look for lemon dessert", "no, I will rephrase"],
                    },
                    "note": "empty retrieval",
                },
            ),
        ],
    },
    {
        "key": "tool-failure",
        "title": "Tool failure (Epicure not configured, no retry)",
        "request": "Chicken pairings for a roast.",
        "session": {},
        "epicure_enabled": False,
        "turns": lambda: [
            (
                "tools",
                [
                    ("c1", "find_balanced_pairings", {"ingredient": "chicken"}),
                    ("c2", "search_recipes", {"query": "roast chicken"}),
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
                finish(
                    [
                        opt("curry-1", CHICKEN_Q, "Creamy Chicken Curry"),
                        opt("lentil-2", LENTIL_Q, "Red Lentil Soup"),
                    ]
                ),
            ),
        ],
    },
    {
        "key": "budget-exhaustion",
        "title": "Budget exhaustion (batch exceeds tool calls)",
        "request": "Compare five chicken dishes.",
        "session": {"tool_calls_remaining": 2},
        "turns": lambda: [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "a"}),
                    ("c2", "search_recipes", {"query": "b"}),
                    ("c3", "search_recipes", {"query": "c"}),
                ],
            ),
        ],
    },
    {
        "key": "wall-clock-stop",
        "title": "Wall-clock stop",
        "request": "A slow search day.",
        "session": {},
        "settings": {"agent_wall_clock_s": 0.3, "tool_timeout_s": 10.0},
        "slow": 0.6,
        "turns": lambda: [
            ("tools", [("c1", "search_recipes", {"query": "slow"})]),
        ],
    },
    {
        "key": "epicure-skip",
        "title": "Epicure skip (simple technique question)",
        "request": "How do I boil an egg?",
        "session": {},
        "turns": lambda: [
            ("tools", [("c1", "search_recipes", {"query": "egg"})]),
            (
                "parsed",
                finish(
                    [
                        {
                            "dataset_id": "odunola/foodie",
                            "source_id": "lentil-2",
                            "title": "Red Lentil Soup",
                            "quantities": [],
                            "adaptations": [],
                        },
                        {
                            "dataset_id": "odunola/foodie",
                            "source_id": "curry-1",
                            "title": "Creamy Chicken Curry",
                            "quantities": [],
                            "adaptations": [],
                        },
                    ],
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
        ],
    },
    {
        "key": "skip-rejected-direct-lookup",
        "title": "Skip rejected (direct_recipe_lookup removed)",
        "request": "Vegetarian dinner, something hearty.",
        "session": {"constraints": {"dietary_constraints": ["vegetarian"]}},
        "turns": lambda: [
            (
                "tools",
                [
                    ("c1", "search_recipes", {"query": "hearty vegetarian"}),
                    (
                        "c2",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                    ),
                ],
            ),
            (
                "parsed",
                finish(
                    [opt("lentil-2", LENTIL_Q, "Red Lentil Soup")],
                    constraints_honored=["dietary_constraints"],
                    epicure_skip_reason="direct_recipe_lookup",
                ),
            ),
            (
                "parsed",
                finish(
                    [opt("lentil-2", LENTIL_Q, "Red Lentil Soup")],
                    constraints_honored=["dietary_constraints"],
                    epicure_skip_reason="direct_recipe_lookup",
                ),
            ),
        ],
    },
    {
        "key": "skip-rejected-pairing-cue",
        "title": "Skip rejected (pairing cue in technique question)",
        "request": "How do I boil an egg, and what soup goes with it?",
        "session": {},
        "turns": lambda: [
            ("tools", [("c1", "search_recipes", {"query": "egg soup"})]),
            (
                "parsed",
                finish(
                    [
                        {
                            "dataset_id": "odunola/foodie",
                            "source_id": "lentil-2",
                            "title": "Red Lentil Soup",
                            "quantities": [],
                            "adaptations": [],
                        },
                        {
                            "dataset_id": "odunola/foodie",
                            "source_id": "curry-1",
                            "title": "Creamy Chicken Curry",
                            "quantities": [],
                            "adaptations": [],
                        },
                    ],
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
            (
                "parsed",
                finish(
                    [
                        {
                            "dataset_id": "odunola/foodie",
                            "source_id": "lentil-2",
                            "title": "Red Lentil Soup",
                            "quantities": [],
                            "adaptations": [],
                        },
                        {
                            "dataset_id": "odunola/foodie",
                            "source_id": "curry-1",
                            "title": "Creamy Chicken Curry",
                            "quantities": [],
                            "adaptations": [],
                        },
                    ],
                    epicure_skip_reason="simple_technique_question",
                ),
            ),
        ],
    },
    {
        "key": "evidence-rejected-unretrieved",
        "title": "Options rejected (valid ID never retrieved)",
        "request": "I want a chicken dinner for tonight, about 30 minutes.",
        "session": {},
        "turns": lambda: [
            (
                "parsed",
                finish([opt("curry-1", CHICKEN_Q, "Creamy Chicken Curry")]),
            ),
            (
                "parsed",
                finish([opt("curry-1", CHICKEN_Q, "Creamy Chicken Curry")]),
            ),
        ],
    },
    {
        "key": "no-progress",
        "title": "No progress (identical calls, no new information)",
        "request": "Chicken please.",
        "session": {},
        "turns": lambda: [
            ("tools", [("c1", "search_recipes", {"query": "chicken"})]),
            ("tools", [("c2", "search_recipes", {"query": "chicken"})]),
            ("tools", [("c3", "search_recipes", {"query": "chicken"})]),
        ],
    },
]


def run_scenario(engine: Any, spec: dict[str, Any]) -> dict[str, Any]:
    import uuid

    from culinary_copilot.agent.loop import (
        AgentDeps,
        AgentLoopError,
        record_answer,
        record_select,
        run_agent,
    )
    from culinary_copilot.config import Settings
    from culinary_copilot.domain.sessions import SessionState
    from culinary_copilot.services.session_store import PostgresSessionStore

    settings = Settings(
        _env_file=None,
        epicure_enabled=spec.get("epicure_enabled", True),
        **spec.get("settings", {}),
    )
    store = PostgresSessionStore(engine)
    sid = f"ses-{spec['key'][:8]}-{uuid.uuid4().hex[:6]}"
    session_seed = dict(spec.get("session", {}))
    store.create(SessionState(id=sid, **session_seed))

    def _deps(provider: Any) -> AgentDeps:
        return AgentDeps(
            settings=settings,
            session_store=store,
            provider=provider,
            tool_context=make_context(
                store,
                settings,
                search_rows=spec.get("search_rows", None) if "search_rows" in spec else None,
                slow=spec.get("slow", 0.0),
                epicure_enabled=spec.get("epicure_enabled", True),
            ),
            recipe_resolver=lambda ds, s: DOCS.get((ds, s)),
            request_text=spec.get("request"),
        )

    runs: list[dict[str, Any]] = []

    def _usage_totals() -> tuple[int, int]:
        used_in = used_out = 0
        for event in store.list_events(sid):
            payload = event.payload or {}
            used_in += int(payload.get("input_tokens") or 0)
            used_out += int(payload.get("output_tokens") or 0)
        return used_in, used_out

    def _budgets() -> dict[str, int]:
        current = store.get(sid)
        assert current is not None
        used_in, used_out = _usage_totals()
        return {
            "steps_remaining": current.steps_remaining,
            "tool_calls_remaining": current.tool_calls_remaining,
            "input_tokens_used": used_in,
            "output_tokens_used": used_out,
        }

    def _do_run(provider: Any, expected_revision: Any = None) -> dict[str, Any]:
        before_in, before_out = _usage_totals()
        try:
            result = asyncio.run(
                run_agent(sid, deps=_deps(provider), expected_revision=expected_revision)
            )
            run: dict[str, Any] = {
                "stop_reason": result.stop_reason,
                "phase": result.phase,
                "revision": result.revision,
                "final": result.final,
            }
        except AgentLoopError as exc:
            current = store.get(sid)
            run = {
                "stop_reason": exc.reason,
                "error": True,
                "http_status": exc.http_status,
                "message": exc.message,
                "next_action": exc.next_action,
                "revision": current.revision if current else None,
            }
        after_in, after_out = _usage_totals()
        # Full pre-turn estimates (items + tool defs + schema), as enforced.
        run["turn_input_tokens"] = list(provider.seen_inputs_estimated)
        run["tokens_used"] = {
            "input": after_in - before_in,
            "output": after_out - before_out,
        }
        run["budgets_after"] = _budgets()
        return run

    runs.append(_do_run(ScriptedProvider(spec["turns"]())))
    if "answer" in spec:
        current = store.get(sid)
        assert current is not None
        record_answer(
            store,
            sid,
            expected_revision=current.revision,
            question_id=spec["answer"]["question_id"],
            answer=spec["answer"]["answer"],
        )
        runs.append(_do_run(ScriptedProvider(spec["resume_turns"]())))
    if "select" in spec:
        current = store.get(sid)
        assert current is not None
        record_select(
            store,
            sid,
            expected_revision=current.revision,
            dataset_id=spec["select"]["dataset_id"],
            source_id=spec["select"]["source_id"],
        )
        runs.append(_do_run(ScriptedProvider(spec["plan_turns"]())))

    final_state = store.get(sid)
    assert final_state is not None
    events = [
        {"type": e.event_type, "payload": e.payload}
        for e in store.list_events(sid)
        if e.event_type != "created"
    ]
    return {
        "key": spec["key"],
        "title": spec["title"],
        "request": spec["request"],
        "session_seed": session_seed,
        "epicure_enabled": spec.get("epicure_enabled", True),
        "runs": runs,
        "final_phase": final_state.current_phase,
        "suggestions": final_state.suggestions,
        "cooking_plan": final_state.cooking_plan,
        "selected_dish": final_state.selected_dish,
        "confirmed_answers": final_state.confirmed_answers,
        "unresolved_questions": final_state.unresolved_questions,
        "epicure_outcome": final_state.epicure_outcome,
        "epicure_skip_reason": final_state.epicure_skip_reason,
        "events": events,
    }


def _describe_option(option: dict[str, Any]) -> str:
    """Plain-language option: title, IDs, source facts vs adaptations."""
    head = (
        f"Option {option.get('title')!r} ({option.get('dataset_id')}, {option.get('source_id')}):"
    )
    from_source = "; ".join(
        f"{q.get('amount')} {q.get('unit') or ''} {q.get('ingredient')}".strip()
        for q in option.get("quantities") or []
    )
    bits = [head]
    if from_source:
        bits.append(f"from the source: {from_source}")
    for adaptation in option.get("adaptations") or []:
        bits.append(
            f"adaptation (labelled {adaptation.get('label')}): {adaptation.get('description')}"
        )
    for sub in option.get("substitutions") or []:
        bits.append(f"unverified substitution: {sub}")
    return " ".join(bits) + "."


def _describe_plan(plan: dict[str, Any]) -> str:
    """Plain-language plan: source, sections, quantities, adaptations."""
    source = plan.get("source") or {}
    bits = [
        f"Plan from ({source.get('dataset_id')}, {source.get('source_id')}): "
        f"{len(plan.get('mise_en_place') or [])} mise en place items, "
        f"{len(plan.get('steps') or [])} steps, "
        f"plating: {plan.get('plating')}.".replace("  ", " ")
    ]
    for claim in plan.get("quantities") or []:
        bits.append(
            f"from the source: {claim.get('amount')} {claim.get('unit') or ''} "
            f"{claim.get('ingredient')}.".replace("  ", " ")
        )
    for adaptation in plan.get("adaptations") or []:
        bits.append(
            f"adaptation (labelled {adaptation.get('label')}): {adaptation.get('description')}."
        )
    return " ".join(bits)


def render_review(trajectories: list[dict[str, Any]]) -> str:
    generated = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        "# Phase 3 review packet: 13 offline trajectories",
        "",
        f"Generated {generated} by `evals/phase3_agent/generate.py` from actual "
        "loop outputs (scripted fake provider + fake tools, disposable "
        "`culinary_check_packet` database). No model calls, no network, no "
        "paid calls. Live evaluation is a separate, unrun plan "
        "(`LIVE_PLAN.md`).",
        "",
        "Regenerated 2026-09-29 under P3-A-01 (options need same-session "
        "retrieval evidence; quantities and plans need get_recipe) and "
        "P3-A-02 (Epicure queried by default including direct requests; "
        "`direct_recipe_lookup` removed; pairing-cue guard; degraded "
        "mode). Three scenarios show the new rejections; the empty-"
        "retrieval question now offers a concrete choice without claiming "
        "an alternative was found (owner Q1 note).",
        "",
        "Conventions: `stop_reason` is the stable loop reason; tool outcomes "
        "are `ok`/`error_type`/`reason`; Epicure lines record each "
        "suggestion's use or rejection in one line.",
        "",
        "Token sizes are full pre-turn estimates (input items, offered tool "
        "definitions, directive schema), chars/4 — the repo has no token "
        "estimator; per-run and per-session totals come from the same "
        "accounting the loop enforces (provider-reported when present, "
        "estimated otherwise). Budgets after each run show steps and tool "
        "calls remaining plus session token totals.",
        "",
    ]
    for traj in trajectories:
        parts.append(f"## {traj['title']} (`{traj['key']}`)")
        parts.append("")
        parts.append(f"User request: {traj['request']}")
        parts.append("")
        for index, run in enumerate(traj["runs"], 1):
            label = f"Run {index}" if len(traj["runs"]) > 1 else "Run"
            parts.append(f"{label}: stopped with `{run['stop_reason']}`.")
            if run.get("error"):
                parts.append(
                    f"Error {run['http_status']}: {run['message']} "
                    f"(next_action: {run['next_action']})."
                )
            final = run.get("final") or {}
            if final.get("question"):
                parts.append(f"Asked: {final['question']['question_text']}")
            for option in final.get("options") or []:
                parts.append(_describe_option(option))
            if final.get("single_option_reason"):
                parts.append(f"Single-option reason recorded: {final['single_option_reason']}.")
            if final.get("plan"):
                parts.append(_describe_plan(final["plan"]))
            turns = ", ".join(str(t) for t in run.get("turn_input_tokens", []))
            used = run.get("tokens_used", {})
            parts.append(
                f"Turn input sizes (full pre-turn estimate, chars/4 tokens): [{turns}]; "
                f"this run used {used.get('input', 0)} in / {used.get('output', 0)} out."
            )
            budgets = run.get("budgets_after", {})
            parts.append(
                f"Budgets after: {budgets.get('steps_remaining')} steps, "
                f"{budgets.get('tool_calls_remaining')} tool calls, "
                f"{budgets.get('input_tokens_used')} input tokens, "
                f"{budgets.get('output_tokens_used')} output tokens used in session."
            )
            parts.append("")
        tool_events = [e for e in traj["events"] if e["type"] == "tool_call"]
        if tool_events:
            calls = "; ".join(
                f"{e['payload'].get('tool')} "
                f"({'ok' if e['payload'].get('outcome') == 'ok' else e['payload'].get('reason')})"
                for e in tool_events
            )
            parts.append(f"Tools called: {calls}.")
        questions = [e for e in traj["events"] if e["type"] == "agent_question"]
        for event in questions:
            parts.append(f"Question asked: `{event['payload'].get('question_id')}`.")
        finished = [e for e in traj["events"] if e["type"] == "agent_finished"]
        for event in finished:
            for line in event["payload"].get("epicure_lines", []):
                parts.append(f"Epicure: {line}.")
            if event["payload"].get("epicure_skip_reason"):
                parts.append(f"Epicure skipped: {event['payload']['epicure_skip_reason']}.")
            if event["payload"].get("epicure_degraded"):
                parts.append("Epicure degraded mode recorded.")
        if traj["epicure_outcome"]:
            parts.append(f"Epicure outcome: {traj['epicure_outcome']}.")
        if traj["epicure_skip_reason"]:
            parts.append(f"Epicure skip reason: {traj['epicure_skip_reason']}.")
        if traj["confirmed_answers"]:
            parts.append(f"Confirmed answers kept: {traj['confirmed_answers']}.")
        if traj["selected_dish"]:
            parts.append(f"Selected dish: {traj['selected_dish']}.")
        if traj["cooking_plan"]:
            parts.append(f"Cooking plan plating: {traj['cooking_plan'].get('plating')}.")
        parts.append(f"Final phase: `{traj['final_phase']}`.")
        parts.append("")
    return "\n".join(parts)


if __name__ == "__main__":
    sys.exit(main())
