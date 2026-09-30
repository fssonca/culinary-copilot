#!/usr/bin/env python3
"""Phase 3 live runner (built and tested only; the live run is NOT authorized).

Drives the 8 frozen scenarios in ``live_scenarios.json`` against the
bounded agent loop with 2 attempts max per scenario phase, records a
spend ledger, snapshots, and grades — then reports first-attempt
outcomes separately from after-retry.

Safety (all enforced, all tested with fakes on disposable databases):

- refuses without ``--live --yes --ceiling-usd <= 0.15`` plus
  ``--expect-db-name/--expect-db-host``; ``--fake`` runs the whole
  pipeline against the fake provider on a disposable database;
- preflight refuses on: unknown model / pricing, ceiling breach,
  DB-guard mismatch, ``HF_HUB_OFFLINE != 1``, missing technique
  tables, or an unverifiable snapshot;
- every paid call (model turns, query embeddings, retries) is
  reserved before the call from a conservative local input bound
  (UTF-8 bytes/3 over the full payload, never below chars/4) plus
  maximum permitted output, reconciled with reported usage after,
  kept as spent on ambiguous failure, and refused when it does not
  fit the remaining cap;
- trial isolation: a fresh session per attempt (a retry never
  inherits answers or evidence); ask-and-resume stays inside one
  session with scripted answers frozen in the scenario file; the
  runner keeps a manifest of every session it created; pre/post
  snapshots prove no writes outside sessions/session_events.

Raw model responses and trajectories go under ``data/phase3-live/``
(git-ignored). The committed summary
(``evals/phase3_agent/live-summary.json``) is written only by a live
run (``--fake`` writes its summary under the raw dir instead).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

EVALS_DIR = REPO_ROOT / "evals" / "phase3_agent"
DEFAULT_SCENARIOS = EVALS_DIR / "live_scenarios.json"
DEFAULT_RAW_DIR = REPO_ROOT / "data" / "phase3-live"
LIVE_SUMMARY = EVALS_DIR / "live-summary.json"

LIVE_CAP_USD = 0.15
MAX_ATTEMPTS = 2


# --- input bound ---------------------------------------------------------------


def estimate_input_tokens(payload: Any) -> int:
    """Conservative local input bound: UTF-8 bytes/3, never below chars/4.

    The Responses input-token endpoint exists and the installed SDK
    exposes it, but the docs do not confirm it is unbilled — a counting
    call could itself cost money and break the ledger. So reservations
    use this deterministic offline bound over the full payload (input
    items, tools, schema). bytes/3 strictly dominates chars/4 for any
    UTF-8 text, hence the floor never binds, but it is kept as stated.
    """
    text = json.dumps(payload, sort_keys=True, default=str)
    encoded = text.encode("utf-8")
    return max(len(encoded) // 3, len(text) // 4)


# --- spend ledger --------------------------------------------------------------


class BudgetExhausted(RuntimeError):
    """A reservation refusal: ending the run cleanly, not a provider failure.

    Raised by the ledger wrappers when a call's reservation does not fit
    the remaining cap. run_agent maps it to an internal error terminal;
    run_scenario_live detects it through the exception chain and marks
    the scenario "not_completed: budget" (never counted as an agent or
    provider failure in the grades).
    """


class SpendLedger:
    """Run-level ledger: reserve before each paid call, reconcile after.

    Every entry is priced by its own model: chat turns at the chat-model
    rate, query embeddings at the embedding rate from
    ``embeddings/registry.py``. The entry records the model, its kind,
    and the pricing version used.
    """

    def __init__(self, *, model: str, ceiling_usd: float) -> None:
        self.model = model
        self.ceiling_usd = float(ceiling_usd)
        self.remaining_usd = float(ceiling_usd)
        self.spent_usd = 0.0
        self.entries: list[dict[str, Any]] = []

    def _cost(
        self, model: str, kind: str, input_tokens: int, output_tokens: int
    ) -> tuple[float | None, str | None]:
        if kind == "embedding":
            from culinary_copilot.embeddings.registry import (
                EMBED_PRICING_VERSION,
            )
            from culinary_copilot.embeddings.registry import (
                estimate_cost_usd as estimate_embed_cost_usd,
            )

            return (
                estimate_embed_cost_usd(int(input_tokens) + int(output_tokens), model),
                EMBED_PRICING_VERSION,
            )
        from culinary_copilot.llm.models import PRICING_VERSION
        from culinary_copilot.recommendations.pricing import estimate_cost_usd

        return estimate_cost_usd(int(input_tokens), int(output_tokens), model), PRICING_VERSION

    def reserve(
        self,
        label: str,
        *,
        input_tokens: int,
        max_output: int,
        model: str | None = None,
        kind: str = "chat",
    ) -> bool:
        """Reserve input + maximum output. False (no state change) when unaffordable."""
        priced_model = model or self.model
        cost, pricing_version = self._cost(priced_model, kind, int(input_tokens), int(max_output))
        if cost is None:
            return False
        if cost > self.remaining_usd:
            self.entries.append(
                {
                    "label": label,
                    "decision": "refused",
                    "reserved_usd": cost,
                    "model": priced_model,
                    "kind": kind,
                    "pricing_version": pricing_version,
                }
            )
            return False
        self.remaining_usd -= cost
        self.entries.append(
            {
                "label": label,
                "decision": "reserved",
                "reserved_in": int(input_tokens),
                "reserved_out": int(max_output),
                "reserved_usd": cost,
                "model": priced_model,
                "kind": kind,
                "pricing_version": pricing_version,
            }
        )
        return True

    def reconcile(self, label: str, *, reported_in: int | None, reported_out: int | None) -> None:
        """Replace the reservation with reported usage (release the rest)."""
        for entry in reversed(self.entries):
            if entry.get("label") == label and entry.get("decision") == "reserved":
                if reported_in is None or reported_out is None:
                    entry["decision"] = "kept-ambiguous"
                    self.spent_usd += float(entry["reserved_usd"])
                    return
                actual, _ = self._cost(
                    str(entry.get("model") or self.model),
                    str(entry.get("kind") or "chat"),
                    int(reported_in),
                    int(reported_out),
                )
                actual = actual or 0.0
                entry["decision"] = "reconciled"
                entry["used_in"] = int(reported_in)
                entry["used_out"] = int(reported_out)
                entry["used_usd"] = actual
                self.remaining_usd += float(entry["reserved_usd"]) - actual
                self.spent_usd += actual
                return
        raise ValueError(f"no open reservation for {label!r}")

    def keep(self, label: str) -> None:
        """Ambiguous failure: the reservation stays spent."""
        for entry in reversed(self.entries):
            if entry.get("label") == label and entry.get("decision") == "reserved":
                entry["decision"] = "kept-ambiguous"
                self.spent_usd += float(entry["reserved_usd"])
                return
        raise ValueError(f"no open reservation for {label!r}")

    def summary(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "ceiling_usd": self.ceiling_usd,
            "spent_usd": self.spent_usd,
            "remaining_usd": self.remaining_usd,
            "entries": list(self.entries),
        }


# --- snapshots -----------------------------------------------------------------


def snapshot(engine: Any) -> dict[str, Any]:
    """Isolation snapshot: counts plus a checksum over technique hashes."""
    from sqlalchemy import text

    with engine.connect() as conn:
        recipes = conn.execute(text("SELECT count(*) FROM recipes")).scalar()
        quarantine = conn.execute(text("SELECT count(*) FROM recipe_quarantine")).scalar()
        docs = conn.execute(text("SELECT count(*) FROM technique_documents")).scalar()
        chunks = conn.execute(text("SELECT count(*) FROM technique_chunks")).scalar()
        hashes = [
            r[0]
            for r in conn.execute(
                text("SELECT sha256_normalized FROM technique_documents ORDER BY doc_id")
            )
        ]
        sessions = conn.execute(text("SELECT count(*) FROM sessions")).scalar()
        events = conn.execute(text("SELECT count(*) FROM session_events")).scalar()
        event_keys = [
            (r[0], r[1]) for r in conn.execute(text("SELECT session_id, seq FROM session_events"))
        ]
    checksum = hashlib.sha256("\n".join(hashes).encode()).hexdigest()
    return {
        "recipes": int(recipes or 0),
        "quarantine": int(quarantine or 0),
        "technique_documents": int(docs or 0),
        "technique_chunks": int(chunks or 0),
        "technique_checksum": checksum,
        "sessions": int(sessions or 0),
        "session_events": int(events or 0),
        "event_keys": [[s, q] for s, q in event_keys],
    }


def verify_isolation(
    pre: dict[str, Any], post: dict[str, Any], created_ids: set[str]
) -> tuple[bool, list[str]]:
    """Prove no writes outside sessions/session_events by created sessions."""
    problems: list[str] = []
    for key in ("recipes", "quarantine", "technique_documents", "technique_chunks"):
        if pre.get(key) != post.get(key):
            problems.append(f"{key} changed ({pre.get(key)} -> {post.get(key)})")
    if pre.get("technique_checksum") != post.get("technique_checksum"):
        problems.append("technique checksum changed")
    if int(post.get("sessions", 0)) - int(pre.get("sessions", 0)) != len(created_ids):
        problems.append("sessions growth does not match the created-session manifest")
    pre_keys = {tuple(k) for k in pre.get("event_keys", [])}
    for key in post.get("event_keys", []):
        if tuple(key) not in pre_keys and key[0] not in created_ids:
            problems.append(f"event outside created sessions: {key}")
            break
    return (not problems, problems)


# --- scenarios -----------------------------------------------------------------


def load_scenarios(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    body = {k: v for k, v in payload.items() if k != "freeze_sha256"}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if digest != payload.get("freeze_sha256"):
        raise ValueError("live scenarios freeze hash mismatch")
    return payload


# --- provider wrappers ---------------------------------------------------------


class LedgerModelProvider:
    """Wraps a model provider: reserve per turn, reconcile with usage."""

    def __init__(
        self, inner: Any, ledger: SpendLedger, *, max_output: int, model: str | None = None
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._max_output = int(max_output)
        self._model = model
        self._seq = 0

    async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
        payload = {
            "input_items": kwargs.get("input_items"),
            "tools": kwargs.get("tools"),
            "response_model": str(kwargs.get("response_model")),
        }
        self._seq += 1
        label = f"model-turn-{self._seq}"
        reserved_in = estimate_input_tokens(payload)
        # The call's own cap wins when the loop passes one; otherwise the
        # configured per-turn maximum.
        max_out = kwargs.get("max_output_tokens", self._max_output)
        try:
            max_out = int(max_out)
        except (TypeError, ValueError):
            max_out = self._max_output
        model = self._model or self._ledger.model
        if not self._ledger.reserve(
            label, input_tokens=reserved_in, max_output=max_out, model=model, kind="chat"
        ):
            raise BudgetExhausted(
                f"overrun guard: turn reservation exceeds remaining "
                f"${self._ledger.remaining_usd:.4f}"
            )
        try:
            result = await self._inner.complete_native_tool_turn(**kwargs)
        except Exception as exc:
            if bool(getattr(exc, "request_sent", True)):
                self._ledger.keep(label)
            else:
                # Nothing was sent: release the reservation.
                for entry in reversed(self._ledger.entries):
                    if entry.get("label") == label and entry.get("decision") == "reserved":
                        entry["decision"] = "released-unsent"
                        self._ledger.remaining_usd += float(entry["reserved_usd"])
                        break
            raise
        self._ledger.reconcile(
            label,
            reported_in=getattr(result, "input_tokens", None),
            reported_out=getattr(result, "output_tokens", None),
        )
        return result


class LedgerEmbedProvider:
    """Wraps an embedding provider: reserve per call, reconcile with usage.

    Exactly one request per wrapped call: the runner forces
    ``embed_max_retries=0`` on the effective settings, so retries happen
    only as runner-level scenario attempts, each reserved separately.
    """

    def __init__(
        self, inner: Any, ledger: SpendLedger, *, retries: int, model: str | None = None
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._retries = max(0, int(retries))
        self._model = model or getattr(inner, "model", None) or "text-embedding-3-small"
        self._seq = 0

    async def embed_texts(self, texts: list[str]) -> Any:
        from culinary_copilot.embeddings.rendering import estimate_tokens_bytes

        self._seq += 1
        label = f"query-embed-{self._seq}"
        reserved = sum(estimate_tokens_bytes(t) for t in texts) * (self._retries + 1)
        if not self._ledger.reserve(
            label, input_tokens=reserved, max_output=0, model=self._model, kind="embedding"
        ):
            raise BudgetExhausted(
                f"overrun guard: embedding reservation exceeds remaining "
                f"${self._ledger.remaining_usd:.4f}"
            )
        try:
            result = await self._inner.embed_texts(texts)
        except Exception:
            self._ledger.keep(label)
            raise
        self._ledger.reconcile(
            label,
            reported_in=int(getattr(result.usage, "prompt_tokens", 0) or 0),
            reported_out=0,
        )
        return result


# --- preflight -----------------------------------------------------------------


def _effective_settings(settings: Any) -> Any:
    """Runner settings copy: provider-internal retries forced to zero.

    One wrapped call then equals exactly one billed request (model turns
    via ``llm_app_max_retries``, embeddings via ``embed_max_retries``);
    retries happen only as runner-level scenario attempts, each reserved
    separately. Never edits .env; the copy lives for the run only.
    """
    return settings.model_copy(update={"llm_app_max_retries": 0, "embed_max_retries": 0})


def preflight(args: Any, settings: Any) -> tuple[bool, list[str], dict[str, Any]]:
    """Refuse (False) on any failed check; records versions and corpus state."""
    problems: list[str] = []
    record: dict[str, Any] = {}
    from culinary_copilot.llm.models import PRICING_VERSION, model_spec

    model = str(args.model or settings.llm_rec_model)
    spec = model_spec(model)
    if spec is None:
        problems.append(f"unknown model {model!r} (not in registry)")
    record["model"] = model
    record["pricing_version"] = PRICING_VERSION
    if int(getattr(settings, "llm_app_max_retries", 0)) != 0:
        problems.append(
            "llm_app_max_retries must be 0 (one wrapped call must equal one request; "
            "retries are runner-level attempts only)"
        )
    if int(getattr(settings, "embed_max_retries", 0)) != 0:
        problems.append(
            "embed_max_retries must be 0 (one wrapped call must equal one request; "
            "retries are runner-level attempts only)"
        )
    if float(args.ceiling_usd) > LIVE_CAP_USD:
        problems.append(f"ceiling ${float(args.ceiling_usd):.2f} exceeds ${LIVE_CAP_USD:.2f} cap")
    if os.environ.get("HF_HUB_OFFLINE") != "1":
        problems.append("HF_HUB_OFFLINE != 1 (Epicure assets must fail fast, never download)")
    db_url = args.database_url or settings.database_url.get_secret_value()
    from urllib.parse import urlparse

    parts = urlparse(db_url)
    name = parts.path.rsplit("/", 1)[-1]
    host = parts.hostname or ""
    if name != args.expect_db_name or host != args.expect_db_host:
        problems.append(
            f"DB guard mismatch: {host}/{name} != {args.expect_db_host}/{args.expect_db_name}"
        )
    record["technique"] = {}
    if bool(getattr(settings, "embeddings_enabled", False)):
        # Embeddings on: the query-embedding provider must build, or the
        # vector calls the agent may choose have no funding path.
        try:
            from culinary_copilot.tools.search_tools import build_embed_provider

            if build_embed_provider(settings) is None:
                problems.append("EMBEDDINGS_ENABLED but no query-embedding provider could be built")
        except Exception as exc:
            problems.append(
                "EMBEDDINGS_ENABLED but the query-embedding provider failed: "
                f"{type(exc).__name__}: {exc}"
            )
    if not problems:
        try:
            from sqlalchemy import create_engine

            engine = create_engine(db_url)
            snap = snapshot(engine)
            engine.dispose()
            manifest_path = REPO_ROOT / "evals" / "technique_corpus" / "manifest.json"
            manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            record["technique"] = {
                "manifest_sha256": manifest_hash,
                "documents": snap["technique_documents"],
                "chunks": snap["technique_chunks"],
                "checksum": snap["technique_checksum"],
            }
            from sqlalchemy import create_engine as _ce

            engine2 = _ce(db_url)
            with engine2.connect() as conn:
                from sqlalchemy import text

                embeddings = conn.execute(
                    text("SELECT count(*) FROM technique_embeddings")
                ).scalar()
                try:
                    recipe_embeddings = conn.execute(
                        text("SELECT count(*) FROM recipe_embeddings")
                    ).scalar()
                except Exception:
                    recipe_embeddings = 0
            engine2.dispose()
            record["technique"]["embeddings"] = int(embeddings or 0)
            record["vector"] = {
                "recipe_embeddings": int(recipe_embeddings or 0),
                "technique_embeddings": int(embeddings or 0),
            }
        except Exception as exc:
            problems.append(f"technique snapshot unverifiable: {type(exc).__name__} (use option B)")
    return (not problems, problems, record)


# --- grading -------------------------------------------------------------------


def grade_attempt(
    scenario: dict[str, Any],
    final: dict[str, Any] | None,
    stop_reason: str,
    store: Any,
    session_id: str,
) -> dict[str, Any]:
    """Structural grades (deterministic; relevance stays owner review)."""
    from culinary_copilot.agent.loop import recipe_session_evidence

    expected = scenario.get("expected", {})
    grades: dict[str, Any] = {}
    options = list((final or {}).get("options") or [])
    if not options and (final or {}).get("plan"):
        # Plan flows finish on the plan; judge the persisted recommendations.
        try:
            committed = store.get(session_id)
            options = list((committed.suggestions if committed else []) or [])
        except Exception:
            options = []
    grades["task_completion"] = stop_reason == expected.get("stop_reason") and (
        len(options) >= int(expected.get("min_options", 0))
    )
    grades["termination"] = stop_reason == expected.get("stop_reason")
    hard = set((scenario.get("session", {}).get("constraints") or {}).keys())
    honored = set((final or {}).get("constraints_honored") or [])
    grades["constraint_adherence"] = not hard or hard <= honored
    retrieved, _full = recipe_session_evidence(store=store, session_id=session_id)
    grades["evidence_support"] = all(
        isinstance(o, dict) and (str(o.get("dataset_id")), str(o.get("source_id"))) in retrieved
        for o in options
    )
    question = (final or {}).get("question") or {}
    if expected.get("stop_reason") == "agent_needs_user_input":
        grades["clarification_quality"] = bool(question.get("question_text")) and bool(
            question.get("options")
        )
        contains = expected.get("question_contains")
        if contains:
            grades["clarification_quality"] = grades["clarification_quality"] and (
                contains in str(question.get("question_text") or "")
            )
    else:
        grades["clarification_quality"] = question == {}
    epicure_expected = str(expected.get("epicure", "consulted"))
    if epicure_expected == "consulted":
        grades["epicure_behaviour"] = not bool(
            (final or {}).get("epicure_skip_reason")
        ) and not bool((final or {}).get("epicure_degraded"))
    elif epicure_expected == "degraded":
        grades["epicure_behaviour"] = bool((final or {}).get("epicure_degraded"))
    elif epicure_expected.startswith("skip:"):
        grades["epicure_behaviour"] = (final or {}).get("epicure_skip_reason") == epicure_expected[
            len("skip:") :
        ]
    else:
        grades["epicure_behaviour"] = False
    grades["request_relevance"] = {
        "verdict": "manual-review",
        "request": scenario.get("request"),
        "option_titles": [str(o.get("title")) for o in options if isinstance(o, dict)],
    }
    return grades


# --- fake stack (--fake only) ----------------------------------------------------


_FAKE_ROWS = [
    {"dataset_id": "odunola/foodie", "source_id": "curry-1", "title": "Creamy Chicken Curry"},
    {"dataset_id": "odunola/foodie", "source_id": "lentil-2", "title": "Red Lentil Soup"},
]
_FAKE_DOCS = {
    ("odunola/foodie", "curry-1"): {
        "dataset_id": "odunola/foodie",
        "source_id": "curry-1",
        "title": "Creamy Chicken Curry",
        "servings": 4.0,
        "ingredients": [
            {"canonical": "chicken", "amount": "500", "unit": "g", "quantity_text": "500 g"}
        ],
    },
    ("odunola/foodie", "lentil-2"): {
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
            }
        ],
    },
}


class FakeRunProvider:
    """Deterministic fake model: retrieval turns then a valid finish (tests only).

    Per-flow scripts (calls count turns across resume runs in one attempt):
    full: tools, options finish, then plan finishes; ask: tools, ask,
    tools, options finish; direct: tools, single finish; empty: tools,
    ask; technique: tools, skip finish.
    """

    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        self.calls = 0

    def _tools(self, *calls: tuple[str, str, dict[str, Any]]) -> Any:
        from culinary_copilot.llm.client import NativeToolCall, NativeTurnResult

        return NativeTurnResult(
            tool_calls=[
                NativeToolCall(call_id=c, name=n, arguments=json.dumps(a)) for c, n, a in calls
            ],
            parsed=None,
            chain_items=[
                {
                    "type": "function_call",
                    "call_id": c,
                    "name": n,
                    "arguments": json.dumps(a),
                }
                for c, n, a in calls
            ],
            input_tokens=10,
            output_tokens=5,
        )

    def _parsed(self, payload: dict[str, Any]) -> Any:
        from culinary_copilot.llm.client import NativeTurnResult

        return NativeTurnResult(
            tool_calls=[],
            parsed=payload,
            chain_items=[],
            input_tokens=10,
            output_tokens=5,
        )

    def _options_finish(self, options: list[dict[str, Any]], **kw: Any) -> Any:
        return self._parsed(
            {
                "decision": "finish",
                "move_to": "recommend",
                "result": {"options": options},
                "constraints_honored": list(
                    (self.scenario.get("session", {}).get("constraints") or {}).keys()
                ),
                "note": "fake finish",
                **kw,
            }
        )

    def _opt(self, source_id: str, title: str, quantities: list[dict[str, Any]]) -> dict[str, Any]:
        dataset = "odunola/foodie"
        return {
            "dataset_id": dataset,
            "source_id": source_id,
            "title": title,
            "quantities": quantities,
            "adaptations": [],
        }

    async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
        self.calls += 1
        flow = self.scenario.get("fake_flow", "direct")
        query = {"query": self.scenario["request"][:60]}
        if flow == "full":
            if self.calls == 1:
                return self._tools(
                    ("c1", "search_recipes", query),
                    ("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
                    ("c3", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
                    ("c4", "find_balanced_pairings", {"ingredient": "chicken"}),
                )
            if self.calls == 2:
                return self._options_finish(
                    [
                        self._opt(
                            "curry-1",
                            "Creamy Chicken Curry",
                            [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
                        ),
                        self._opt(
                            "lentil-2",
                            "Red Lentil Soup",
                            [{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
                        ),
                    ]
                )
            return self._parsed(
                {
                    "decision": "finish",
                    "move_to": "plan",
                    "result": {
                        "plan": {
                            "source": {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
                            "mise_en_place": ["dice chicken"],
                            "steps": ["brown chicken", "serve"],
                            "plating": "in bowls",
                            "quantities": [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
                            "adaptations": [],
                        }
                    },
                    "constraints_honored": [],
                    "note": "fake plan",
                }
            )
        if flow == "ask":
            if self.calls == 1:
                return self._tools(("c1", "search_recipes", query))
            if self.calls == 2:
                scripted = list(self.scenario.get("scripted_answers", []))
                question_id = scripted[0]["question_id"] if scripted else "q-fake"
                return self._parsed(
                    {
                        "decision": "ask_user",
                        "question": {
                            "question_id": question_id,
                            "question_text": "Do you have plain yogurt?",
                            "options": ["yes", "no"],
                        },
                        "note": "fake ask",
                    }
                )
            if self.calls == 3:
                return self._tools(
                    ("c3", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
                    ("c4", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
                    ("c5", "find_substitutions", {"ingredient": "yogurt"}),
                )
            return self._options_finish(
                [
                    self._opt(
                        "curry-1",
                        "Creamy Chicken Curry",
                        [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
                    ),
                    self._opt(
                        "lentil-2",
                        "Red Lentil Soup",
                        [{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
                    ),
                ]
            )
        if flow == "empty":
            if self.calls == 1:
                return self._tools(("c1", "search_recipes", query))
            return self._parsed(
                {
                    "decision": "ask_user",
                    "question": {
                        "question_id": "q-fake",
                        "question_text": (
                            "I found no recipes for dragonfruit soufflé glacé. "
                            "Want me to look for a lemon dessert instead?"
                        ),
                        "options": ["yes, look", "no"],
                    },
                    "note": "fake ask",
                }
            )
        if flow == "technique":
            if self.calls == 1:
                return self._tools(
                    ("c1", "search_recipes", query),
                    ("c2", "search_techniques", query),
                )
            return self._options_finish(
                [
                    self._opt("curry-1", "Creamy Chicken Curry", []),
                    self._opt("lentil-2", "Red Lentil Soup", []),
                ],
                epicure_skip_reason="simple_technique_question",
            )
        if flow == "degraded":
            if self.calls == 1:
                return self._tools(
                    ("c1", "search_recipes", query),
                    ("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"}),
                    ("c3", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
                    ("c4", "find_balanced_pairings", {"ingredient": "chicken"}),
                )
            return self._options_finish(
                [
                    self._opt(
                        "curry-1",
                        "Creamy Chicken Curry",
                        [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
                    ),
                    self._opt(
                        "lentil-2",
                        "Red Lentil Soup",
                        [{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
                    ),
                ]
            )
        if self.calls == 1:
            return self._tools(
                ("c1", "search_recipes", query),
                ("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
                ("c3", "find_balanced_pairings", {"ingredient": "lentils"}),
            )
        return self._options_finish(
            [
                self._opt(
                    "lentil-2",
                    "Red Lentil Soup",
                    [{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
                )
            ]
        )


def _fake_search(rows: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    def _run(args: Any, context: Any) -> dict[str, Any]:
        return {"ok": True, "mode_ran": "fulltext", "cost_class": "free", "results": list(rows)}

    return _run


def _fake_get(args: Any, context: Any) -> dict[str, Any]:
    doc = _FAKE_DOCS.get((args.dataset_id, args.source_id))
    if doc is None:
        return {
            "ok": False,
            "error_type": "invalid_arguments",
            "reason": "tool_invalid_arguments",
            "message": "not found",
            "next_action": "change_request",
        }
    return {"ok": True, "recipe": dict(doc)}


def _fake_techniques(args: Any, context: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "mode_ran": "fulltext",
        "match": "all",
        "cost_class": "free",
        "results": [
            {
                "doc_id": "tech-egg-boil-18",
                "chunk_id": 0,
                "section": "Boiled egg",
                "title": "Boiled egg",
                "url": "https://en.wikipedia.org/wiki/Boiled_egg",
                "licence": "CC-BY-SA-4.0",
                "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
                "attribution_text": "fake attribution",
                "excerpt": "Boil eggs.",
            }
        ],
    }


class _FakeEpicureCore:
    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled

    def find_balanced_pairings(self, ingredient: str, k: int = 5):  # type: ignore[no-untyped-def]
        from culinary_copilot.tools.epicure import EpicureDisabledError, Pairing

        if not self._enabled:
            raise EpicureDisabledError("disabled")
        return [Pairing(ingredient="pork", score=0.5)][:k]


# --- run driver ------------------------------------------------------------------


def _new_session_id(scenario_key: str, attempt: int) -> str:
    return f"ses-live-{scenario_key[:12]}-a{attempt}-{uuid.uuid4().hex[:6]}"


def _caused_by_budget(exc: BaseException) -> bool:
    """True when BudgetExhausted is anywhere in the exception chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, BudgetExhausted):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _classify_loop_error(exc: Any) -> str | None:
    """Fatal run stops: provider-auth and contact-operator terminals."""
    if getattr(exc, "reason", None) == "provider_auth":
        return "provider-auth"
    if getattr(exc, "next_action", None) == "contact_operator":
        return "contact-operator"
    return None


