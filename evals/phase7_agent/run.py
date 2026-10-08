#!/usr/bin/env python3
"""Phase 7 offline scenario harness (part 1, offline only).

Runs each case in cases.json through the real agent loop, tools and
validators with a scripted provider (synthetic turns, not model output)
and fake or disposable-DB tools. No paid calls, no embeddings calls, no
web requests, no downloads. Writes go to the disposable
``culinary_check_phase7`` database only (name contains "check").

Usage (from repo root, one command)::

    uv run python evals/phase7_agent/run.py

Outputs (into this directory):
- results.json (case sha256, per-case terminals, aggregate metrics,
  budget evidence, Epicure comparison status, technique miss record)
- epicure_compare.json (balanced vs cooc on fixed synthetic list, or
  missing-asset note)

cases.json is frozen before the first scored run: results.json records
its sha256. A later edit means a new version, never a silent change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
import uuid
from collections import Counter, deque
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

DB_NAME = "culinary_check_phase7"

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
PEANUT_DOC = {
    "dataset_id": "odunola/foodie",
    "source_id": "peanut-3",
    "title": "Peanut Chicken",
    "servings": 4.0,
    "ingredients": [
        {"canonical": "chicken", "amount": "500", "unit": "g", "quantity_text": "500 g"},
        {"canonical": "peanut", "amount": "50", "unit": "g", "quantity_text": "50 g"},
    ],
    "instructions": ["Brown the chicken.", "Add peanuts and serve."],
}
OAT_DOC = {
    "dataset_id": "odunola/foodie",
    "source_id": "oat-5",
    "title": "Oat Milk Porridge",
    "servings": 2.0,
    "ingredients": [
        {"canonical": "oat milk", "amount": "1", "unit": "cup", "quantity_text": "1 cup oat milk"},
    ],
    "instructions": ["Warm the oat milk.", "Serve hot."],
}
FLOUR_DOC = {
    "dataset_id": "odunola/foodie",
    "source_id": "flour-6",
    "title": "Flour Pancakes",
    "servings": 4.0,
    "ingredients": [
        {"canonical": "flour", "amount": "1.5", "unit": "cup", "quantity_text": "1 1/2 cups"},
    ],
    "instructions": ["Mix the flour.", "Cook pancakes."],
}
DOCS = {
    ("odunola/foodie", "curry-1"): CURRY_DOC,
    ("odunola/foodie", "lentil-2"): LENTIL_DOC,
    ("odunola/foodie", "peanut-3"): PEANUT_DOC,
    ("odunola/foodie", "oat-5"): OAT_DOC,
    ("odunola/foodie", "flour-6"): FLOUR_DOC,
    ("odunola/foodie", "quick-7"): {
        "dataset_id": "odunola/foodie",
        "source_id": "quick-7",
        "title": "20-Minute Chicken Parmesan",
        "servings": 4.0,
        "ingredients": [
            {
                "canonical": "chicken",
                "amount": "500",
                "unit": "g",
                "quantity_text": "500 g",
            },
        ],
        "instructions": ["Coat the chicken.", "Bake until done.", "Serve hot."],
    },
    # Synthetic fixture shaped like foodie-013643 (6 stored directions,
    # raw chicken, cashew method) for the plan-attribution cases.
    ("odunola/foodie", "cashew-8"): {
        "dataset_id": "odunola/foodie",
        "source_id": "cashew-8",
        "title": "Creamy Cashew Chicken Curry",
        "servings": 4.0,
        "ingredients": [
            {
                "canonical": "chicken",
                "amount": "500",
                "unit": "g",
                "quantity_text": "500 g",
            },
            {
                "canonical": "cashews",
                "amount": "100",
                "unit": "g",
                "quantity_text": "100 g",
            },
        ],
        "instructions": [
            "Mix the salt and spices in a small bowl.",
            "Cut the chicken into pieces.",
            "Coat the chicken with oil and spices; refrigerate to marinate.",
            "Brown the chicken in butter over high heat.",
            "Cook the onion, garlic and remaining spices.",
            "Blend the cashews with cold water until smooth and stir in.",
        ],
    },
    # H7 v10: one clipped direction (>600 chars, real words so
    # attribution word-overlap can pass) for the clipped-directions case.
    ("odunola/foodie", "long-10"): {
        "dataset_id": "odunola/foodie",
        "source_id": "long-10",
        "title": "Long Direction Stew",
        "servings": 2.0,
        "ingredients": [
            {
                "canonical": "chicken",
                "amount": "500",
                "unit": "g",
                "quantity_text": "500 g",
            },
        ],
        "instructions": [
            "Chop the onion.",
            ("Fold the dough gently and rest. " * 25).strip(),
            "Serve hot.",
        ],
    },
    # H7 v10: 14 directions for the omitted-index path (same shape as
    # the H3 unit fixture; the harness case uses long-10, this doc pins
    # the >12 branch in search_rows_for).
    ("odunola/foodie", "many-11"): {
        "dataset_id": "odunola/foodie",
        "source_id": "many-11",
        "title": "Fourteen Step Rice",
        "servings": 2.0,
        "ingredients": [
            {
                "canonical": "rice",
                "amount": "1",
                "unit": "cup",
                "quantity_text": "1 cup",
            },
        ],
        "instructions": [f"Do step {i} with rice." for i in range(14)],
    },
    # H7 v10: tree-nut fixture (walnuts violate tree nuts).
    ("odunola/foodie", "walnut-9"): {
        "dataset_id": "odunola/foodie",
        "source_id": "walnut-9",
        "title": "Walnut Cake",
        "servings": 4.0,
        "ingredients": [
            {
                "canonical": "walnuts",
                "amount": "100",
                "unit": "g",
                "quantity_text": "100 g walnuts",
            },
            {
                "canonical": "flour",
                "amount": "200",
                "unit": "g",
                "quantity_text": "200 g flour",
            },
        ],
        "instructions": ["Mix the walnuts with flour.", "Bake the cake."],
    },
}

ROWS_DEFAULT = [
    {"dataset_id": "odunola/foodie", "source_id": "curry-1", "title": "Creamy Chicken Curry"},
    {"dataset_id": "odunola/foodie", "source_id": "lentil-2", "title": "Red Lentil Soup"},
]
# (search row variants are built by search_rows_for below)


def search_rows_for(spec: str | list[Any] | None) -> list[dict[str, Any]]:
    if spec == "default" or spec is None:
        return list(ROWS_DEFAULT)
    if spec == "empty":
        return []
    if spec == "peanut":
        return [
            {"dataset_id": "odunola/foodie", "source_id": "peanut-3", "title": "Peanut Chicken"},
            {"dataset_id": "odunola/foodie", "source_id": "lentil-2", "title": "Red Lentil Soup"},
        ]
    if spec == "oat":
        return [
            {"dataset_id": "odunola/foodie", "source_id": "oat-5", "title": "Oat Milk Porridge"},
            {"dataset_id": "odunola/foodie", "source_id": "lentil-2", "title": "Red Lentil Soup"},
        ]
    if spec == "flour":
        return [
            {"dataset_id": "odunola/foodie", "source_id": "flour-6", "title": "Flour Pancakes"},
        ]
    if spec == "quick":
        return [
            {
                "dataset_id": "odunola/foodie",
                "source_id": "quick-7",
                "title": "20-Minute Chicken Parmesan",
            },
            {"dataset_id": "odunola/foodie", "source_id": "lentil-2", "title": "Red Lentil Soup"},
        ]
    if spec == "cashew":
        return [
            {
                "dataset_id": "odunola/foodie",
                "source_id": "cashew-8",
                "title": "Creamy Cashew Chicken Curry",
            },
        ]
    if spec == "long":
        return [
            {
                "dataset_id": "odunola/foodie",
                "source_id": "long-10",
                "title": "Long Direction Stew",
            },
        ]
    if spec == "many":
        return [
            {
                "dataset_id": "odunola/foodie",
                "source_id": "many-11",
                "title": "Fourteen Step Rice",
            },
        ]
    if spec == "walnut":
        return [
            {"dataset_id": "odunola/foodie", "source_id": "walnut-9", "title": "Walnut Cake"},
            {"dataset_id": "odunola/foodie", "source_id": "lentil-2", "title": "Red Lentil Soup"},
        ]
    if spec == "curry-raw":
        return list(ROWS_DEFAULT)
    if isinstance(spec, list):
        return list(spec)
    return list(ROWS_DEFAULT)


SAFETY_ROW = {
    "doc_id": "tech-fda-safe-32",
    "chunk_id": 0,
    "section": "Safe Food Handling",
    "title": "Safe Food Handling",
    "url": "https://www.fda.gov/food/safe",
    "licence": "public-domain",
    "licence_url": "https://www.fda.gov",
    "attribution_text": "synthetic safety chunk for offline harness",
    "excerpt": "Cook poultry to 165 F. (synthetic)",
}
EGG_ROW = {
    "doc_id": "tech-egg-boil-18",
    "chunk_id": 0,
    "section": "Boiling Eggs",
    "title": "Boiling Eggs",
    "url": "https://example.com/egg-boil",
    "licence": "CC-BY-SA-4.0",
    "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
    "attribution_text": "synthetic egg chunk for offline harness",
    "excerpt": "Simmer eggs gently. (synthetic)",
}


def technique_rows_for(spec: Any) -> list[dict[str, Any]]:
    if spec == "egg":
        return [dict(EGG_ROW)]
    if spec == "safety":
        return [dict(SAFETY_ROW)]
    if spec == "empty":
        return []
    if isinstance(spec, list):
        return list(spec)
    return [dict(SAFETY_ROW)]


TECHNIQUE_RESOLVER_ROWS = {
    ("tech-fda-safe-32", 0): {
        "doc_id": "tech-fda-safe-32",
        "chunk_id": 0,
        "section": "Safe Food Handling",
        "title": "Safe Food Handling",
        "url": "https://www.fda.gov/food/safe",
        "licence": "public-domain",
        "licence_url": "https://www.fda.gov",
        "attribution_text": "synthetic safety chunk for offline harness",
        "chunk_text": "Cook poultry to a safe internal temperature of 165 F. (synthetic)",
    },
    ("tech-egg-boil-18", 0): {
        "doc_id": "tech-egg-boil-18",
        "chunk_id": 0,
        "section": "Boiling Eggs",
        "title": "Boiling Eggs",
        "url": "https://example.com/egg-boil",
        "licence": "CC-BY-SA-4.0",
        "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "attribution_text": "synthetic egg chunk for offline harness",
        "chunk_text": "Simmer eggs gently then cool. (synthetic)",
    },
}


def fake_technique_resolver(doc_id: str, chunk_id: int) -> dict[str, Any] | None:
    row = TECHNIQUE_RESOLVER_ROWS.get((doc_id, chunk_id))
    return dict(row) if row is not None else None


class ScriptedProvider:
    def __init__(self, turns: list[Any]) -> None:
        from culinary_copilot.llm.client import NativeToolCall, NativeTurnResult

        self._ntc = NativeToolCall
        self._ntr = NativeTurnResult
        self.turns: deque[Any] = deque(turns)

    async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
        if not self.turns:
            raise AssertionError("provider script exhausted")
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
        if kind == "empty":
            return self._ntr(tool_calls=[], parsed=None, chain_items=[])
        if kind == "raw_tools":
            calls = [
                self._ntc(call_id=cid, name=name, arguments=args) for cid, name, args in payload
            ]
            chain = [
                {"type": "function_call", "call_id": cid, "name": name, "arguments": args}
                for cid, name, args in payload
            ]
            return self._ntr(tool_calls=calls, parsed=None, chain_items=chain)
        raise AssertionError(f"bad script kind {kind!r}")


class FakeEpicureCore:
    # H7 v10: optional ``vocab`` names drive the pairing-claim guard
    # via the ``vocabulary()`` hook the tool path reads; None keeps
    # the old skip behaviour for the pre-v10 cases.
    def __init__(self, enabled: bool = True, vocab: Any = None) -> None:
        self._enabled = enabled
        self._vocab = set(vocab) if vocab else None

    def vocabulary(self) -> Any:
        return set(self._vocab) if self._vocab is not None else None

    def find_balanced_pairings(self, ingredient: str, k: int = 5):  # type: ignore[no-untyped-def]
        from culinary_copilot.tools.epicure import EpicureDisabledError, Pairing

        if not self._enabled:
            raise EpicureDisabledError("disabled")
        if ingredient == "chicken":
            base = [("pork", 0.5), ("beef", 0.4)]
        elif ingredient == "flour":
            base = [("coconut milk", 0.45), ("sour cream", 0.4)]
        else:
            base = [("coconut milk", 0.45), ("sour cream", 0.4)]
        return [Pairing(ingredient=n, score=s) for n, s in base[:k]]


class FakeWebSearchProvider:
    """Offline fake search sub-request returning fixed synthetic sources."""

    def __init__(self, urls: list[str]) -> None:
        self.urls = list(urls)
        self.calls: list[dict[str, Any]] = []

    async def complete_web_search(
        self, *, instruction: str, query: str, max_output_tokens: Any = None, timeout: Any = None
    ) -> Any:
        from culinary_copilot.llm.client import WebSearchResult

        self.calls.append({"instruction": instruction, "query": query})
        sources = [
            {
                "url": url,
                "title": "Okonomiyaki guide" if "okonomiyaki" in url else f"Guide {i}",
                "excerpt_model": "a savoury pancake (synthetic model text)",
                "published_at": None,
            }
            for i, url in enumerate(self.urls)
        ]
        return WebSearchResult(
            performed=True,
            parsed={"summary": "Okonomiyaki is a savoury pancake. (synthetic)", "sources": sources},
            web_search_call_ids=["ws_fake_1"],
            citations=[{"url": url, "title": "Okonomiyaki guide"} for url in self.urls],
            action_sources=[{"type": "url", "url": url} for url in self.urls],
            model="fake",
            latency_ms=1,
            attempts=1,
        )


def make_context(store: Any, settings: Any, case: dict[str, Any]) -> Any:
    from culinary_copilot.tools.registry import ToolContext

    rows = search_rows_for(case.get("search_rows"))
    tech_rows = technique_rows_for(case.get("technique_rows"))
    slow = float(case.get("slow_search") or 0.0)
    slow_state = {"n": 0}
    # ((dataset_id, source_id), "full" | "short") per get_recipe call,
    # read by run_case for grading the duplicate rule.
    fetch_log: list[tuple[tuple[str, str], str]] = []

    def _search(args: Any, context: Any) -> dict[str, Any]:
        # Timeout probe: sleep once (first call) so the recovery turn runs fast.
        if slow and slow_state["n"] == 0:
            slow_state["n"] += 1
            time.sleep(slow)
        # Unavailable probe for p7-tool-unavailable (offered tool, backend failure).
        try:
            q = str(getattr(args, "query", "") or "")
        except Exception:
            q = ""
        if q == "trigger-unavailable":
            return {
                "ok": False,
                "error_type": "unavailable",
                "reason": "tool_unavailable",
                "message": "synthetic transient outage for offline harness",
                "next_action": "retry",
            }
        return {"ok": True, "mode_ran": "fulltext", "cost_class": "free", "results": list(rows)}

    def _get(args: Any, context: Any) -> dict[str, Any]:
        # Mirror the production repeat-fetch rule: an identical pair
        # already returned full in this session comes back short — but
        # only when its full output is still visible to the model (the
        # loop sets visible_full_recipes per turn). Each call is logged
        # for grading.
        from culinary_copilot.agent.loop import recipe_session_evidence

        def _short(doc: dict[str, Any]) -> dict[str, Any]:
            fetch_log.append(((args.dataset_id, args.source_id), "short"))
            return {
                "ok": True,
                "duplicate_of_session_evidence": True,
                "dataset_id": args.dataset_id,
                "source_id": args.source_id,
                "title": str(doc.get("title") or ""),
                "message": (
                    "already returned in this session; use the earlier "
                    "evidence (see the evidence digest), do not re-fetch"
                ),
            }

        session_store = getattr(context, "session_store", None)
        bound_session = getattr(context, "bound_session_id", None)
        visible = getattr(context, "visible_full_recipes", None)
        if session_store is not None and bound_session and visible is not None:
            try:
                _, full_pairs = recipe_session_evidence(
                    store=session_store, session_id=str(bound_session)
                )
            except Exception:
                full_pairs = []
            if (args.dataset_id, args.source_id) in set(full_pairs) and (
                args.dataset_id,
                args.source_id,
            ) in set(visible):
                doc = DOCS.get((args.dataset_id, args.source_id)) or {}
                return _short(doc)
        fetch_log.append(((args.dataset_id, args.source_id), "full"))
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

    def _tech(args: Any, context: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode_ran": "fulltext",
            "match": "all" if tech_rows else "any",
            "cost_class": "free",
            "results": list(tech_rows),
        }

    core = FakeEpicureCore(
        enabled=bool(case.get("epicure_enabled", True)), vocab=case.get("epicure_vocab")
    )
    # Web: real search_web impl with a fake sub-request provider when the
    # case enables search; permission-off and limit cases use the real impl
    # so backend enforcement is exercised. No impl_overrides for search_web.
    web_urls = case.get("web_sources") or []
    search_provider = (
        FakeWebSearchProvider(list(web_urls))
        if web_urls or case.get("id", "").startswith("p7-search")
        else None
    )
    ctx = ToolContext(
        settings=settings,
        engine=None,
        session_store=store,
        impl_overrides={"search_recipes": _search, "get_recipe": _get, "search_techniques": _tech},
        epicure_core=core,
        epicure_cooc=core,
        epicure_chem=core,
        search_provider=search_provider,
    )
    ctx.fetch_log = fetch_log  # type: ignore[attr-defined]
    return ctx


def run_case(engine: Any, case: dict[str, Any]) -> dict[str, Any]:

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
        epicure_enabled=case.get("epicure_enabled", True),
        **case.get("settings", {}),
    )
    store = PostgresSessionStore(engine)
    sid = f"ses-p7-{case['id'][:12]}-{uuid.uuid4().hex[:6]}"
    session_seed: dict[str, Any] = dict(case.get("session", {}))
    # Seed confirmed answers for allergen cases (naming answer present before run).
    create_kwargs = dict(session_seed)
    state = SessionState(id=sid, **create_kwargs)
    if case.get("confirmed_answers"):
        state.confirmed_answers = list(case["confirmed_answers"])
    store.create(state)

    contexts: list[Any] = []

    def _deps(provider: Any, use_store: Any = None) -> AgentDeps:
        ctx_store = use_store or store
        ctx = make_context(ctx_store, settings, case)
        contexts.append(ctx)
        return AgentDeps(
            settings=settings,
            session_store=ctx_store,
            provider=provider,
            tool_context=ctx,
            recipe_resolver=lambda ds, s: DOCS.get((ds, s)),
            technique_resolver=fake_technique_resolver,
            request_text=case.get("request"),
        )

    # Record the user request as a user_message event (same helper as API).
    from culinary_copilot.agent.loop import record_user_message

    if case.get("request"):
        record_user_message(store, sid, text=str(case["request"]))

    runs: list[dict[str, Any]] = []

    def _do_run(provider: Any, use_store: Any = None) -> dict[str, Any]:
        eff_store = use_store or store
        try:
            result = asyncio.run(run_agent(sid, deps=_deps(provider, eff_store)))
            return {
                "stop_reason": result.stop_reason,
                "phase": result.phase,
                "revision": result.revision,
                "final": result.final,
            }
        except AgentLoopError as exc:
            current = eff_store.get(sid)
            return {
                "stop_reason": exc.reason,
                "error": True,
                "http_status": exc.http_status,
                "message": exc.message,
                "next_action": exc.next_action,
                "revision": current.revision if current else None,
            }

    runs.append(_do_run(ScriptedProvider([list(t) for t in case.get("turns", [])])))
    # Ask-and-resume (with optional restart: fresh store/app objects, same DB).
    if case.get("answer"):
        last_final = runs[-1].get("final") or {}
        asked = last_final.get("question") or {}
        qid = asked.get("question_id") or case["answer"]["question_id"]
        if case.get("restart"):
            fresh_store = PostgresSessionStore(engine)
            current = fresh_store.get(sid)
            assert current is not None, "restart case lost session row"
            record_answer(
                fresh_store,
                sid,
                expected_revision=current.revision,
                question_id=qid,
                answer=case["answer"]["answer"],
            )
            runs.append(
                _do_run(
                    ScriptedProvider([list(t) for t in case.get("resume_turns", [])]), fresh_store
                )
            )
        else:
            current = store.get(sid)
            assert current is not None
            record_answer(
                store,
                sid,
                expected_revision=current.revision,
                question_id=qid,
                answer=case["answer"]["answer"],
            )
            runs.append(_do_run(ScriptedProvider([list(t) for t in case.get("resume_turns", [])])))
    if case.get("select"):
        current = store.get(sid)
        assert current is not None
        record_select(
            store,
            sid,
            expected_revision=current.revision,
            dataset_id=case["select"]["dataset_id"],
            source_id=case["select"]["source_id"],
        )
        runs.append(_do_run(ScriptedProvider([list(t) for t in case.get("plan_turns", [])])))
    # H7 v10: follow-up turns in the same session after the plan (a
    # technique question asked after the plan keeps the phase).
    if case.get("followup_turns"):
        runs.append(_do_run(ScriptedProvider([list(t) for t in case.get("followup_turns", [])])))

    final_state = store.get(sid)
    assert final_state is not None
    events = [
        {"type": e.event_type, "payload": e.payload}
        for e in store.list_events(sid)
        if e.event_type != "created"
    ]
    backend_probe: dict[str, Any] | None = None
    if case["id"] == "p7-search-off-refused":
        # Backend gate probe: real search_web impl with permission off must
        # refuse even when called directly (bypassing the offer filter).
        import asyncio as _asyncio

        from culinary_copilot.tools import all_tool_definitions, all_tool_impls, run_tool
        from culinary_copilot.tools.registry import ToolContext as _TC

        _defs = {d.name: d for d in all_tool_definitions(timeout_s=10.0)}
        _ctx = _TC(settings=settings, engine=None, session_store=store)
        _ctx.bound_session_id = sid
        try:
            _out = _asyncio.run(
                run_tool(
                    _defs["search_web"],
                    all_tool_impls()["search_web"],
                    {"query": "direct probe"},
                    _ctx,
                    session_id=sid,
                    call_id="probe-search-off",
                )
            )
        except Exception as exc:
            _out = {"ok": False, "reason": f"{type(exc).__name__}", "message": str(exc)[:200]}
        backend_probe = {
            "reason": _out.get("reason"),
            "ok": bool(_out.get("ok")),
            "refused": _out.get("reason") == "tool_permission_denied",
        }
    return {
        "id": case["id"],
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
        "backend_probe": backend_probe,
        "fetch_log": [
            {"dataset_id": pair[0], "source_id": pair[1], "mode": mode}
            for ctx in contexts
            for pair, mode in getattr(ctx, "fetch_log", [])
        ],
    }


def grade_case(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:

    expected = case.get("expected", {})
    last = result["runs"][-1] if result["runs"] else {}
    stop = last.get("stop_reason", "")
    final = last.get("final") or {}
    events = result.get("events", [])

    # Shape detection.
    if last.get("error"):
        shape = "error"
    elif (final.get("options") or None) is not None:
        shape = "options"
    elif (final.get("plan") or None) is not None:
        shape = "plan"
    elif (final.get("technique_answer") or None) is not None:
        shape = "technique_answer"
    elif (final.get("web_answer") or None) is not None:
        shape = "web_answer"
    elif (final.get("question") or None) is not None:
        shape = "question"
    else:
        shape = "none"

    ok = (stop == expected.get("stop_reason")) and (
        expected.get("shape") in (None, shape)
        or (expected.get("shape") == "error" and last.get("error"))
    )
    if expected.get("min_options") is not None:
        opts = final.get("options") or []
        ok = ok and len(opts) >= int(expected["min_options"])
    # Search-refused check.
    if expected.get("search_refused"):
        reasons = [e["payload"].get("reason") for e in events if e["type"] == "tool_call"]
        tools_called = [
            str(e["payload"].get("tool") or "") for e in events if e["type"] == "tool_call"
        ]
        if expected["search_refused"] == "tool_permission_denied":
            # Loop filters search_web when permission is off, so the model
            # call is rejected as unoffered (no tool_call event) and never
            # reaches the backend. The backend gate itself is probed
            # directly below; here pass when no search_web dispatch ran.
            ok = ok and ("search_web" not in tools_called)
        else:
            ok = ok and (expected["search_refused"] in reasons)
    if expected.get("saw_error"):
        # Strict: the specific typed reason must appear. No generic
        # "error" fallback: any error outcome is not the expected guard.
        reasons = [e["payload"].get("reason") for e in events if e["type"] == "tool_call"]
        ok = ok and (expected["saw_error"] in reasons)
    # Expected rejection: the substring must appear in this run's
    # agent_validation_reject errors. Any other validation_failed
    # reason fails the case.
    if expected.get("expected_rejection"):
        reject_texts: list[str] = []
        for e in events:
            if e["type"] == "agent_validation_reject":
                for err in (e["payload"] or {}).get("errors", []) or []:
                    reject_texts.append(str(err))
        ok = ok and any(expected["expected_rejection"] in t for t in reject_texts)
    # First-finish rejection (multi-finish cases): the substring must
    # appear in the first validation_reject of the run.
    if expected.get("first_rejection_contains"):
        first_reject: list[str] = []
        for e in events:
            if e["type"] == "agent_validation_reject":
                first_reject = [str(err) for err in (e["payload"] or {}).get("errors", []) or []]
                break
        ok = ok and any(expected["first_rejection_contains"] in t for t in first_reject)
    # Forbidden options must be absent from the final.
    if expected.get("forbidden_option_ids"):
        present = {str((o or {}).get("source_id") or "") for o in final.get("options") or []}
        ok = ok and not (set(expected["forbidden_option_ids"]) & present)
    # Expected constraint_check statuses per source_id, scored on the
    # run's own final (final["constraint_check"] entries).
    if expected.get("constraint_status"):
        checks = final.get("constraint_check") or []
        by_source: dict[str, list[dict[str, Any]]] = {}
        for entry in checks:
            if isinstance(entry, dict):
                by_source.setdefault(str(entry.get("source_id") or ""), []).append(entry)
        for source_id, want_status in expected["constraint_status"].items():
            entries = by_source.get(str(source_id), [])
            want_value = (expected.get("constraint_value") or {}).get(str(source_id))
            matched = False
            for entry in entries:
                if str(entry.get("status")) != str(want_status):
                    continue
                if want_value is not None and str(entry.get("value")) != str(want_value):
                    continue
                matched = True
            ok = ok and matched
    # Dropped option must be present with a reason mentioning the guard.
    if expected.get("dropped_with_reason"):
        want = expected["dropped_with_reason"]
        dropped = final.get("dropped_options") or []
        ok = ok and any(
            isinstance(d, dict)
            and str(d.get("source_id")) == str(want.get("source_id"))
            and str(want.get("reason_contains", "")).lower() in str(d.get("error") or "").lower()
            for d in dropped
        )
    # Expected plan attribution label.
    if expected.get("steps_source") is not None:
        plan = final.get("plan") or {}
        ok = ok and plan.get("steps_source") == expected["steps_source"]
    # Expected fetch modes: the last logged get_recipe call per pair
    # must be full or short (duplicate pointer) as declared.
    if expected.get("expect_fetch"):
        last_mode: dict[tuple[str, str], str] = {}
        for entry in result.get("fetch_log") or []:
            if isinstance(entry, dict):
                last_mode[(str(entry.get("dataset_id")), str(entry.get("source_id")))] = str(
                    entry.get("mode")
                )
        for want in expected["expect_fetch"]:
            key = (str(want.get("dataset_id")), str(want.get("source_id")))
            want_mode = "short" if want.get("duplicate") else "full"
            ok = ok and last_mode.get(key) == want_mode
    if expected.get("saw_invalid_transition"):
        texts = json.dumps(events)
        ok = ok and ("invalid phase move" in texts)
    if expected.get("epicure") == "degraded":
        ok = ok and bool(
            (
                final.get("epicure_degraded")
                or result.get("epicure_skip_reason") == "epicure_not_configured"
                or "degraded" in json.dumps(events)
            )
        )
    if expected.get("allergen_checked"):
        # Resumed options must be allergen-checked on the run's own
        # final: every final option needs a constraint_check entry
        # with the allergen value and a non-violated status. Quantity
        # lines alone do not prove the check ran.
        label = str(expected["allergen_checked"])
        checks = final.get("constraint_check") or []
        by_source = {
            str(e.get("source_id") or ""): e
            for e in checks
            if isinstance(e, dict) and str(e.get("value") or "") == label
        }
        for opt in final.get("options") or []:
            entry = by_source.get(str((opt or {}).get("source_id") or ""))
            ok = ok and (isinstance(entry, dict) and str(entry.get("status")) != "violated")
    if expected.get("retrieval_miss"):
        counts = [
            e["payload"].get("result_count")
            for e in events
            if e["type"] == "tool_call" and e["payload"].get("tool") == "search_techniques"
        ]
        ok = ok and counts and all(c == 0 for c in counts)
    if case["id"] == "p7-search-off-refused":
        probe = result.get("backend_probe") or {}
        ok = ok and probe.get("refused") is True
    if expected.get("needs_safety_ref"):
        # The refs must resolve to food-safety technique documents, not
        # merely be non-empty.
        plan = final.get("plan") or {}
        refs = plan.get("technique_refs") or []
        safety = False
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            try:
                chunk_id = int(ref.get("chunk_id"))
            except (TypeError, ValueError):
                continue
            row = TECHNIQUE_RESOLVER_ROWS.get((str(ref.get("doc_id") or ""), chunk_id))
            if not isinstance(row, dict):
                continue
            text = f"{row.get('title') or ''} {row.get('section') or ''}".lower()
            if "safe" in text or "food handling" in text:
                safety = True
        ok = ok and safety
    # H7 v10: follow-up phase (technique answer after a plan keeps it).
    if expected.get("expect_phase") is not None:
        ok = ok and result.get("final_phase") == expected["expect_phase"]
    # H7 v10: wrap-up decision (withholds only the repeated tools).
    if expected.get("expect_wrapup") is not None:
        want = expected["expect_wrapup"] or {}
        want_withheld = set(want.get("withheld") or [])
        want_offered = set(want.get("offered_contains") or [])
        found = False
        for e in events:
            if e["type"] != "agent_turn" or not (e["payload"] or {}).get("wrap_up"):
                continue
            offered = set((e["payload"] or {}).get("offered") or [])
            withheld = {
                str(w.get("tool"))
                for w in ((e["payload"] or {}).get("withheld") or [])
                if isinstance(w, dict)
            }
            reasons = {
                str(w.get("tool")): str(w.get("reason"))
                for w in ((e["payload"] or {}).get("withheld") or [])
                if isinstance(w, dict)
            }
            if want_withheld <= withheld and want_offered <= offered:
                if all(reasons.get(t) == "repeat_wrap_up" for t in want_withheld):
                    found = True
        ok = ok and found
    # H7 v10: a repeated search was marked nothing-new for the model.
    if expected.get("expect_repeat_noted"):
        ok = ok and any(
            e["type"] == "agent_step" and bool((e["payload"] or {}).get("repeat_noted"))
            for e in events
        )
    # H7 v10: one turn withheld a tool for the named reason (or a
    # list of them, e.g. select withholds search and pairings).
    if expected.get("expect_turn_withheld") is not None:
        want_raw = expected["expect_turn_withheld"]
        wants = want_raw if isinstance(want_raw, list) else [want_raw]
        for want in wants:
            if not isinstance(want, dict):
                continue
            found = False
            for e in events:
                if e["type"] != "agent_turn":
                    continue
                for w in (e["payload"] or {}).get("withheld") or []:
                    if not isinstance(w, dict):
                        continue
                    if str(w.get("tool")) == str(want.get("tool")) and str(w.get("reason")) == str(
                        want.get("reason")
                    ):
                        found = True
            ok = ok and found
    # v11 stall recovery: after a step that repeated an earlier call,
    # the run's last turn is a tool-less finishing turn (offered=[]).
    if expected.get("expect_stall_recovery"):
        turns = [e for e in events if e["type"] == "agent_turn"]
        repeated = any(
            e["type"] == "agent_step" and bool((e["payload"] or {}).get("repeated_tools"))
            for e in events
        )
        last_turn = (turns[-1]["payload"] or {}) if turns else {}
        ok = (
            ok and repeated and bool(last_turn.get("final_turn")) and last_turn.get("offered") == []
        )
    if expected.get("stop_message_contains"):
        ok = ok and str(expected["stop_message_contains"]) in str(last.get("message") or "")
    # H7 decision log invariant: every turn records one agent_turn
    # with offered/withheld plus remaining budgets, bounded payloads,
    # and no raw reasoning (only the labelled diagnostic). A run that
    # stops before its first turn (e.g. the token-budget probe) has no
    # turn to record and is vacuously ok.
    turn_events = [e for e in events if e["type"] == "agent_turn"]
    turn_evidence = [
        e
        for e in events
        if e["type"]
        in (
            "tool_call",
            "agent_step",
            "agent_question",
            "agent_finished",
            "agent_validation_reject",
        )
    ]
    decision_ok = bool(turn_events) or not turn_evidence
    for e in turn_events:
        payload = e["payload"] or {}
        if not isinstance(payload.get("offered"), list):
            decision_ok = False
        if not isinstance(payload.get("withheld"), list):
            decision_ok = False
        if payload.get("steps_remaining") is None or payload.get("tool_calls_remaining") is None:
            decision_ok = False
        for w in payload.get("withheld") or []:
            if not isinstance(w, dict) or len(str(w.get("reason") or "")) > 80:
                decision_ok = False
        diag = payload.get("reasoning_diagnostic")
        if diag is not None and (
            not isinstance(diag, dict) or len(str(diag.get("text") or "")) > 500
        ):
            decision_ok = False
    blob = json.dumps(events)
    if "reasoning_content" in blob or "chain_of_thought" in blob:
        decision_ok = False
    ok = ok and decision_ok
    # Oat case: scored on the run's own final. The oat option's
    # constraint_check must be unverified for wheat/gluten. The direct
    # validator call stays only as extra detail.
    oat_detail = None
    if case["id"] == "p7-oat-milk-gluten-gap":
        checks = final.get("constraint_check") or []
        oat_ok = any(
            isinstance(e, dict)
            and str(e.get("source_id")) == "oat-5"
            and str(e.get("status")) == "unverified"
            and str(e.get("value")) == "wheat/gluten"
            for e in checks
        )
        from culinary_copilot.agent.validate import check_allergen_option

        _, entry = check_allergen_option(0, {}, OAT_DOC, "wheat/gluten")
        oat_detail = {"validator_entry": entry, "run_final_ok": bool(oat_ok)}
        ok = ok and oat_ok and entry.get("status") == "unverified"

    # Metrics per case.
    tool_events = [e for e in events if e["type"] == "tool_call"]
    total_calls = len(tool_events)
    invalid_calls = sum(
        1 for e in tool_events if e["payload"].get("reason") == "tool_invalid_arguments"
    )
    validity = 1.0 if total_calls == 0 else (total_calls - invalid_calls) / total_calls
    required = set(case.get("required_tools", []))
    allowed = set(case.get("allowed_tools", []))
    in_scope = required | allowed
    unnecessary = (
        sum(1 for e in tool_events if str(e["payload"].get("tool") or "") not in in_scope)
        if total_calls
        else 0
    )
    unnecessary_rate = (unnecessary / total_calls) if total_calls else 0.0
    forbidden_hit = any(
        str(e["payload"].get("tool") or "") in set(case.get("forbidden_tools", []))
        for e in tool_events
    )
    invalid_transitions = sum(
        1 for e in events if "invalid phase move" in json.dumps(e.get("payload") or {})
    )
    # Epicure compliance (loop rule: consulted or allowlisted skip; web
    # answers and pre-finish stops are exempt).
    has_epicure_evidence = any(
        e["type"] == "tool_call"
        and str(e["payload"].get("tool") or "")
        in {
            "find_balanced_pairings",
            "find_conventional_pairings",
            "find_flavor_pairings",
            "find_substitutions",
        }
        and e["payload"].get("outcome") == "ok"
        for e in events
    ) or bool(result.get("epicure_outcome"))
    skip = result.get("epicure_skip_reason")
    if (
        shape in ("web_answer", "question", "none")
        or case["id"].startswith("p7-search-")
        or stop
        in (
            "agent_max_steps",
            "agent_tool_budget_exhausted",
            "agent_token_budget_exhausted",
            "agent_wall_clock_exceeded",
        )
    ):
        epicure_ok = True
    elif case["id"] in ("p7-epicure-skip-justified",):
        epicure_ok = skip == "simple_technique_question"
    elif case["id"] in ("p7-epicure-degraded",):
        epicure_ok = skip == "epicure_not_configured"
    else:
        epicure_ok = bool(
            has_epicure_evidence
            or (skip in ("simple_technique_question", "epicure_not_configured"))
        )
    # Source-reference correctness: reuse evidence helpers on a light store stub.
    # Recompute from tool_call events (returned_identities) instead of store.
    retrieved: set[tuple[str, str]] = set()
    full: set[tuple[str, str]] = set()
    returned_tech: set[tuple[str, int]] = set()
    for e in tool_events:
        for ident in e["payload"].get("returned_identities") or []:
            if isinstance(ident, dict) and ident.get("dataset_id") and ident.get("source_id"):
                retrieved.add((ident["dataset_id"], ident["source_id"]))
                if ident.get("via") == "full":
                    full.add((ident["dataset_id"], ident["source_id"]))
            if (
                isinstance(ident, dict)
                and ident.get("doc_id") is not None
                and ident.get("chunk_id") is not None
            ):
                try:
                    returned_tech.add((str(ident["doc_id"]), int(ident["chunk_id"])))
                except (TypeError, ValueError):
                    pass
    src_ok = True
    if shape == "options":
        for opt in final.get("options") or []:
            key = (str(opt.get("dataset_id")), str(opt.get("source_id")))
            if key not in retrieved:
                src_ok = False
            for q in opt.get("quantities") or []:
                if key not in full:
                    src_ok = False
    if shape == "plan":
        plan = final.get("plan") or {}
        src = plan.get("source") or {}
        key = (str(src.get("dataset_id")), str(src.get("source_id")))
        if key not in full:
            src_ok = False
        for ref in plan.get("technique_refs") or []:
            if (str(ref.get("doc_id")), int(ref.get("chunk_id"))) not in returned_tech:
                src_ok = False
    if shape == "technique_answer":
        for ref in (final.get("technique_answer") or {}).get("technique_refs") or []:
            if (str(ref.get("doc_id")), int(ref.get("chunk_id"))) not in returned_tech:
                src_ok = False
    if shape == "web_answer":
        # Web refs must equal fake source URLs for the case.
        urls = set(case.get("web_sources") or [])
        for ref in (final.get("web_answer") or {}).get("web_refs") or []:
            if str(ref.get("url")) not in urls:
                src_ok = False
    if shape in ("none",) and last.get("error"):
        src_ok = True
    # Grounding rejections in validation feedback.
    grounding = sum(
        1
        for e in events
        if e["type"] == "agent_validation_reject"
        and (
            "unsupported" in json.dumps(e.get("payload") or {})
            or "unverified" in json.dumps(e.get("payload") or {})
        )
    )
    # Steps: count agent_step events + question/finish steps (each run consumes steps).
    steps = sum(
        1 for e in events if e["type"] in ("agent_step", "agent_question", "agent_finished")
    )
    return {
        "task_completion": bool(ok),
        "stop_reason": stop,
        "shape": shape,
        "tool_calls": total_calls,
        "steps": steps,
        "invalid_transitions": invalid_transitions,
        "tool_argument_validity": validity,
        "unnecessary_call_rate": unnecessary_rate,
        "forbidden_call": bool(forbidden_hit),
        "epicure_compliance": bool(epicure_ok),
        "source_reference_correctness": bool(src_ok),
        "grounding_rejections": grounding,
        "oat_detail": oat_detail,
    }


def run_epicure_comparison() -> dict[str, Any]:
    """Balanced vs cooc on a fixed synthetic list (offline, cache-only)."""
    try:
        from culinary_copilot.config import Settings
        from culinary_copilot.tools.epicure_tools import build_epicure_variants

        settings = Settings(_env_file=None, epicure_enabled=True)
        variants = build_epicure_variants(settings)
        ingredients = ["chicken", "garlic", "lemon", "rice", "tomato"]
        out: dict[str, Any] = {"ingredients": ingredients, "variants": {}}
        for label, key in (("balanced", "core"), ("cooc", "cooc")):
            rows: dict[str, Any] = {}
            for ing in ingredients:
                try:
                    pairs = variants[key].find_balanced_pairings(ing, 5)
                    rows[ing] = [
                        {"ingredient": p.ingredient, "score": float(p.score)}
                        for p in (pairs or [])[:5]
                    ]
                except Exception as exc:
                    rows[ing] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            out["variants"][label] = rows
        out["status"] = "compared-offline-cache-only-no-download"
        out["default_unchanged"] = True
        return out
    except Exception as exc:
        return {
            "status": "missing",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "default_unchanged": True,
        }


def main() -> int:
    from sqlalchemy import create_engine, text

    from culinary_copilot.config import Settings
    from culinary_copilot.recipes import import_data

    cases_path = HERE / "cases.json"
    cases_sha256 = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = payload.get("cases", [])

    settings = Settings()
    base = settings.database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    maint = create_engine(f"{head}/postgres", isolation_level="AUTOCOMMIT")
    with maint.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{DB_NAME}' AND pid <> pg_backend_pid()"
            )
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{DB_NAME}"'))
        conn.execute(text(f'CREATE DATABASE "{DB_NAME}"'))
    maint.dispose()
    results: list[dict[str, Any]] = []
    try:
        engine = create_engine(f"{head}/{DB_NAME}")
        with engine.begin() as conn:
            import_data.apply_migrations(conn)
        for case in cases:
            res = run_case(engine, case)
            grade = grade_case(case, res)
            results.append(
                {
                    "case": {k: case[k] for k in ("id", "criteria", "adversarial") if k in case},
                    "expected": case.get("expected", {}),
                    "result": res,
                    "metrics": grade,
                    "expected_fail": bool(case.get("expected_fail", False)),
                }
            )
        engine.dispose()
    finally:
        maint = create_engine(f"{head}/postgres", isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname='{DB_NAME}' AND pid <> pg_backend_pid()"
                )
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{DB_NAME}"'))
        maint.dispose()

    # Aggregates (expected-fail cases tracked separately, not against pass rate).
    scored = [r for r in results if not r.get("expected_fail")]
    expected_fail = [r for r in results if r.get("expected_fail")]
    completed = sum(1 for r in scored if r["metrics"]["task_completion"])
    adv = [r for r in results if r["case"].get("adversarial")]
    adv_caught = sum(1 for r in adv if r["metrics"]["task_completion"])
    stop_dist = dict(Counter(r["metrics"]["stop_reason"] for r in results))
    aggregate = {
        "total": len(results),
        "scored": len(scored),
        "completed": completed,
        "task_completion_rate": (completed / len(scored)) if scored else 0.0,
        "expected_fail_total": len(expected_fail),
        "expected_fail_completed": sum(1 for r in expected_fail if r["metrics"]["task_completion"]),
        "stop_reason_distribution": stop_dist,
        "invalid_transitions_total": sum(r["metrics"]["invalid_transitions"] for r in results),
        "tool_argument_validity_mean": sum(r["metrics"]["tool_argument_validity"] for r in results)
        / len(results)
        if results
        else 0.0,
        "unnecessary_call_rate_mean": sum(r["metrics"]["unnecessary_call_rate"] for r in results)
        / len(results)
        if results
        else 0.0,
        "epicure_compliance_rate": sum(1 for r in results if r["metrics"]["epicure_compliance"])
        / len(results)
        if results
        else 0.0,
        "source_reference_correctness_rate": sum(
            1 for r in results if r["metrics"]["source_reference_correctness"]
        )
        / len(results)
        if results
        else 0.0,
        "unsupported_claim_cases": sum(
            1 for r in results if r["metrics"]["grounding_rejections"] > 0
        ),
        "adversarial_total": len(adv),
        "adversarial_caught": adv_caught,
        "adversarial_catch_rate": (adv_caught / len(adv)) if adv else 0.0,
        "latency_tokens_cost": "not measured offline (scripted provider)",
    }
    # Cooperative end-to-end trajectory lengths for P3-L-12 (request->options->select->plan).
    e2e_ids = {
        "p7-epicure-default",
        "p7-select-plan-cited",
        "p7-plan-raw-protein-safety",
        "p7-ask-missing-ingredient",
        "p7-restart-survives",
    }
    budget_evidence = [
        {
            "id": r["case"]["id"],
            "steps": r["metrics"]["steps"],
            "tool_calls": r["metrics"]["tool_calls"],
            "stop": r["metrics"]["stop_reason"],
        }
        for r in results
        if r["case"]["id"] in e2e_ids
    ]
    # Technique miss record (full-text mode, offline fakes mirror the lexical miss).
    tq = [
        {
            "id": r["case"]["id"],
            "retrieval_miss": bool(r["expected"].get("retrieval_miss")),
            "stop": r["metrics"]["stop_reason"],
            "completed": r["metrics"]["task_completion"],
        }
        for r in results
        if r["case"]["id"] in ("p7-tq04-chicken-temp", "p7-tq15-pink-chicken")
    ]
    epicure = run_epicure_comparison()
    (HERE / "epicure_compare.json").write_text(
        json.dumps(epicure, indent=2) + "\n", encoding="utf-8"
    )
    # v1 history: v1 scored dietary/adversarial cases on the stop
    # reason alone. Kept so the v2 tightening is auditable.
    history_v1 = {
        "cases_version": "phase7-cases-v1-2026-10-04",
        "cases_sha256": "5655b1ccc792a0106b08d56e01bd7d76248cdf936175660ad9abd03515b5c35b",
        "aggregate": {
            "total": 28,
            "scored": 27,
            "completed": 27,
            "task_completion_rate": 1.0,
            "expected_fail_total": 1,
            "expected_fail_completed": 0,
            "stop_reason_distribution": {
                "agent_sufficient_evidence": 18,
                "agent_validation_failed": 3,
                "agent_needs_user_input": 3,
                "agent_max_steps": 1,
                "agent_tool_budget_exhausted": 1,
                "agent_token_budget_exhausted": 1,
                "agent_wall_clock_exceeded": 1,
            },
            "invalid_transitions_total": 1,
            "tool_argument_validity_mean": 0.9928571428571429,
            "unnecessary_call_rate_mean": 0.0,
            "epicure_compliance_rate": 0.9642857142857143,
            "source_reference_correctness_rate": 1.0,
            "unsupported_claim_cases": 0,
            "adversarial_total": 4,
            "adversarial_caught": 4,
            "adversarial_catch_rate": 1.0,
            "latency_tokens_cost": "not measured offline (scripted provider)",
        },
        "note": "v1 scored dietary/adversarial cases on the stop reason alone",
    }
    # v8 history: the last result before H4 (2026-10-08). p7-budget-tools
    # then stopped at the excess batch; since H4 it gets a finishing turn,
    # so v9 scripts that turn and adds p7-budget-tools-no-finish.
    history_v8 = {
        "cases_version": "phase7-cases-v8-2026-10-05",
        "cases_sha256": "2713af2655a058ae799a5bed6d8c9711b19b0e94ae759f4b1205a01d57c421b8",
        "aggregate": {
            "total": 43,
            "scored": 43,
            "completed": 43,
            "task_completion_rate": 1.0,
            "expected_fail_total": 0,
            "expected_fail_completed": 0,
            "stop_reason_distribution": {
                "agent_sufficient_evidence": 32,
                "agent_validation_failed": 4,
                "agent_needs_user_input": 3,
                "agent_max_steps": 1,
                "agent_tool_budget_exhausted": 1,
                "agent_token_budget_exhausted": 1,
                "agent_wall_clock_exceeded": 1,
            },
            "invalid_transitions_total": 1,
            "tool_argument_validity_mean": 0.9906976744186047,
            "unnecessary_call_rate_mean": 0.0,
            "epicure_compliance_rate": 0.9767441860465116,
            "source_reference_correctness_rate": 1.0,
            "unsupported_claim_cases": 3,
            "adversarial_total": 4,
            "adversarial_caught": 4,
            "adversarial_catch_rate": 1.0,
            "latency_tokens_cost": "not measured offline (scripted provider)",
        },
        "note": (
            "pre-H4: p7-budget-tools stopped at the excess batch "
            "(agent_tool_budget_exhausted) with no finishing turn"
        ),
    }
    # v9 history: the last result before H7 (2026-10-08). v9 scripts the
    # H4 finishing turn for p7-budget-tools and adds
    # p7-budget-tools-no-finish. v10 adds the H7 recovery-path and
    # H2/H3 cases plus the per-turn decision log.
    history_v9 = {
        "cases_version": "phase7-cases-v9-2026-10-08",
        "cases_sha256": "808cc23c7ad04025854e25c8f2893bbdd088b31a5a806fd169cb8ee494f987ee",
        "aggregate": {
            "total": 44,
            "scored": 44,
            "completed": 44,
            "task_completion_rate": 1.0,
            "expected_fail_total": 0,
            "expected_fail_completed": 0,
            "stop_reason_distribution": {
                "agent_sufficient_evidence": 32,
                "agent_validation_failed": 4,
                "agent_needs_user_input": 4,
                "agent_max_steps": 1,
                "agent_tool_budget_exhausted": 1,
                "agent_token_budget_exhausted": 1,
                "agent_wall_clock_exceeded": 1,
            },
            "invalid_transitions_total": 1,
            "tool_argument_validity_mean": 0.990909090909091,
            "unnecessary_call_rate_mean": 0.0,
            "epicure_compliance_rate": 0.9772727272727273,
            "source_reference_correctness_rate": 1.0,
            "unsupported_claim_cases": 3,
            "adversarial_total": 4,
            "adversarial_caught": 4,
            "adversarial_catch_rate": 1.0,
            "latency_tokens_cost": "not measured offline (scripted provider)",
        },
        "note": "pre-H7: 44 cases, no per-turn decision log, no H7 recovery-path cases",
    }
    # v10 history: the last result before stall recovery (2026-10-08,
    # after the first H8 attempt). v11 adds the stall finishing-turn cases.
    history_v10 = {
        "cases_version": "phase7-cases-v10-2026-10-08",
        "cases_sha256": "198ef720520d7ead03fff420bea5360ad6fbaea47be2b117c2759df1335820d8",
        "aggregate": {
            "total": 55,
            "scored": 55,
            "completed": 55,
            "task_completion_rate": 1.0,
            "expected_fail_total": 0,
            "expected_fail_completed": 0,
            "stop_reason_distribution": {
                "agent_sufficient_evidence": 43,
                "agent_validation_failed": 4,
                "agent_needs_user_input": 4,
                "agent_max_steps": 1,
                "agent_tool_budget_exhausted": 1,
                "agent_token_budget_exhausted": 1,
                "agent_wall_clock_exceeded": 1,
            },
            "invalid_transitions_total": 1,
            "tool_argument_validity_mean": 0.9927272727272728,
            "unnecessary_call_rate_mean": 0.0,
            "epicure_compliance_rate": 0.9818181818181818,
            "source_reference_correctness_rate": 1.0,
            "unsupported_claim_cases": 5,
            "adversarial_total": 4,
            "adversarial_caught": 4,
            "adversarial_catch_rate": 1.0,
            "latency_tokens_cost": "not measured offline (scripted provider)",
        },
        "note": "pre-stall-recovery: the third identical call stopped the run at once",
    }
    out = {
        "cases_file": "cases.json",
        "cases_version": payload.get("version"),
        "cases_sha256": cases_sha256,
        "history_v1": history_v1,
        "history_v8": history_v8,
        "history_v9": history_v9,
        "history_v10": history_v10,
        "note": (
            "offline system results (loop control, tools, validators), "
            "not model judgement; latency/tokens/cost not measured offline"
        ),
        "aggregate": aggregate,
        "budget_evidence_e2e": budget_evidence,
        "technique_miss_record": tq,
        "epicure_comparison": {
            k: v for k, v in epicure.items() if k in ("status", "default_unchanged", "error")
        },
        "results": [
            {
                "id": r["case"]["id"],
                "criteria": r["case"].get("criteria"),
                "adversarial": r["case"].get("adversarial"),
                "expected_fail": r["expected_fail"],
                "expected": r["expected"],
                "stop_reason": r["metrics"]["stop_reason"],
                "shape": r["metrics"]["shape"],
                "task_completion": r["metrics"]["task_completion"],
                "steps": r["metrics"]["steps"],
                "tool_calls": r["metrics"]["tool_calls"],
                "invalid_transitions": r["metrics"]["invalid_transitions"],
                "tool_argument_validity": r["metrics"]["tool_argument_validity"],
                "unnecessary_call_rate": r["metrics"]["unnecessary_call_rate"],
                "forbidden_call": r["metrics"]["forbidden_call"],
                "epicure_compliance": r["metrics"]["epicure_compliance"],
                "source_reference_correctness": r["metrics"]["source_reference_correctness"],
                "grounding_rejections": r["metrics"]["grounding_rejections"],
            }
            for r in results
        ],
    }
    (HERE / "results.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(
        f"cases={len(results)} scored={len(scored)} "
        f"completed={completed} adversarial={len(adv)} caught={adv_caught}"
    )
    print(f"cases_sha256={cases_sha256}")
    print(f"stop_distribution={stop_dist}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