def run_scenario_live(
    *,
    engine: Any,
    store: Any,
    settings: Any,
    scenario: dict[str, Any],
    ledger: SpendLedger,
    provider_factory: Any,
    context_factory: Any,
    raw_dir: Path,
    max_attempts: int = MAX_ATTEMPTS,
    recipe_resolver: Any = None,
) -> dict[str, Any]:
    from culinary_copilot.agent.loop import (
        AgentDeps,
        AgentLoopError,
        record_answer,
        record_select,
        run_agent,
    )

    created: list[str] = []
    attempts: list[dict[str, Any]] = []
    flow = list(scenario.get("flow", ["recommend"]))
    last_final: dict[str, Any] | None = None
    last_stop = ""
    run_stop: dict[str, str] | None = None
    for attempt in range(1, max_attempts + 1):
        sid = _new_session_id(scenario["key"], attempt)
        created.append(sid)
        session_seed = dict(scenario.get("session", {}))
        from culinary_copilot.domain.sessions import SessionState

        store.create(SessionState(id=sid, **session_seed))
        deps = AgentDeps(
            settings=settings,
            session_store=store,
            provider=provider_factory(scenario),
            tool_context=context_factory(store, scenario),
            recipe_resolver=recipe_resolver,
            request_text=scenario.get("request"),
        )
        attempt_record: dict[str, Any] = {"attempt": attempt, "session_id": sid, "runs": []}
        try:
            result = asyncio.run(run_agent(sid, deps=deps))
            attempt_record["runs"].append(
                {"stop_reason": result.stop_reason, "phase": result.phase, "final": result.final}
            )
            last_final, last_stop = result.final, result.stop_reason
            if result.stop_reason == "agent_needs_user_input" and "resume" in flow:
                answers = list(scenario.get("scripted_answers", []))
                if answers:
                    current = store.get(sid)
                    record_answer(
                        store,
                        sid,
                        expected_revision=current.revision,
                        question_id=answers[0]["question_id"],
                        answer=answers[0]["answer"],
                    )
                    result2 = asyncio.run(run_agent(sid, deps=deps))
                    attempt_record["runs"].append(
                        {
                            "stop_reason": result2.stop_reason,
                            "phase": result2.phase,
                            "final": result2.final,
                        }
                    )
                    last_final, last_stop = result2.final, result2.stop_reason
            if "select-first" in flow and (last_final or {}).get("options"):
                current = store.get(sid)
                first = last_final["options"][0]
                record_select(
                    store,
                    sid,
                    expected_revision=current.revision,
                    dataset_id=first["dataset_id"],
                    source_id=first["source_id"],
                )
                if "plan" in flow:
                    result3 = asyncio.run(run_agent(sid, deps=deps))
                    attempt_record["runs"].append(
                        {
                            "stop_reason": result3.stop_reason,
                            "phase": result3.phase,
                            "final": result3.final,
                        }
                    )
                    last_final, last_stop = result3.final, result3.stop_reason
        except AgentLoopError as exc:
            if _caused_by_budget(exc):
                # Reservation refusal: end the scenario cleanly with no
                # further attempt. Never an agent or provider failure.
                attempt_record["runs"].append(
                    {"stop_reason": "budget-exhausted", "budget_stop": True}
                )
                last_final, last_stop = None, "budget-exhausted"
                run_stop = {"reason": "budget"}
                attempts.append(attempt_record)
                break
            fatal = _classify_loop_error(exc)
            attempt_record["runs"].append(
                {
                    "stop_reason": exc.reason,
                    "error": True,
                    "http_status": exc.http_status,
                    "message": exc.message,
                }
            )
            last_final, last_stop = None, exc.reason
            if fatal is not None:
                run_stop = {"reason": fatal}
        except Exception as exc:
            # Preflight-class failure mid-run (snapshot, store, driver):
            # stop the run, do not grade the wreckage.
            attempt_record["runs"].append(
                {"stop_reason": "runner-error", "error": True, "message": str(exc)[:200]}
            )
            last_final, last_stop = None, "runner-error"
            run_stop = {"reason": "runner-error"}
            attempts.append(attempt_record)
            break
        attempts.append(attempt_record)
        if run_stop is not None:
            break
        if last_stop == (scenario.get("expected", {}) or {}).get("stop_reason") and last_final:
            break
    if run_stop is not None and run_stop.get("reason") == "budget":
        grades: dict[str, Any] = {"graded": False, "reason": "budget-exhausted"}
    else:
        grades = grade_attempt(scenario, last_final, last_stop, store, created[-1])
    return {
        "key": scenario["key"],
        "first_attempt": attempts[0] if attempts else None,
        "final_attempt": attempts[-1] if attempts else None,
        "attempts": len(attempts),
        "stop_reason": last_stop,
        "status": "not_completed: budget" if last_stop == "budget-exhausted" else "completed",
        "run_stop": run_stop,
        "grades": grades,
        "sessions": created,
    }


# --- CLI -------------------------------------------------------------------------


def _args(argv: list[str] | None = None) -> Any:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS))
    parser.add_argument("--database-url", default="")
    parser.add_argument("--expect-db-name", default="")
    parser.add_argument("--expect-db-host", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    parser.add_argument("--ceiling-usd", type=float, default=None)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--raw-dir", default="")
    parser.add_argument("--summary-out", default="")
    return parser.parse_args(argv)


def _require_disposable_db(url: str) -> str:
    from urllib.parse import urlparse

    name = urlparse(url).path.rsplit("/", 1)[-1]
    lowered = url.lower()
    if name == "culinary_copilot" and ("localhost:5432" in lowered or "127.0.0.1:5432" in lowered):
        return "refusing --fake on the application database"
    if "test" not in name and "disposable" not in name and "check" not in name:
        return f"refusing --fake on database {name!r}"
    return ""


def _ensure_disposable_db(db_url: str) -> None:
    """Drop + create the disposable database (fake runs and tests only)."""
    from urllib.parse import urlparse

    from sqlalchemy import create_engine, text

    parts = urlparse(db_url)
    name = parts.path.rsplit("/", 1)[-1]
    head = db_url.rsplit("/", 1)[0]
    maint = create_engine(f"{head}/postgres", isolation_level="AUTOCOMMIT")
    with maint.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{name}' AND pid <> pg_backend_pid()"
            )
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
        with create_engine(db_url).begin() as migration_conn:
            from culinary_copilot.recipes import import_data

            import_data.apply_migrations(migration_conn)
    maint.dispose()


def _drop_disposable_db(db_url: str) -> None:
    from urllib.parse import urlparse

    from sqlalchemy import create_engine, text

    parts = urlparse(db_url)
    name = parts.path.rsplit("/", 1)[-1]
    head = db_url.rsplit("/", 1)[0]
    maint = create_engine(f"{head}/postgres", isolation_level="AUTOCOMMIT")
    with maint.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{name}' AND pid <> pg_backend_pid()"
            )
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    maint.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    from culinary_copilot.config import Settings

    scenarios = load_scenarios(Path(args.scenarios))
    if args.fake:
        settings = Settings(_env_file=None)
        db_url = args.database_url or settings.database_url.get_secret_value()
        guard = _require_disposable_db(db_url)
        if guard:
            print(f"error: {guard}", file=sys.stderr)
            return 2
        raw_dir = Path(args.raw_dir) if args.raw_dir else Path(str(DEFAULT_RAW_DIR) + "-fake")
        summary_out = Path(args.summary_out) if args.summary_out else raw_dir / "summary.json"
        _ensure_disposable_db(db_url)
        try:
            return _run_all(args, settings, scenarios, db_url, raw_dir, summary_out, fake=True)
        finally:
            _drop_disposable_db(db_url)
    if not (args.live and args.yes and args.ceiling_usd is not None):
        print(
            "error: refusing without --live --yes --ceiling-usd (live run not "
            "authorized); --fake runs the full pipeline on a disposable DB.",
            file=sys.stderr,
        )
        return 2
    if float(args.ceiling_usd) > LIVE_CAP_USD:
        print(f"error: ceiling exceeds ${LIVE_CAP_USD:.2f} cap", file=sys.stderr)
        return 2
    if not args.expect_db_name or not args.expect_db_host:
        print("error: --expect-db-name and --expect-db-host are required", file=sys.stderr)
        return 2
    settings = _effective_settings(Settings())
    ok, problems, record = preflight(args, settings)
    if not ok:
        for problem in problems:
            print(f"preflight: {problem}", file=sys.stderr)
        return 2
    db_url = args.database_url or settings.database_url.get_secret_value()
    raw_dir = Path(args.raw_dir) if args.raw_dir else DEFAULT_RAW_DIR
    summary_out = Path(args.summary_out) if args.summary_out else LIVE_SUMMARY
    result = _run_all(args, settings, scenarios, db_url, raw_dir, summary_out, fake=False)
    print(json.dumps({"preflight": record}, indent=2))
    return result


def _run_all(
    args: Any,
    settings: Any,
    scenarios: dict[str, Any],
    db_url: str,
    raw_dir: Path,
    summary_out: Path,
    *,
    fake: bool,
) -> int:
    from sqlalchemy import create_engine

    from culinary_copilot.services.session_store import PostgresSessionStore

    engine = create_engine(db_url)
    store = PostgresSessionStore(engine)
    model = str(args.model or settings.llm_rec_model)
    ledger = SpendLedger(model=model, ceiling_usd=float(args.ceiling_usd or 0.0))
    max_output = int(settings.llm_rec_max_output_tokens)
    effective = _effective_settings(settings)
    pre = snapshot(engine)
    created_all: list[str] = []
    scenario_reports: list[dict[str, Any]] = []
    raw_dir.mkdir(parents=True, exist_ok=True)
    streak = 0
    stopped_early: dict[str, str] | None = None

    if not fake:
        _provider_factory = _live_provider_factory(effective, ledger, max_output)
        _context_factory = _live_context_factory(effective, engine, ledger)

    for scenario in scenarios["scenarios"]:
        if fake:
            report = _run_fake_scenario(
                engine, store, settings, scenario, ledger, raw_dir, args.max_attempts
            )
        else:
            scenario_settings = _scenario_settings(effective, scenario)
            report = run_scenario_live(
                engine=engine,
                store=store,
                settings=scenario_settings,
                scenario=scenario,
                ledger=ledger,
                provider_factory=_provider_factory,
                context_factory=_context_factory,
                raw_dir=raw_dir,
                max_attempts=args.max_attempts,
                recipe_resolver=None,
            )
        created_all.extend(report["sessions"])
        scenario_reports.append(report)
        (raw_dir / f"{scenario['key']}.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        if report.get("run_stop") is not None:
            stopped_early = {
                "reason": str(report["run_stop"].get("reason")),
                "after_scenario": scenario["key"],
            }
            break
        if report.get("stop_reason") in ("agent_no_progress", "agent_validation_failed"):
            streak += 1
            if streak >= 2:
                stopped_early = {
                    "reason": "consecutive-failures",
                    "after_scenario": scenario["key"],
                }
                break
        else:
            streak = 0

    if stopped_early is not None:
        done_keys = {r["key"] for r in scenario_reports}
        for scenario in scenarios["scenarios"]:
            if scenario["key"] not in done_keys:
                scenario_reports.append(
                    {"key": scenario["key"], "status": f"not_run: {stopped_early['reason']}"}
                )

    post = snapshot(engine)
    isolated, isolation_problems = verify_isolation(pre, post, set(created_all))

    def _attempt_stop(attempt: Any) -> str | None:
        runs = (attempt or {}).get("runs") or []
        return runs[-1].get("stop_reason") if runs else None

    summary = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fake": fake,
        "scenarios_sha256": scenarios["freeze_sha256"],
        "model": model,
        "spend": ledger.summary(),
        "isolation": {"ok": isolated, "problems": isolation_problems},
        "stopped_early": stopped_early,
        "scenarios": [
            {
                "key": r["key"],
                "status": r.get("status", "completed"),
                "attempts": r.get("attempts", 0),
                "stop_reason": r.get("stop_reason"),
                "grades": r.get("grades", {"graded": False, "reason": "not-run"}),
                "first_attempt_stop": _attempt_stop(r.get("first_attempt")),
            }
            for r in scenario_reports
        ],
    }
    raw_dir.mkdir(parents=True, exist_ok=True)
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"scenarios: {len(scenario_reports)}; isolation ok: {isolated}")
    print(f"spent ${ledger.spent_usd:.4f} of ${ledger.ceiling_usd:.2f}")
    return 0 if isolated else 1


def _fake_context(current_store: Any, settings: Any, scenario: dict[str, Any] | None) -> Any:
    from culinary_copilot.tools.registry import ToolContext

    rows = [] if (scenario or {}).get("fake_flow") == "empty" else list(_FAKE_ROWS)
    core = _FakeEpicureCore(enabled=bool(settings.epicure_enabled))
    return ToolContext(
        settings=settings,
        engine=None,
        session_store=current_store,
        impl_overrides={
            "search_recipes": _fake_search(rows),
            "get_recipe": _fake_get,
            "search_techniques": _fake_techniques,
        },
        epicure_core=core,
        epicure_cooc=core,
        epicure_chem=core,
    )


def _scenario_settings(settings: Any, scenario: dict[str, Any]) -> Any:
    """Per-scenario settings: scenario overrides win, never the .env file.

    The Epicure-unavailable scenario sets ``epicure_enabled: false``
    here (configuration for that scenario only). Internal retries stay
    forced to zero (runner-level attempts only).
    """
    overrides = dict(scenario.get("settings", {}))
    if not overrides:
        return _effective_settings(settings)
    data = settings.model_dump()
    data.update(overrides)
    return _effective_settings(type(settings)(_env_file=None, **data))


def _live_provider_factory(settings: Any, ledger: SpendLedger, max_output: int) -> Any:
    """Model provider factory for live runs (module-level for tests)."""

    def _factory(scenario: dict[str, Any]) -> Any:
        from culinary_copilot.llm.client import OpenAIApplicationProvider

        return LedgerModelProvider(
            OpenAIApplicationProvider(settings), ledger, max_output=max_output
        )

    return _factory


def _wrap_context_embed_provider(ctx: Any, ledger: SpendLedger, settings: Any) -> Any:
    """Install the ledger-wrapped query-embedding provider on one context.

    The single shared ``embed_provider`` serves both ``search_recipes``
    and ``search_techniques`` (each reads it from the tool context), so
    wrapping it once covers both tools' query embeddings.
    """
    if getattr(ctx, "embed_provider", None) is not None:
        ctx.embed_provider = LedgerEmbedProvider(
            ctx.embed_provider, ledger, retries=int(getattr(settings, "embed_max_retries", 0) or 0)
        )
    return ctx


def _live_context_factory(settings: Any, engine: Any, ledger: SpendLedger) -> Any:
    """Tool-context factory for live runs (module-level for tests)."""

    def _factory(current_store: Any, scenario: dict[str, Any]) -> Any:
        from culinary_copilot.tools import build_tool_context

        return _wrap_context_embed_provider(
            build_tool_context(settings, engine, current_store), ledger, settings
        )

    return _factory


def _run_fake_scenario(
    engine: Any,
    store: Any,
    settings: Any,
    scenario: dict[str, Any],
    ledger: SpendLedger,
    raw_dir: Path,
    max_attempts: int,
) -> dict[str, Any]:
    from culinary_copilot.config import Settings

    scenario_settings = Settings(
        _env_file=None,
        epicure_enabled=bool(scenario.get("settings", {}).get("epicure_enabled", True)),
    )

    def _provider_factory(current: dict[str, Any]) -> Any:
        return FakeRunProvider(current)

    def _context_factory(current_store: Any, current: dict[str, Any]) -> Any:
        return _fake_context(current_store, scenario_settings, current)

    def _recipe_resolver(dataset_id: str, source_id: str) -> dict[str, Any] | None:
        doc = _FAKE_DOCS.get((dataset_id, source_id))
        return dict(doc) if doc is not None else None

    return run_scenario_live(
        engine=engine,
        store=store,
        settings=scenario_settings,
        scenario=scenario,
        ledger=ledger,
        provider_factory=_provider_factory,
        context_factory=_context_factory,
        raw_dir=raw_dir,
        max_attempts=max_attempts,
        recipe_resolver=_recipe_resolver,
    )


if __name__ == "__main__":
    raise SystemExit(main())
