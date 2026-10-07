"""Phase 3 live-runner tests (fakes only, disposable databases).

Covers: CLI refusal paths, spend-ledger reservation math (model turns
plus embeddings plus retries), stop-before-overrun, ambiguous-failure
accounting, usage reconciliation, trial isolation (fresh session per
attempt, no foreign evidence, snapshot verification), frozen-scenario
hashing, first-attempt vs after-retry reporting, and a full --fake run
end to end. Skipped when PostgreSQL is unreachable.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals" / "phase3_agent"))

import live_run
from live_run import (
    SpendLedger,
    estimate_embedding_request_tokens,
    estimate_request_tokens,
    load_scenarios,
    verify_isolation,
)

TEST_DB = "culinary_test_live"


def _urls() -> tuple[str, str]:
    from culinary_copilot.config import Settings

    base = Settings().database_url.get_secret_value()
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
        from culinary_copilot.recipes import import_data

        with eng.begin() as conn:
            import_data.apply_migrations(conn)
        yield eng
        eng.dispose()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL unavailable for live-runner tests: {exc!r}")
    finally:
        try:
            maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
            with maint.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            maint.dispose()
        except Exception:
            pass


def _ledger(model: str = "gpt-6-luna", ceiling: float = 0.15) -> SpendLedger:
    return SpendLedger(model=model, ceiling_usd=ceiling)


def _default_summary_bytes() -> bytes | None:
    """Raw bytes of the committed default summary path (None when absent).

    Test runs must leave it untouched: a pre-existing attempt file stays
    byte-identical, and none is created when absent.
    """
    path = Path(__file__).resolve().parents[1] / "evals" / "phase3_agent" / "live-summary.json"
    try:
        return path.read_bytes()
    except OSError:
        return None


# --- refusals ----------------------------------------------------------------------


def test_refuses_without_live_flags() -> None:
    assert live_run.main([]) == 2
    assert live_run.main(["--live"]) == 2
    assert live_run.main(["--live", "--yes"]) == 2


def test_refuses_ceiling_above_cap() -> None:
    assert (
        live_run.main(
            [
                "--live",
                "--yes",
                "--ceiling-usd",
                "0.16",
                "--expect-db-name",
                "x",
                "--expect-db-host",
                "y",
            ]
        )
        == 2
    )


def test_refuses_without_db_guards() -> None:
    assert live_run.main(["--live", "--yes", "--ceiling-usd", "0.15"]) == 2


def test_refuses_fake_on_app_db() -> None:
    assert (
        live_run.main(
            [
                "--fake",
                "--database-url",
                "postgresql+psycopg://copilot:pw@localhost:5432/culinary_copilot",
            ]
        )
        == 2
    )


def test_preflight_refuses_without_hf_offline(
    engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    _, test_url = _urls()
    rc = live_run.main(
        [
            "--live",
            "--yes",
            "--ceiling-usd",
            "0.15",
            "--expect-db-name",
            TEST_DB,
            "--expect-db-host",
            "localhost",
            "--database-url",
            test_url,
            "--raw-dir",
            str(tmp_path),
        ]
    )
    assert rc == 2


# --- ledger math -------------------------------------------------------------------


def _turn5_shaped_payload() -> dict[str, Any]:
    """Synthetic attempt-7-turn-5-shaped request: tool history plus tools.

    Sized so the old bytes/3 bound lands near the observed 2203 while
    the provider reported 2452 input tokens (1.11x over reservation).
    """
    history = [
        {
            "type": "function_call",
            "call_id": f"c{i}",
            "name": "search_recipes",
            "arguments": json.dumps({"query": "roast chicken " + "x" * 40}),
        }
        for i in range(3)
    ]
    history += [
        {
            "type": "function_call_output",
            "call_id": f"c{i}",
            "output": json.dumps(
                {
                    "tool": "search_recipes",
                    "ok": True,
                    "results": [{"title": "Roast Chicken " + "y" * 60}],
                }
            ),
        }
        for i in range(3)
    ]
    filler = "z" * 4200
    return {
        "input_items": [
            {"role": "user", "content": "roast chicken request"},
            *history,
            {"role": "user", "content": "framing snapshot " + filler},
        ],
        "tools": [
            {
                "type": "function",
                "name": f"tool_{i}",
                "description": "d" * 30,
                "parameters": {"type": "object"},
            }
            for i in range(12)
        ],
    }


def test_request_bound_is_byte_length_plus_overhead() -> None:
    from openai.lib._parsing._responses import type_to_text_format_param

    from culinary_copilot.agent.loop import AgentDirective

    payload = _turn5_shaped_payload()
    bound = estimate_request_tokens(
        input_items=payload["input_items"],
        tools=payload["tools"],
        response_model=AgentDirective,
    )
    raw = json.dumps(
        {
            "input_items": payload["input_items"],
            "tools": payload["tools"],
            "text_format": type_to_text_format_param(AgentDirective),
            "tool_choice": None,
            "max_output_tokens": None,
        },
        sort_keys=True,
        default=str,
    )
    assert (
        bound
        == len(raw.encode("utf-8"))
        + live_run._RESERVE_PER_ITEM_TOKENS * len(payload["input_items"])
        + live_run._RESERVE_REQUEST_OVERHEAD_TOKENS
    )
    assert bound >= len(raw.encode("utf-8"))


def test_request_bound_covers_converted_schema() -> None:
    """The bound for AgentDirective covers the strict-converted
    text.format payload the SDK actually sends (larger than the plain
    JSON schema)."""
    import json as _json

    from openai.lib._parsing._responses import type_to_text_format_param

    from culinary_copilot.agent.loop import AgentDirective

    converted = _json.dumps(type_to_text_format_param(AgentDirective), sort_keys=True, default=str)
    bound = estimate_request_tokens(input_items=[], tools=[], response_model=AgentDirective)
    assert bound >= len(converted.encode("utf-8"))
    plain = _json.dumps(AgentDirective.model_json_schema(), sort_keys=True, default=str)
    assert len(converted.encode("utf-8")) > len(plain.encode("utf-8"))


def test_request_bound_covers_all_turn_shapes() -> None:
    from culinary_copilot.agent.loop import AgentDirective

    first = estimate_request_tokens(
        input_items=[
            {"role": "user", "content": "request"},
            {"role": "user", "content": "framing"},
        ],
        tools=[{"type": "function", "name": "search_recipes"}],
        response_model=AgentDirective,
    )
    history_turn = estimate_request_tokens(
        input_items=_turn5_shaped_payload()["input_items"],
        tools=_turn5_shaped_payload()["tools"],
        response_model=AgentDirective,
    )
    final_turn = estimate_request_tokens(
        input_items=[{"role": "user", "content": "request"}],
        tools=[],
        response_model=AgentDirective,
    )
    for bound in (first, history_turn, final_turn):
        assert bound > 0
    assert history_turn > first > final_turn
    # Regression on attempts 6-9: the old bound reserved 2203 for a
    # turn-5-shaped request while the provider reported 2452. The new
    # bound covers the reported usage.
    assert history_turn >= 2452


def test_large_tool_and_schema_payloads_covered_by_bound() -> None:
    from culinary_copilot.agent.loop import AgentDirective

    small = estimate_request_tokens(input_items=[], tools=[], response_model=None)
    big_tools = [
        {
            "type": "function",
            "name": f"tool_{i}",
            "description": "d" * 500,
            "parameters": {
                "type": "object",
                "properties": {f"p{j}": {"type": "string"} for j in range(20)},
            },
        }
        for i in range(12)
    ]
    big = estimate_request_tokens(input_items=[], tools=big_tools, response_model=AgentDirective)
    assert big > small
    raw = json.dumps(
        {
            "input_items": [],
            "tools": big_tools,
            "text_format": AgentDirective.model_json_schema(),
            "tool_choice": None,
            "max_output_tokens": None,
        },
        sort_keys=True,
        default=str,
    )
    assert big >= len(raw.encode("utf-8"))


def test_embedding_bound_is_byte_length_plus_overhead() -> None:
    texts = ["roast chicken query", "lentil soup query"]
    bound = estimate_embedding_request_tokens(texts)
    raw = json.dumps({"texts": texts}, sort_keys=True, default=str)
    assert bound == len(raw.encode("utf-8")) + live_run._RESERVE_PER_ITEM_TOKENS * 2 + (
        live_run._RESERVE_REQUEST_OVERHEAD_TOKENS
    )
    assert estimate_embedding_request_tokens(texts, attempts=3) == bound * 3


def test_reconcile_excess_input_raises_breach() -> None:
    ledger = _ledger()
    assert ledger.reserve("t1", input_tokens=1000, max_output=500) is True
    with pytest.raises(live_run.ReservationBreach):
        ledger.reconcile("t1", reported_in=1001, reported_out=50)
    entry = [e for e in ledger.entries if e.get("label") == "t1"][0]
    assert entry["decision"] == "reservation_breach"
    assert entry["used_in"] == 1001 and entry["used_out"] == 50
    assert entry["used_usd"] > 0.0
    assert "input 1001 > reserved 1000" in entry["breach"]


def test_reconcile_excess_output_raises_breach() -> None:
    ledger = _ledger()
    assert ledger.reserve("t1", input_tokens=1000, max_output=500) is True
    with pytest.raises(live_run.ReservationBreach):
        ledger.reconcile("t1", reported_in=100, reported_out=501)
    entry = [e for e in ledger.entries if e.get("label") == "t1"][0]
    assert entry["decision"] == "reservation_breach"
    assert "output 501 > reserved 500" in entry["breach"]


def test_wrapper_breach_on_excess_usage() -> None:
    inner = _ScriptedModel([{"in": 10**9, "out": 5}])
    ledger = _ledger()
    wrapped = live_run.LedgerModelProvider(inner, ledger, max_output=100)
    with pytest.raises(live_run.ReservationBreach):
        asyncio.run(
            wrapped.complete_native_tool_turn(input_items=[], tools=[], response_model=None)
        )
    assert ledger.entries[-1]["decision"] == "reservation_breach"


def test_embed_wrapper_breach_on_excess_usage() -> None:
    class _ScriptedEmbed:
        async def embed_texts(self, texts: list[str]) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10**9))

    ledger = _ledger()
    wrapped = live_run.LedgerEmbedProvider(_ScriptedEmbed(), ledger, retries=0)
    with pytest.raises(live_run.ReservationBreach):
        asyncio.run(wrapped.embed_texts(["hi"]))
    assert ledger.entries[-1]["decision"] == "reservation_breach"


def test_breach_stop_marks_contact_operator(engine, tmp_path: Path) -> None:
    from culinary_copilot.config import Settings
    from culinary_copilot.services.session_store import PostgresSessionStore

    settings = Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    ledger = _ledger()
    breach = live_run.ReservationBreach("reservation breach on model-turn-1", label="model-turn-1")
    report = live_run.run_scenario_live(
        engine=engine,
        store=store,
        settings=settings,
        scenario=_live_scenario(),
        ledger=ledger,
        provider_factory=lambda s: _raising_provider(breach),
        context_factory=lambda s, sc: live_run._fake_context(s, settings, sc),
        raw_dir=tmp_path,
        max_attempts=2,
        recipe_resolver=lambda ds, sid: dict(live_run._FAKE_DOCS.get((ds, sid)) or {}) or None,
    )
    assert report["status"] == "stopped: contact-operator"
    assert report["attempts"] == 1  # no further calls after the breach
    assert report["run_stop"] is not None and report["run_stop"]["reason"] == "contact-operator"
    assert report["grades"] == {"graded": False, "reason": "reservation-breach"}
    assert report["stop_reason"] == "reservation-breach"


def test_preflight_refuses_unacknowledged_breach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import argparse
    import json as _json

    from culinary_copilot.config import Settings

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    history = tmp_path / "spend-history.json"
    history.write_text(
        _json.dumps(
            {
                "ceiling_usd": 0.15,
                "runs": [
                    {
                        "run_utc": "2026-09-30T12:00:00Z",
                        "attempt": 9,
                        "model": "gpt-6-luna",
                        "entries": [
                            {
                                "label": "model-turn-5",
                                "decision": "reservation_breach",
                                "usd": 0.001,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert live_run.recorded_spend_total(history) == pytest.approx(0.001)
    assert live_run.find_unacknowledged_breach(history) == {
        "run_utc": "2026-09-30T12:00:00Z",
        "label": "model-turn-5",
    }
    args = argparse.Namespace(
        model="",
        ceiling_usd=0.15,
        expect_db_name="culinary_test_live",
        expect_db_host="localhost",
        database_url="",
    )
    settings = live_run._effective_settings(
        Settings(_env_file=None, llm_recommendation_enabled=True)
    )
    ok, problems, _record = live_run.preflight(args, settings, history_path=history)
    assert ok is False
    assert any("unacknowledged reservation breach" in p for p in problems)


def test_preflight_accepts_acknowledged_breach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json as _json

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    history = tmp_path / "spend-history.json"
    history.write_text(
        _json.dumps(
            {
                "ceiling_usd": 0.15,
                "breach_acknowledgments": [
                    {
                        "run_utc": "2026-09-30T12:00:00Z",
                        "label": "model-turn-5",
                        "by": "owner",
                    }
                ],
                "runs": [
                    {
                        "run_utc": "2026-09-30T12:00:00Z",
                        "attempt": 9,
                        "model": "gpt-6-luna",
                        "entries": [
                            {
                                "label": "model-turn-5",
                                "decision": "reservation_breach",
                                "usd": 0.001,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert live_run.find_unacknowledged_breach(history) is None


def test_reserve_reconcile_release() -> None:
    ledger = _ledger()
    assert ledger.reserve("t1", input_tokens=1000, max_output=500) is True
    reserved = ledger.remaining_usd
    ledger.reconcile("t1", reported_in=100, reported_out=50)
    assert ledger.remaining_usd > reserved  # unused reservation released
    assert ledger.spent_usd > 0.0
    entry = [e for e in ledger.entries if e.get("label") == "t1"][0]
    assert entry["decision"] == "reconciled"
    assert entry["used_in"] == 100 and entry["used_out"] == 50


def test_stop_before_overrun() -> None:
    ledger = _ledger(ceiling=0.000001)
    assert ledger.reserve("t1", input_tokens=10**9, max_output=10**9) is False
    assert ledger.entries[-1]["decision"] == "refused"


def test_ambiguous_failure_keeps_reservation() -> None:
    ledger = _ledger()
    assert ledger.reserve("t1", input_tokens=1000, max_output=500) is True
    before = ledger.remaining_usd
    ledger.keep("t1")
    assert ledger.remaining_usd == before
    assert ledger.spent_usd > 0.0


def test_unknown_model_has_no_cost() -> None:
    ledger = SpendLedger(model="no-such-model", ceiling_usd=1.0)
    assert ledger.reserve("t1", input_tokens=100, max_output=100) is False


# --- provider wrappers ---------------------------------------------------------------


class _ScriptedModel:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)

    async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
        from culinary_copilot.llm.client import NativeTurnResult

        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return NativeTurnResult(
            tool_calls=[],
            parsed=None,
            chain_items=[],
            input_tokens=item["in"],
            output_tokens=item["out"],
        )


def test_ledger_model_provider_reconciles() -> None:
    inner = _ScriptedModel([{"in": 11, "out": 7}])
    ledger = _ledger()
    wrapped = live_run.LedgerModelProvider(inner, ledger, max_output=100)
    result = asyncio.run(
        wrapped.complete_native_tool_turn(input_items=[], tools=[], response_model=None)
    )
    assert (result.input_tokens, result.output_tokens) == (11, 7)
    assert ledger.entries[-1]["decision"] == "reconciled"
    assert ledger.entries[-1]["used_in"] == 11


def test_ledger_model_provider_overrun_raises() -> None:
    inner = _ScriptedModel([{"in": 1, "out": 1}])
    ledger = _ledger(ceiling=0.0000001)
    wrapped = live_run.LedgerModelProvider(inner, ledger, max_output=10**9)
    with pytest.raises(Exception):
        asyncio.run(
            wrapped.complete_native_tool_turn(
                input_items=[{"x": "y" * 5000}], tools=[], response_model=None
            )
        )
    assert inner.script  # inner never called


def test_ledger_model_provider_ambiguous_keeps() -> None:
    from culinary_copilot.llm.client import ProviderTimeoutError

    inner = _ScriptedModel([ProviderTimeoutError("t", request_sent=True)])
    ledger = _ledger()
    before = ledger.remaining_usd
    wrapped = live_run.LedgerModelProvider(inner, ledger, max_output=100)
    with pytest.raises(ProviderTimeoutError):
        asyncio.run(
            wrapped.complete_native_tool_turn(input_items=[], tools=[], response_model=None)
        )
    assert ledger.entries[-1]["decision"] == "kept-ambiguous"
    assert ledger.remaining_usd < before


def test_ledger_model_provider_unsent_releases() -> None:
    from culinary_copilot.llm.client import ProviderTimeoutError

    inner = _ScriptedModel([ProviderTimeoutError("t", request_sent=False)])
    ledger = _ledger()
    before = ledger.remaining_usd
    wrapped = live_run.LedgerModelProvider(inner, ledger, max_output=100)
    with pytest.raises(ProviderTimeoutError):
        asyncio.run(
            wrapped.complete_native_tool_turn(input_items=[], tools=[], response_model=None)
        )
    assert ledger.entries[-1]["decision"] == "released-unsent"
    assert ledger.remaining_usd == before


def test_ledger_embed_provider_reserve_reconcile() -> None:
    from culinary_copilot.embeddings.provider import EmbeddingResult, EmbeddingUsage

    class _Inner:
        async def embed_texts(self, texts: list[str]) -> Any:
            return EmbeddingResult(
                vectors=[[0.0] * 1536 for _ in texts],
                model="text-embedding-3-small",
                dimension=1536,
                usage=EmbeddingUsage(prompt_tokens=3, total_tokens=3),
            )

    ledger = _ledger()
    wrapped = live_run.LedgerEmbedProvider(_Inner(), ledger, retries=1)
    result = asyncio.run(wrapped.embed_texts(["hello world, this is a query"]))
    assert len(result.vectors) == 1
    assert ledger.entries[-1]["decision"] == "reconciled"
    assert ledger.entries[-1]["used_in"] == 3


# --- frozen scenarios ----------------------------------------------------------------


def test_scenarios_hash(tmp_path: Path) -> None:
    payload = load_scenarios(
        Path(__file__).resolve().parents[1] / "evals" / "phase3_agent" / "live_scenarios.json"
    )
    assert len(payload["scenarios"]) == 8
    tampered = dict(payload)
    tampered["scenarios"] = [dict(s) for s in payload["scenarios"]]
    tampered["scenarios"][0] = dict(tampered["scenarios"][0], request="tampered")
    name = tmp_path / "tampered.json"
    name.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError):
        load_scenarios(name)


# --- isolation -----------------------------------------------------------------------


def test_snapshot_verify_clean_and_tamper(engine) -> None:
    pre = live_run.snapshot(engine)
    post = live_run.snapshot(engine)
    ok, problems = verify_isolation(pre, post, set())
    assert ok and problems == []
    tampered = dict(post, recipes=int(post["recipes"]) + 1)
    ok, problems = verify_isolation(pre, tampered, set())
    assert not ok and any("recipes" in p for p in problems)
    tampered = dict(post, technique_checksum="0" * 64)
    ok, problems = verify_isolation(pre, tampered, set())
    assert not ok and any("checksum" in p for p in problems)


def test_foreign_session_growth_detected(engine) -> None:
    from culinary_copilot.domain.sessions import SessionState
    from culinary_copilot.services.session_store import PostgresSessionStore

    _, test_url = _urls()
    eng = create_engine(test_url)
    try:
        store = PostgresSessionStore(eng)
        pre = live_run.snapshot(engine)
        store.create(SessionState(id="ses-foreign-growth"))
        post = live_run.snapshot(engine)
        ok, problems = verify_isolation(pre, post, {"ses-someone-else"})
        assert not ok
        assert any("sessions" in p for p in problems)
    finally:
        eng.dispose()


def test_foreign_session_evidence_does_not_count(engine) -> None:
    from culinary_copilot.agent.loop import recipe_session_evidence
    from culinary_copilot.config import Settings
    from culinary_copilot.domain.sessions import SessionState
    from culinary_copilot.services.session_store import PostgresSessionStore
    from culinary_copilot.tools import all_tool_definitions, all_tool_impls, run_tool
    from culinary_copilot.tools.registry import ToolContext

    _, test_url = _urls()
    eng = create_engine(test_url)
    store = PostgresSessionStore(eng)
    store.create(SessionState(id="ses-foreign-a"))
    store.create(SessionState(id="ses-foreign-b"))

    async def _call(session_id: str) -> None:
        defs = {d.name: d for d in all_tool_definitions(timeout_s=10.0)}
        impls = all_tool_impls()

        def _search(args: Any, context: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "mode_ran": "fulltext",
                "cost_class": "free",
                "results": [{"dataset_id": "odunola/foodie", "source_id": "curry-1", "title": "x"}],
            }

        ctx = ToolContext(
            settings=Settings(_env_file=None),
            engine=None,
            session_store=store,
            impl_overrides={"search_recipes": _search},
        )
        await run_tool(
            defs["search_recipes"],
            impls["search_recipes"],
            {"query": "q"},
            ctx,
            session_id=session_id,
        )

    asyncio.run(_call("ses-foreign-a"))
    retrieved_b, _ = recipe_session_evidence(store=store, session_id="ses-foreign-b")
    assert retrieved_b == set()
    retrieved_a, _ = recipe_session_evidence(store=store, session_id="ses-foreign-a")
    assert ("odunola/foodie", "curry-1") in retrieved_a
    eng.dispose()


# --- full fake run -------------------------------------------------------------------


def test_full_fake_run_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    # The end-to-end fake run exercises the Epicure-consulted path, so
    # it opts into Epicure explicitly (a disabled environment fails the
    # pairing flows offline instead; see
    # test_fake_path_uses_effective_epicure_settings).
    monkeypatch.setenv("EPICURE_ENABLED", "true")
    default_summary_before = _default_summary_bytes()
    from culinary_copilot.config import Settings

    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    # A dedicated disposable DB: the --fake run drops and recreates it,
    # which must not disturb the shared module fixture database.
    db_url = f"{head}/culinary_test_live_run"
    raw_dir = tmp_path / "raw"
    summary_out = tmp_path / "summary.json"
    rc = live_run.main(
        [
            "--fake",
            "--database-url",
            db_url,
            "--raw-dir",
            str(raw_dir),
            "--summary-out",
            str(summary_out),
        ]
    )
    assert rc == 0
    summary = json.loads(summary_out.read_text(encoding="utf-8"))
    assert len(summary["scenarios"]) == 8
    assert summary["isolation"]["ok"] is True
    assert summary["spend"]["spent_usd"] == 0.0
    # The run must not touch the committed default summary path: a
    # pre-existing attempt file stays byte-identical, and none is
    # created when absent.
    assert _default_summary_bytes() == default_summary_before
    raws = sorted(raw_dir.glob("*.json"))
    assert len(raws) == 8


def test_first_attempt_vs_after_retry_reported(engine, tmp_path: Path) -> None:
    from live_run import _FAKE_DOCS, run_scenario_live

    from culinary_copilot.config import Settings
    from culinary_copilot.services.session_store import PostgresSessionStore

    settings = Settings(_env_file=None, epicure_enabled=True)
    store = PostgresSessionStore(engine)
    scenario = {
        "key": "retry-probe",
        "title": "retry probe",
        "request": "Give me the red lentil soup recipe.",
        "session": {},
        "settings": {},
        "scripted_answers": [],
        "flow": ["recommend"],
        "fake_flow": "direct",
        "expected": {"stop_reason": "agent_sufficient_evidence", "min_options": 1},
    }
    # Simpler deterministic shape: attempt 1 finishes unretrieved (fails),
    # attempt 2 retrieves then finishes valid (succeeds).
    from culinary_copilot.llm.client import NativeToolCall, NativeTurnResult

    class _Flaky2:
        def __init__(self, bad: bool) -> None:
            self.bad = bad
            self.turns = 0

        def _bad_finish(self) -> Any:
            return NativeTurnResult(
                tool_calls=[],
                parsed={
                    "decision": "finish",
                    "move_to": "recommend",
                    "result": {
                        "options": [
                            {
                                "dataset_id": "odunola/foodie",
                                "source_id": "unretrieved-9",
                                "title": "Ghost",
                                "quantities": [],
                                "adaptations": [],
                            }
                        ]
                    },
                    "constraints_honored": [],
                    "note": "bad attempt",
                },
                chain_items=[],
                input_tokens=5,
                output_tokens=5,
            )

        async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
            self.turns += 1
            if self.bad:
                return self._bad_finish()
            if self.turns == 1:
                calls = [
                    ("c1", "search_recipes", {"query": "lentil"}),
                    (
                        "c2",
                        "get_recipe",
                        {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
                    ),
                    ("c3", "find_balanced_pairings", {"ingredient": "lentils"}),
                ]
                return NativeTurnResult(
                    tool_calls=[
                        NativeToolCall(call_id=cid, name=name, arguments=json.dumps(args))
                        for cid, name, args in calls
                    ],
                    parsed=None,
                    chain_items=[
                        {
                            "type": "function_call",
                            "call_id": cid,
                            "name": name,
                            "arguments": json.dumps(args),
                        }
                        for cid, name, args in calls
                    ],
                    input_tokens=5,
                    output_tokens=5,
                )
            return NativeTurnResult(
                tool_calls=[],
                parsed={
                    "decision": "finish",
                    "move_to": "recommend",
                    "result": {
                        "options": [
                            {
                                "dataset_id": "odunola/foodie",
                                "source_id": "lentil-2",
                                "title": "Red Lentil Soup",
                                "quantities": [
                                    {"ingredient": "red lentils", "amount": "200", "unit": "g"}
                                ],
                                "adaptations": [],
                            }
                        ]
                    },
                    "epicure_lines": [
                        {"ingredient": "pork", "decision": "used", "reason": "fake match"}
                    ],
                    "constraints_honored": [],
                    "note": "good attempt",
                },
                chain_items=[],
                input_tokens=5,
                output_tokens=5,
            )

    made: list[Any] = []

    def _factory(current: dict[str, Any]) -> Any:
        provider = _Flaky2(bad=not made)
        made.append(provider)
        return provider

    ledger = SpendLedger(model="gpt-6-luna", ceiling_usd=0.15)
    report = run_scenario_live(
        engine=engine,
        store=store,
        settings=settings,
        scenario=scenario,
        ledger=ledger,
        provider_factory=_factory,
        context_factory=lambda s, _scenario: live_run._fake_context(s, settings, scenario),
        raw_dir=tmp_path,
        max_attempts=2,
        recipe_resolver=lambda ds, sid: dict(_FAKE_DOCS.get((ds, sid)) or {}) or None,
    )
    assert report["attempts"] == 2
    assert [s for s in report["sessions"][0:1]] != [s for s in report["sessions"][1:2]]
    first_stop = report["first_attempt"]["runs"][-1]["stop_reason"]
    assert first_stop == "agent_validation_failed"
    assert report["stop_reason"] == "agent_sufficient_evidence"
    assert report["grades"]["task_completion"] is True


# --- zero internal retries -----------------------------------------------------------


def test_effective_settings_force_zero_retries() -> None:
    from culinary_copilot.config import Settings

    settings = Settings(_env_file=None)
    assert int(settings.llm_app_max_retries) != 0
    assert int(settings.embed_max_retries) != 0
    effective = live_run._effective_settings(settings)
    assert int(effective.llm_app_max_retries) == 0
    assert int(effective.embed_max_retries) == 0


def test_preflight_refuses_nonzero_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from culinary_copilot.config import Settings

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    args = argparse.Namespace(
        model="", ceiling_usd=0.15, expect_db_name="x", expect_db_host="y", database_url=""
    )
    ok, problems, _ = live_run.preflight(args, Settings(_env_file=None))
    assert not ok
    assert any("llm_app_max_retries" in p for p in problems)
    assert any("embed_max_retries" in p for p in problems)
    ok, _, _ = live_run.preflight(args, live_run._effective_settings(Settings(_env_file=None)))
    assert not ok  # DB guard mismatch still refuses (no DB touched)


def test_wrapped_provider_receives_zeroed_settings() -> None:
    from culinary_copilot.config import Settings

    ledger = SpendLedger(model="gpt-6-luna", ceiling_usd=0.15)
    factory = live_run._live_provider_factory(
        live_run._effective_settings(Settings(_env_file=None)), ledger, 100
    )
    inner = factory({})._inner
    assert int(inner.settings.llm_app_max_retries) == 0


def test_max_output_kwarg_wins_over_configured() -> None:
    inner = _ScriptedModel([{"in": 11, "out": 7}, {"in": 11, "out": 7}])
    ledger = _ledger()
    wrapped = live_run.LedgerModelProvider(inner, ledger, max_output=6500)
    asyncio.run(wrapped.complete_native_tool_turn(input_items=[], tools=[], response_model=None))
    assert ledger.entries[-1]["reserved_out"] == 6500
    asyncio.run(
        wrapped.complete_native_tool_turn(
            input_items=[], tools=[], response_model=None, max_output_tokens=50
        )
    )
    assert ledger.entries[-1]["reserved_out"] == 50


# --- per-model pricing ---------------------------------------------------------------


def test_embedding_entries_use_embedding_rate() -> None:
    from culinary_copilot.embeddings.registry import EMBED_PRICING_VERSION

    ledger = _ledger()
    assert ledger.reserve(
        "q1",
        input_tokens=1_000_000,
        max_output=0,
        model="text-embedding-3-small",
        kind="embedding",
    )
    entry = ledger.entries[-1]
    assert entry["reserved_usd"] == pytest.approx(0.02)
    assert entry["pricing_version"] == EMBED_PRICING_VERSION
    ledger.reconcile("q1", reported_in=500_000, reported_out=0)
    assert entry["used_usd"] == pytest.approx(0.01)


def test_chat_entries_use_chat_rate_and_version() -> None:
    from culinary_copilot.llm.models import PRICING_VERSION

    ledger = _ledger()
    assert ledger.reserve("t1", input_tokens=1_000_000, max_output=0)
    entry = ledger.entries[-1]
    assert entry["model"] == "gpt-6-luna"
    assert entry["kind"] == "chat"
    assert entry["pricing_version"] == PRICING_VERSION
    assert entry["reserved_usd"] == pytest.approx(0.10)


# --- stop conditions -----------------------------------------------------------------


def _live_scenario(**overrides: Any) -> dict[str, Any]:
    scenario: dict[str, Any] = {
        "key": "stop-probe",
        "title": "stop probe",
        "request": "Give me the red lentil soup recipe.",
        "session": {},
        "settings": {},
        "scripted_answers": [],
        "flow": ["recommend"],
        "fake_flow": "direct",
        "expected": {"stop_reason": "agent_sufficient_evidence", "min_options": 1},
    }
    scenario.update(overrides)
    return scenario


def _raising_provider(exc: BaseException) -> Any:
    class _Raise:
        async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
            raise exc

    return _Raise()


def test_budget_stop_marks_not_completed(engine, tmp_path: Path) -> None:
    from culinary_copilot.config import Settings
    from culinary_copilot.services.session_store import PostgresSessionStore

    settings = Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    ledger = SpendLedger(model="gpt-6-luna", ceiling_usd=0.0000001)
    report = live_run.run_scenario_live(
        engine=engine,
        store=store,
        settings=settings,
        scenario=_live_scenario(),
        ledger=ledger,
        provider_factory=lambda s: live_run.LedgerModelProvider(
            _ScriptedModel([{"in": 5, "out": 5}]), ledger, max_output=100
        ),
        context_factory=lambda s, sc: live_run._fake_context(s, settings, sc),
        raw_dir=tmp_path,
        max_attempts=2,
        recipe_resolver=lambda ds, sid: dict(live_run._FAKE_DOCS.get((ds, sid)) or {}) or None,
    )
    assert report["status"] == "not_completed: budget"
    assert report["attempts"] == 1
    assert report["run_stop"] == {"reason": "budget"}
    assert report["grades"] == {"graded": False, "reason": "budget-exhausted"}
    assert report["stop_reason"] == "budget-exhausted"


def test_provider_auth_stops_run(engine, tmp_path: Path) -> None:
    from culinary_copilot.config import Settings
    from culinary_copilot.llm.client import ProviderAuthError
    from culinary_copilot.services.session_store import PostgresSessionStore

    settings = Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    ledger = _ledger()
    report = live_run.run_scenario_live(
        engine=engine,
        store=store,
        settings=settings,
        scenario=_live_scenario(),
        ledger=ledger,
        provider_factory=lambda s: _raising_provider(ProviderAuthError("bad key")),
        context_factory=lambda s, sc: live_run._fake_context(s, settings, sc),
        raw_dir=tmp_path,
        max_attempts=2,
        recipe_resolver=None,
    )
    assert report["run_stop"] == {"reason": "provider-auth"}
    assert report["stop_reason"] == "provider_auth"


def test_contact_operator_stops_run(engine, tmp_path: Path) -> None:
    from culinary_copilot.config import Settings
    from culinary_copilot.llm.client import ProviderBadRequestError
    from culinary_copilot.services.session_store import PostgresSessionStore

    settings = Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    ledger = _ledger()
    report = live_run.run_scenario_live(
        engine=engine,
        store=store,
        settings=settings,
        scenario=_live_scenario(),
        ledger=ledger,
        provider_factory=lambda s: _raising_provider(ProviderBadRequestError("bad")),
        context_factory=lambda s, sc: live_run._fake_context(s, settings, sc),
        raw_dir=tmp_path,
        max_attempts=2,
        recipe_resolver=None,
    )
    assert report["run_stop"] == {"reason": "contact-operator"}


def test_consecutive_failures_stop_run(
    engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from culinary_copilot.config import Settings

    def _failed(key: str) -> dict[str, Any]:
        return {
            "key": key,
            "status": "completed",
            "run_stop": None,
            "sessions": [],
            "attempts": 1,
            "stop_reason": "agent_validation_failed",
            "grades": {},
            "first_attempt": {"runs": [{"stop_reason": "agent_validation_failed"}]},
        }

    calls = {"n": 0}

    def _fake_run_scenario(**kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        return _failed(f"s{calls['n']}")

    monkeypatch.setattr(live_run, "run_scenario_live", _fake_run_scenario)
    args = argparse.Namespace(model="", max_attempts=2, ceiling_usd=0.15)
    settings = Settings(_env_file=None)
    _, test_url = _urls()

    eng = create_engine(test_url)
    try:
        rc = live_run._run_all(
            args,
            settings,
            {"freeze_sha256": "x", "scenarios": [{"key": "s1"}, {"key": "s2"}, {"key": "s3"}]},
            test_url,
            tmp_path,
            tmp_path / "summary.json",
            fake=False,
        )
    finally:
        eng.dispose()
    assert rc == 0
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["stopped_early"] == {"reason": "consecutive-failures", "after_scenario": "s2"}
    assert summary["scenarios"][2]["key"] == "s3"
    assert summary["scenarios"][2]["status"] == "not_run: consecutive-failures"


def test_budget_marks_remaining_not_run(
    engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from culinary_copilot.config import Settings

    def _budget(key: str) -> dict[str, Any]:
        return {
            "key": key,
            "status": "not_completed: budget",
            "run_stop": {"reason": "budget"},
            "sessions": [],
            "attempts": 1,
            "stop_reason": "budget-exhausted",
            "grades": {"graded": False, "reason": "budget-exhausted"},
            "first_attempt": {"runs": [{"stop_reason": "budget-exhausted"}]},
        }

    monkeypatch.setattr(live_run, "run_scenario_live", lambda **kwargs: _budget("s1"))
    args = argparse.Namespace(model="", max_attempts=2, ceiling_usd=0.15)
    settings = Settings(_env_file=None)
    _, test_url = _urls()

    eng = create_engine(test_url)
    try:
        live_run._run_all(
            args,
            settings,
            {"freeze_sha256": "x", "scenarios": [{"key": "s1"}, {"key": "s2"}]},
            test_url,
            tmp_path,
            tmp_path / "summary.json",
            fake=False,
        )
    finally:
        eng.dispose()
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["stopped_early"] == {"reason": "budget", "after_scenario": "s1"}
    assert summary["scenarios"][0]["status"] == "not_completed: budget"
    assert summary["scenarios"][1]["key"] == "s2"
    assert summary["scenarios"][1]["status"] == "not_run: budget"


# --- wrapped query embeddings for both retrieval tools --------------------------------


def test_both_tools_route_through_wrapped_provider() -> None:
    import asyncio as _asyncio

    from culinary_copilot.config import Settings
    from culinary_copilot.tools import all_tool_impls
    from culinary_copilot.tools.registry import ToolContext

    class _SentinelUsedError(RuntimeError):
        pass

    class _Sentinel:
        model = "text-embedding-3-small"

        async def embed_texts(self, texts: list[str]) -> Any:
            raise _SentinelUsedError("wrapped-provider-marker")

    settings = Settings(_env_file=None, embeddings_enabled=True)
    ledger = _ledger()
    impls = all_tool_impls()
    from culinary_copilot.tools.search_tools import SearchRecipesArgs
    from culinary_copilot.tools.technique_tools import SearchTechniquesArgs

    cases = (
        ("search_recipes", SearchRecipesArgs(query="chicken", mode="vector")),
        ("search_techniques", SearchTechniquesArgs(query="chicken", mode="vector")),
    )
    for tool_name, parsed in cases:
        ctx = ToolContext(settings=settings, engine=object())
        ctx.embed_provider = _Sentinel()  # type: ignore[assignment]
        live_run._wrap_context_embed_provider(ctx, ledger, live_run._effective_settings(settings))
        assert isinstance(ctx.embed_provider, live_run.LedgerEmbedProvider)
        result = _asyncio.run(impls[tool_name](parsed, ctx))
        assert result["ok"] is False
        assert "SentinelUsed" in result["message"]

    kinds = [e.get("kind") for e in ledger.entries if e.get("label", "").startswith("query-embed")]
    assert kinds == ["embedding", "embedding"]
    assert all(
        e["model"] == "text-embedding-3-small"
        for e in ledger.entries
        if e.get("kind") == "embedding"
    )


def test_preflight_refuses_unbuildable_embed_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    import culinary_copilot.tools.search_tools as _search_tools
    from culinary_copilot.config import Settings

    def _boom(settings: Any) -> Any:
        raise ValueError("no key in this environment")

    monkeypatch.setattr(_search_tools, "build_embed_provider", _boom)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    args = argparse.Namespace(
        model="",
        ceiling_usd=0.15,
        expect_db_name="culinary_test_live",
        expect_db_host="localhost",
        database_url="",
    )
    settings = Settings(_env_file=None, embeddings_enabled=True)
    ok, problems, _ = live_run.preflight(args, live_run._effective_settings(settings))
    assert not ok
    assert any("query-embedding provider" in p for p in problems)


# --- live path with only the SDK faked -------------------------------------------------
# Drives _run_all(..., fake=False) through the REAL live factories, provider,
# build_tool_context and tools on a disposable pgvector DB. Only the SDK
# clients are fake; a socket tripwire fails any real network access.


def _sdk_response(calls=None, parsed=None, in_tok=1000, out_tok=200):
    from types import SimpleNamespace

    items = []
    for i, (call_id, name, args) in enumerate(calls or []):
        items.append(
            SimpleNamespace(
                type="function_call",
                id=f"fc-{i}",
                call_id=call_id,
                name=name,
                arguments=json.dumps(args),
            )
        )
    return SimpleNamespace(
        id="resp-fake",
        status="completed",
        output=items,
        output_parsed=parsed,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


def _seed_livepath_db(engine) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO recipe_imports (id, dataset_id, revision, checksum, "
                "normalizer_version, vocabulary_checksum, dataset_url, report) "
                "VALUES ('live-test-1', 'odunola/foodie', 'r1', 'c', 'n', 'v', 'u', '{}')"
            )
        )
        for source_id, title, ingredient, amount, unit, search_text in (
            ("soup-1", "Simple Tomato Soup", "tomato", "400", "g", "tomato soup simple dinner"),
            ("curry-1", "Chicken Curry", "chicken", "500", "g", "chicken curry dinner"),
        ):
            conn.execute(
                text(
                    "INSERT INTO recipes (dataset_id, source_id, import_id, title, "
                    "servings, ingredient_names, document, search_text) "
                    "VALUES ('odunola/foodie', :sid, 'live-test-1', :title, 4.0, "
                    "ARRAY[:ingredient], "
                    "(:doc)::jsonb, :search_text)"
                ),
                {
                    "sid": source_id,
                    "title": title,
                    "ingredient": ingredient,
                    "doc": json.dumps(
                        {
                            "ingredients": [
                                {
                                    "canonical": ingredient,
                                    "amount": amount,
                                    "unit": unit,
                                    "quantity_text": f"{amount} {unit}",
                                }
                            ]
                        }
                    ),
                    "search_text": search_text,
                },
            )


def _livepath_scenarios():
    import hashlib as _hashlib

    scenarios = [
        {
            "key": "lp-recommend",
            "title": "recommend two",
            "request": "soup for dinner",
            "session": {"steps_remaining": 20, "tool_calls_remaining": 20},
            "settings": {},
            "scripted_answers": [],
            "flow": ["recommend"],
            "fake_flow": "direct",
            "expected": {"stop_reason": "agent_sufficient_evidence", "min_options": 2},
        },
        {
            "key": "lp-empty",
            "title": "empty ask",
            "request": "dragonfruit dessert",
            "session": {},
            "settings": {},
            "scripted_answers": [],
            "flow": ["recommend-ask"],
            "fake_flow": "empty",
            "expected": {
                "stop_reason": "agent_needs_user_input",
                "min_options": 0,
                "question_contains": "lemon dessert",
            },
        },
    ]
    body = {"version": "livepath-test-v1", "scenarios": scenarios}
    body["freeze_sha256"] = _hashlib.sha256(
        json.dumps(
            {k: v for k, v in body.items() if k != "freeze_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return body


def test_live_path_with_faked_sdk_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket as _socket

    import openai as _openai_module

    from culinary_copilot.config import Settings
    from culinary_copilot.recipes import import_data

    finish_a = {
        "decision": "finish",
        "move_to": "recommend",
        "result": {
            "options": [
                {
                    "dataset_id": "odunola/foodie",
                    "source_id": "soup-1",
                    "title": "Simple Tomato Soup",
                    "quantities": [{"ingredient": "tomato", "amount": "400", "unit": "g"}],
                    "adaptations": [],
                },
                {
                    "dataset_id": "odunola/foodie",
                    "source_id": "curry-1",
                    "title": "Chicken Curry",
                    "quantities": [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
                    "adaptations": [],
                },
            ]
        },
        "constraints_honored": [],
        "note": "live-path finish",
    }
    ask_b = {
        "decision": "ask_user",
        "question": {
            "question_id": "q-live",
            "question_text": (
                "I found no recipes for dragonfruit dessert. "
                "Want me to look for a lemon dessert instead?"
            ),
            "options": ["yes, look", "no"],
        },
        "note": "live-path ask",
    }
    script = [
        # Eight tool turns (one call each): the capped history must keep
        # whole call/output groups, never strand an output.
        _sdk_response(calls=[("c1", "search_recipes", {"query": "soup for dinner"})]),
        _sdk_response(
            calls=[("c2", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "soup-1"})]
        ),
        _sdk_response(calls=[("c3", "search_techniques", {"query": "soup"})]),
        _sdk_response(
            calls=[("c4", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "curry-1"})]
        ),
        _sdk_response(calls=[("c5", "search_recipes", {"query": "chicken curry"})]),
        _sdk_response(calls=[("c6", "search_techniques", {"query": "chicken"})]),
        _sdk_response(
            calls=[("c7", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "soup-1"})]
        ),
        _sdk_response(calls=[("c8", "search_recipes", {"query": "tomato dinner"})]),
        _sdk_response(parsed=finish_a),
        _sdk_response(calls=[("c1", "search_recipes", {"query": "dragonfruit"})]),
        _sdk_response(parsed=ask_b),
    ]
    sdk_calls: list[dict[str, Any]] = []
    seen_tools: list[Any] = []
    seen_formats: list[str] = []

    def _strict_bad_request(what: str, param: str, violations: list[str]) -> Any:
        import httpx
        from openai import BadRequestError

        message = f"Invalid schema for {what}: {violations[0]}"
        body = {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": "strict_schema_violation",
                "param": param,
            }
        }
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        return BadRequestError(
            message=message,
            response=httpx.Response(400, request=request, json=body),
            body=body,
        )

    def _unpaired_call_id(input_items: list[Any]) -> str | None:
        """First call id without exactly one output (None when paired)."""
        from collections import Counter

        calls: Counter[str] = Counter()
        outputs: Counter[str] = Counter()
        for item in input_items:
            if not isinstance(item, dict):
                continue
            call_id = str(item.get("call_id") or "")
            if item.get("type") == "function_call":
                calls[call_id] += 1
            elif item.get("type") == "function_call_output":
                outputs[call_id] += 1
        for call_id in list(calls) + [c for c in outputs if c not in calls]:
            if calls[call_id] != 1 or outputs[call_id] != 1:
                return call_id or "unknown"
        return None

    def _pairing_bad_request(call_id: str) -> Any:
        import httpx
        from openai import BadRequestError

        message = f"No tool output found for function call {call_id}"
        body = {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": "missing_tool_output",
                "param": "input",
            }
        }
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        return BadRequestError(
            message=message,
            response=httpx.Response(400, request=request, json=body),
            body=body,
        )

    class _FakeResponses:
        async def parse(self, **kwargs: Any) -> Any:
            sdk_calls.append(kwargs)
            from openai.lib._parsing._responses import (
                type_to_text_format_param as _to_text_format,
            )

            from culinary_copilot.tools.registry import strict_violations as _strict_violations

            tools = kwargs.get("tools") or []
            seen_tools.append(tools)
            for _tool in tools:
                _violations = _strict_violations(_tool.get("parameters", {}))
                if _violations:
                    raise _strict_bad_request(
                        f"function '{_tool.get('name')}'", "tools", _violations
                    )
            model = kwargs.get("text_format")
            if model is not None and not isinstance(model, dict):
                seen_formats.append(str(getattr(model, "__name__", model)))
                _format_violations = _strict_violations(_to_text_format(model)["schema"])
                if _format_violations:
                    raise _strict_bad_request(
                        f"response_format '{seen_formats[-1]}'",
                        "text.format",
                        _format_violations,
                    )
            _pairing = _unpaired_call_id(kwargs.get("input") or [])
            if _pairing is not None:
                # Behave like OpenAI: an output-less call is a 400.
                raise _pairing_bad_request(_pairing)
            item = script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

    class _FakeAsyncOpenAI:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.responses = _FakeResponses()

    openai_inits: list[str] = []

    class _FakeOpenAI:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            openai_inits.append("sync-client")

            class _Embeddings:
                async def create(self, **kwargs: Any) -> Any:
                    raise AssertionError("embeddings client must not be called")

            self.embeddings = _Embeddings()

    monkeypatch.setattr(_openai_module, "AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr(_openai_module, "OpenAI", _FakeOpenAI)

    real_connect = _socket.socket.connect

    def _guarded_connect(self, address):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else ""
        if host in ("127.0.0.1", "::1", "localhost", ""):
            return real_connect(self, address)
        raise AssertionError(f"non-local network access attempted: {host!r}")

    monkeypatch.setattr(_socket.socket, "connect", _guarded_connect)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    db_name = "culinary_test_livepath"
    db_url = f"{head}/{db_name}"
    maint = create_engine(f"{head}/postgres", isolation_level="AUTOCOMMIT")
    try:
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        eng = create_engine(db_url)
        try:
            with eng.begin() as conn:
                import_data.apply_migrations(conn)
            _seed_livepath_db(eng)
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "techniques"))
            import load as _technique_loader

            assert (
                _technique_loader.main(
                    [
                        "--database-url",
                        db_url,
                        "--expect-db-name",
                        db_name,
                        "--expect-db-host",
                        "localhost",
                    ]
                )
                == 0
            )
            settings = live_run._effective_settings(
                Settings(
                    _env_file=None,
                    epicure_enabled=False,
                    embeddings_enabled=True,
                    openai_api_key="test-key",
                    llm_enabled=True,
                    llm_recommendation_enabled=True,
                )
            )
            import argparse as _argparse

            args = _argparse.Namespace(model="", max_attempts=1, ceiling_usd=0.15)
            raw_dir = tmp_path / "raw"
            summary_out = tmp_path / "live-summary.json"
            live_summary_before = _default_summary_bytes()
            rc = live_run._run_all(
                args,
                settings,
                _livepath_scenarios(),
                db_url,
                raw_dir,
                summary_out,
                fake=False,
            )
            assert rc == 0
            summary = json.loads(summary_out.read_text(encoding="utf-8"))
            assert len(sdk_calls) == 11
            from culinary_copilot.tools.registry import strict_violations as _check_strict

            assert len(seen_tools) == 11
            # Every turn is sent the tool schemas except the one wrap-up
            # turn after c7, which only repeats c2's fetch (2026-10-06).
            tool_less = [i for i, t in enumerate(seen_tools) if not t]
            assert tool_less == [7]
            for payload_tools in seen_tools:
                for _tool in payload_tools:
                    assert _tool["strict"] is True
                    assert _check_strict(_tool["parameters"]) == []
            assert seen_formats == ["AgentDirective"] * 11
            # The scenario request reaches the model: the first turn's
            # input carries it as a user item.
            first_input = sdk_calls[0].get("input") or []
            assert {"role": "user", "content": "soup for dinner"} in first_input
            assert [s["key"] for s in summary["scenarios"]] == ["lp-recommend", "lp-empty"]
            assert summary["scenarios"][0]["stop_reason"] == "agent_sufficient_evidence"
            assert summary["scenarios"][1]["stop_reason"] == "agent_needs_user_input"
            assert summary["isolation"]["ok"] is True
            assert summary["stopped_early"] is None
            spend = summary["spend"]
            assert spend["spent_usd"] > 0.0
            reconciled = [e for e in spend["entries"] if e.get("decision") == "reconciled"]
            assert len(reconciled) == 11
            assert all(e["used_in"] == 1000 and e["used_out"] == 200 for e in reconciled)
            assert live_run.LIVE_SUMMARY == Path(__file__).resolve().parents[1] / (
                "evals/phase3_agent/live-summary.json"
            )
            assert _default_summary_bytes() == live_summary_before
        finally:
            eng.dispose()
            for path in list(sys.path):
                if path.endswith("scripts/techniques"):
                    sys.path.remove(path)
    finally:
        with maint.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname='{db_name}' AND pid <> pg_backend_pid()"
                )
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        maint.dispose()


def test_setup_failure_leaves_no_session(engine, tmp_path: Path) -> None:
    from sqlalchemy import text as _text

    from culinary_copilot.config import Settings
    from culinary_copilot.services.session_store import PostgresSessionStore

    settings = Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    with engine.connect() as conn:
        before = conn.execute(_text("SELECT count(*) FROM sessions")).scalar()

    def _boom_factory(scenario: dict[str, Any]) -> Any:
        raise RuntimeError("context exploded")

    ledger = SpendLedger(model="gpt-6-luna", ceiling_usd=0.15)
    report = live_run.run_scenario_live(
        engine=engine,
        store=store,
        settings=settings,
        scenario=_live_scenario(),
        ledger=ledger,
        provider_factory=lambda s: None,
        context_factory=lambda s, sc: _boom_factory(sc),
        raw_dir=tmp_path,
        max_attempts=2,
        recipe_resolver=None,
    )
    assert report["run_stop"]["reason"] == "runner-error"
    assert "context exploded" in report["run_stop"]["detail"]
    assert report["status"] == "stopped: runner-error"
    assert report["grades"] == {"graded": False, "reason": "runner-error"}
    assert report["sessions"] == []
    assert report["attempts"] == 1
    with engine.connect() as conn:
        after = conn.execute(_text("SELECT count(*) FROM sessions")).scalar()
    assert after == before


def test_scenario_exception_writes_summary(
    engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    from culinary_copilot.config import Settings

    def _raise(**kwargs: Any) -> Any:
        raise RuntimeError("driver exploded")

    monkeypatch.setattr(live_run, "run_scenario_live", _raise)
    args = argparse.Namespace(model="", max_attempts=1, ceiling_usd=0.15)
    settings = Settings(_env_file=None)
    _, test_url = _urls()

    eng = create_engine(test_url)
    try:
        rc = live_run._run_all(
            args,
            settings,
            {"freeze_sha256": "x", "scenarios": [{"key": "s1"}, {"key": "s2"}]},
            test_url,
            tmp_path,
            tmp_path / "summary.json",
            fake=False,
        )
    finally:
        eng.dispose()
    assert rc == 0
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["stopped_early"]["reason"] == "runner-error"
    assert "driver exploded" in summary["stopped_early"]["detail"]
    assert summary["scenarios"][0]["status"] == "stopped: runner-error"
    assert summary["scenarios"][1]["status"] == "not_run: runner-error"


# --- strict schemas, retries, error detail, honest statuses --------------------


def test_effective_settings_zero_all_retry_paths() -> None:
    from culinary_copilot.config import Settings

    raw = Settings(_env_file=None, llm_app_max_retries=3, llm_rec_max_retries=2)
    effective = live_run._effective_settings(raw)
    assert effective.llm_app_max_retries == 0
    assert effective.llm_rec_max_retries == 0
    assert effective.embed_max_retries == 0
    assert raw.llm_rec_max_retries == 2  # the copy, never .env


def test_preflight_refuses_nonzero_rec_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from culinary_copilot.config import Settings

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    args = argparse.Namespace(
        model="",
        ceiling_usd=0.15,
        expect_db_name="culinary_test_live",
        expect_db_host="localhost",
        database_url="",
    )
    settings = Settings(_env_file=None, llm_rec_max_retries=2, llm_recommendation_enabled=True)
    ok, problems, _ = live_run.preflight(args, settings)
    assert not ok
    assert any("llm_rec_max_retries" in p for p in problems)


def test_preflight_requires_recommendation_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from culinary_copilot.config import Settings

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    args = argparse.Namespace(
        model="",
        ceiling_usd=0.15,
        expect_db_name="culinary_test_live",
        expect_db_host="localhost",
        database_url="",
    )
    settings = live_run._effective_settings(Settings(_env_file=None))
    ok, problems, _ = live_run.preflight(args, settings)
    assert not ok
    assert any("LLM_RECOMMENDATION_ENABLED" in p for p in problems)


def test_native_tool_turn_called_once_on_timeout() -> None:
    import asyncio as _asyncio

    import httpx
    from openai import APITimeoutError

    from culinary_copilot.config import Settings
    from culinary_copilot.llm.client import (
        OpenAIApplicationProvider,
        ProviderTimeoutError,
    )

    calls = {"n": 0}

    class _FakeResponses:
        async def parse(self, **kwargs: Any) -> Any:
            calls["n"] += 1
            raise APITimeoutError(
                request=httpx.Request("POST", "https://api.openai.com/v1/responses")
            )

    class _FakeClient:
        responses = _FakeResponses()

    async def _run() -> None:
        settings = live_run._effective_settings(
            Settings(
                _env_file=None,
                llm_recommendation_enabled=True,
                openai_api_key="test-key",
            )
        )
        assert settings.llm_rec_max_retries == 0
        provider = OpenAIApplicationProvider(settings)
        await provider.start()
        provider._client = _FakeClient()  # type: ignore[assignment]
        try:
            await provider.complete_native_tool_turn(
                input_items=[],
                tools=None,
                tool_choice=None,
                response_model=None,
                max_output_tokens=50,
            )
        finally:
            await provider.aclose()

    with pytest.raises(ProviderTimeoutError):
        _asyncio.run(_run())
    assert calls["n"] == 1


def test_ledger_records_http_status_on_ambiguous_failure() -> None:
    import asyncio as _asyncio

    from culinary_copilot.llm.client import ProviderBadRequestError

    err = ProviderBadRequestError(
        "provider rejected the request",
        error_message="Invalid schema for function 'search_recipes'",
        error_code="invalid_schema",
        error_param="tools",
        request_sent=True,
        attempts=1,
    )
    err.attempt_details = [
        {
            "attempt": 1,
            "latency_ms": 5,
            "request_sent": True,
            "sdk_error": "BadRequestError",
            "http_status": 400,
        }
    ]

    class _Raise:
        async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
            raise err

    ledger = _ledger()
    wrapped = live_run.LedgerModelProvider(_Raise(), ledger, max_output=200)
    with pytest.raises(ProviderBadRequestError):
        _asyncio.run(
            wrapped.complete_native_tool_turn(
                input_items=[], tools=None, tool_choice=None, response_model=None
            )
        )
    entry = ledger.entries[-1]
    assert entry["decision"] == "kept-ambiguous"  # conservative reservation kept
    assert entry["http_status"] == 400


def test_provider_error_before_output_is_stopped(engine, tmp_path: Path) -> None:
    from culinary_copilot.config import Settings
    from culinary_copilot.llm.client import ProviderBadRequestError
    from culinary_copilot.services.session_store import PostgresSessionStore

    settings = Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    ledger = _ledger()
    err = ProviderBadRequestError(
        "provider rejected the request",
        error_message="Invalid schema for function 'search_recipes'",
        error_code="invalid_schema",
        error_param="tools",
        request_sent=True,
        attempts=1,
    )
    report = live_run.run_scenario_live(
        engine=engine,
        store=store,
        settings=settings,
        scenario=_live_scenario(),
        ledger=ledger,
        provider_factory=lambda s: _raising_provider(err),
        context_factory=lambda s, sc: live_run._fake_context(s, settings, sc),
        raw_dir=tmp_path,
        max_attempts=1,
        recipe_resolver=None,
    )
    assert report["status"] == "stopped: provider-error"
    assert report["stop_reason"] == "provider_bad_request"
    assert report["grades"] == {"graded": False, "reason": "provider_bad_request"}
    run = report["first_attempt"]["runs"][0]
    assert run["provider_error"]["error_code"] == "invalid_schema"
    assert run["provider_error"]["error_param"] == "tools"
    assert "Invalid schema" in run["provider_error"]["error_message"]
    assert "invalid_schema" in run["message"]


def test_disabled_generation_is_stopped_config_error(engine, tmp_path: Path) -> None:
    from culinary_copilot.config import Settings
    from culinary_copilot.llm.client import ProviderDisabledError
    from culinary_copilot.services.session_store import PostgresSessionStore

    settings = Settings(_env_file=None)
    store = PostgresSessionStore(engine)
    ledger = _ledger()
    report = live_run.run_scenario_live(
        engine=engine,
        store=store,
        settings=settings,
        scenario=_live_scenario(),
        ledger=ledger,
        provider_factory=lambda s: _raising_provider(ProviderDisabledError("off")),
        context_factory=lambda s, sc: live_run._fake_context(s, settings, sc),
        raw_dir=tmp_path,
        max_attempts=1,
        recipe_resolver=None,
    )
    assert report["status"] == "stopped: config-error"
    assert report["stop_reason"] == "generation_disabled"
    assert report["grades"] == {"graded": False, "reason": "generation_disabled"}


# --- request text, pending answers, honest grades, ledger, loops, trajectory --


def _invented_ask_turns() -> list[Any]:
    from culinary_copilot.llm.client import NativeToolCall, NativeTurnResult

    def _chain(calls: list[tuple[str, str, dict[str, Any]]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function_call",
                "call_id": cid,
                "name": name,
                "arguments": json.dumps(args),
            }
            for cid, name, args in calls
        ]

    get_calls = [
        ("g1", "get_recipe", {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}),
        ("g2", "find_balanced_pairings", {"ingredient": "lentils"}),
    ]
    finish = {
        "decision": "finish",
        "move_to": "recommend",
        "result": {
            "options": [
                {
                    "dataset_id": "odunola/foodie",
                    "source_id": "lentil-2",
                    "title": "Red Lentil Soup",
                    "quantities": [{"ingredient": "red lentils", "amount": "200", "unit": "g"}],
                    "adaptations": [],
                }
            ]
        },
        "constraints_honored": [],
        "epicure_lines": [{"ingredient": "pork", "decision": "used", "reason": "fake match"}],
        "note": "good finish",
    }
    ask = {
        "decision": "ask_user",
        "question": {
            "question_id": "model-made-7",
            "question_text": "Do you want the lentil soup spicy?",
            "options": ["yes", "no"],
        },
        "note": "ask",
    }
    return [
        NativeTurnResult(
            tool_calls=[
                NativeToolCall(call_id=cid, name=name, arguments=json.dumps(args))
                for cid, name, args in get_calls
            ],
            parsed=None,
            chain_items=_chain(get_calls),
            input_tokens=5,
            output_tokens=5,
        ),
        NativeTurnResult(
            tool_calls=[], parsed=ask, chain_items=[], input_tokens=5, output_tokens=5
        ),
        NativeTurnResult(
            tool_calls=[], parsed=finish, chain_items=[], input_tokens=5, output_tokens=5
        ),
    ]


def test_runner_answers_pending_question(engine, tmp_path: Path) -> None:
    from culinary_copilot.config import Settings
    from culinary_copilot.services.session_store import PostgresSessionStore

    settings = Settings(_env_file=None, epicure_enabled=True)
    store = PostgresSessionStore(engine)
    turns = _invented_ask_turns()

    class _Script:
        async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
            return turns.pop(0)

    scenario = _live_scenario(
        flow=["recommend", "resume"],
        scripted_answers=[{"question_id": "q-yogurt", "answer": "yes"}],
    )
    report = live_run.run_scenario_live(
        engine=engine,
        store=store,
        settings=settings,
        scenario=scenario,
        ledger=_ledger(),
        provider_factory=lambda s: _Script(),
        context_factory=lambda s, sc: live_run._fake_context(s, settings, sc),
        raw_dir=tmp_path,
        max_attempts=1,
        recipe_resolver=lambda ds, sid: dict(live_run._FAKE_DOCS.get((ds, sid)) or {}) or None,
    )
    assert report["status"] == "completed: answered"
    assert report["stop_reason"] == "agent_sufficient_evidence"
    answered = report["first_attempt"]["answered_question"]
    assert answered["question_id"] == "model-made-7"  # actual, not q-yogurt
    assert answered["question_text"] == "Do you want the lentil soup spicy?"
    assert answered["scripted_answer"] == "yes"


def test_answer_failure_is_stopped_runner_error(
    engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import culinary_copilot.agent.loop as _loop
    from culinary_copilot.agent.loop import AgentLoopError
    from culinary_copilot.config import Settings
    from culinary_copilot.services.session_store import PostgresSessionStore

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AgentLoopError(
            http_status=404, reason="unknown_question", message="unknown question 'x'"
        )

    monkeypatch.setattr(_loop, "record_answer", _boom)
    settings = Settings(_env_file=None, epicure_enabled=True)
    store = PostgresSessionStore(engine)
    turns = _invented_ask_turns()

    class _Script:
        async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
            return turns.pop(0)

    scenario = _live_scenario(
        flow=["recommend", "resume"],
        scripted_answers=[{"question_id": "q-yogurt", "answer": "yes"}],
    )
    report = live_run.run_scenario_live(
        engine=engine,
        store=store,
        settings=settings,
        scenario=scenario,
        ledger=_ledger(),
        provider_factory=lambda s: _Script(),
        context_factory=lambda s, sc: live_run._fake_context(s, settings, sc),
        raw_dir=tmp_path,
        max_attempts=1,
        recipe_resolver=lambda ds, sid: dict(live_run._FAKE_DOCS.get((ds, sid)) or {}) or None,
    )
    assert report["status"] == "stopped: runner-error"
    assert report["stop_reason"] == "runner-error"
    assert report["grades"] == {"graded": False, "reason": "runner-error"}


def test_empty_run_grades_are_na(engine) -> None:
    from culinary_copilot.domain.sessions import SessionState
    from culinary_copilot.services.session_store import PostgresSessionStore

    store = PostgresSessionStore(engine)
    state = store.create(SessionState(id="ses-grade-probe"))
    scenario = _live_scenario()
    grades = live_run.grade_attempt(
        scenario, None, "agent_no_progress", store, state.id, manual_review=True
    )
    assert grades["evidence_support"] == "n/a"
    assert grades["constraint_adherence"] == "n/a"
    assert grades["epicure_behaviour"] == "n/a"
    assert grades["clarification_quality"] == "manual-review"
    assert grades["task_completion"] is False


def test_spend_history_floor_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import argparse
    import json as _json

    from culinary_copilot.config import Settings

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    history = tmp_path / "spend-history.json"
    history.write_text(
        _json.dumps(
            {
                "ceiling_usd": 0.15,
                "runs": [
                    {
                        "run_utc": "2026-09-30T01:05:26Z",
                        "attempt": 3,
                        "model": "gpt-6-luna",
                        "entries": [
                            {
                                "label": "model-turn-1",
                                "decision": "kept-ambiguous",
                                "usd": 0.1499,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert live_run.recorded_spend_total(history) == 0.1499
    args = argparse.Namespace(
        model="",
        ceiling_usd=0.15,
        expect_db_name="culinary_test_live",
        expect_db_host="localhost",
        database_url="",
    )
    settings = live_run._effective_settings(
        Settings(_env_file=None, llm_recommendation_enabled=True)
    )
    ok, problems, record = live_run.preflight(args, settings, history_path=history)
    assert not ok
    assert any("first turn" in p for p in problems)
    assert record["prior_recorded_spend_usd"] == 0.1499


def test_fresh_providers_are_loop_bound(engine, tmp_path: Path) -> None:
    import asyncio as _asyncio

    from culinary_copilot.config import Settings
    from culinary_copilot.services.session_store import PostgresSessionStore

    settings = Settings(_env_file=None, epicure_enabled=True)
    store = PostgresSessionStore(engine)
    instances: list[Any] = []
    script = list(_invented_ask_turns())

    class _Tracked:
        def __init__(self) -> None:
            self.used: list[Any] = []
            self.closed: list[Any] = []
            instances.append(self)

        async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
            self.used.append(_asyncio.get_running_loop())
            return script.pop(0)

        async def aclose(self) -> None:
            self.closed.append(_asyncio.get_running_loop())

    scenario = _live_scenario(
        flow=["recommend", "resume"],
        scripted_answers=[{"question_id": "q-yogurt", "answer": "yes"}],
    )
    report = live_run.run_scenario_live(
        engine=engine,
        store=store,
        settings=settings,
        scenario=scenario,
        ledger=_ledger(),
        provider_factory=lambda s: _Tracked(),
        context_factory=lambda s, sc: live_run._fake_context(s, settings, sc),
        raw_dir=tmp_path,
        max_attempts=1,
        recipe_resolver=lambda ds, sid: dict(live_run._FAKE_DOCS.get((ds, sid)) or {}) or None,
        fresh_provider_per_run=True,
    )
    assert report["stop_reason"] == "agent_sufficient_evidence"
    used = [inst for inst in instances if inst.used]
    assert len(used) == 2  # ask run + resume run; the setup probe is never used
    for inst in used:
        assert len(inst.closed) == 1
        assert all(loop is inst.closed[0] for loop in inst.used)


def test_trajectory_recorded_in_raw_report(engine, tmp_path: Path) -> None:
    from culinary_copilot.config import Settings
    from culinary_copilot.services.session_store import PostgresSessionStore

    settings = Settings(_env_file=None, epicure_enabled=True)
    store = PostgresSessionStore(engine)
    report = live_run._run_fake_scenario(
        engine,
        store,
        settings,
        _live_scenario(),
        _ledger(),
        tmp_path,
        max_attempts=1,
    )
    assert report["trajectory"], "every scenario records a trajectory"
    session_traj = report["trajectory"][0]
    tool_events = [e for e in session_traj["events"] if e["type"] == "tool_call"]
    assert tool_events, "tool calls are in the trajectory"
    first = tool_events[0]
    assert first["args_digest"] and first["args"]
    assert first["outcome"] in ("ok", "error")
    assert session_traj["option_titles"], "model option titles are recorded"


def test_preflight_refuses_disabled_epicure(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from culinary_copilot.config import Settings

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    args = argparse.Namespace(
        model="",
        ceiling_usd=0.15,
        expect_db_name="culinary_test_live",
        expect_db_host="localhost",
        database_url="",
    )
    settings = live_run._effective_settings(Settings(_env_file=None))
    assert bool(settings.epicure_enabled) is False
    ok, problems, _ = live_run.preflight(args, settings)
    assert not ok
    assert any("epicure_enabled must be true" in p for p in problems)
    scenarios = load_scenarios(
        Path(__file__).resolve().parents[1] / "evals" / "phase3_agent" / "live_scenarios.json"
    )
    ok, problems, record = live_run.preflight(args, settings, scenarios)
    assert not ok
    assert any("live-chicken-dinner" in p for p in problems)
    assert record["epicure"]["scenarios"]["live-chicken-dinner"] is False
    assert record["epicure"]["scenarios"]["live-epicure-unavailable"] is False


def test_preflight_refuses_failing_epicure_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from culinary_copilot.config import Settings

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    args = argparse.Namespace(
        model="",
        ceiling_usd=0.15,
        expect_db_name="culinary_test_live",
        expect_db_host="localhost",
        database_url="",
    )
    settings = live_run._effective_settings(
        Settings(_env_file=None, epicure_enabled=True, llm_recommendation_enabled=True)
    )
    failing = {"core": {"ok": False, "pairs": 0, "error": "LocalEntryNotFoundError: no cache"}}
    ok, problems, record = live_run.preflight(args, settings, probe=lambda s: dict(failing))
    assert not ok
    assert any("Epicure cache probe failed for core" in p for p in problems)
    assert record["epicure"]["probe"]["core"]["ok"] is False


def test_unavailable_scenario_keeps_false_override(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from culinary_copilot.config import Settings

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    args = argparse.Namespace(
        model="",
        ceiling_usd=0.15,
        expect_db_name="culinary_test_live",
        expect_db_host="localhost",
        database_url="",
    )
    settings = live_run._effective_settings(
        Settings(_env_file=None, epicure_enabled=True, llm_recommendation_enabled=True)
    )
    scenarios = load_scenarios(
        Path(__file__).resolve().parents[1] / "evals" / "phase3_agent" / "live_scenarios.json"
    )
    unavailable = [s for s in scenarios["scenarios"] if s["key"] == "live-epicure-unavailable"][0]
    assert live_run._scenario_epicure_enabled(True, unavailable) is False
    assert live_run._scenario_settings(settings, unavailable).epicure_enabled is False
    passing = {n: {"ok": True, "pairs": 1} for n in ("core", "cooc", "chem", "substitutions")}
    ok, problems, record = live_run.preflight(
        args, settings, scenarios, probe=lambda s: dict(passing)
    )
    assert not any("epicure" in p.lower() for p in problems)
    assert record["epicure"]["scenarios"]["live-epicure-unavailable"] is False
    assert record["epicure"]["scenarios"]["live-chicken-dinner"] is True
    assert record["epicure"]["probe"] == passing


def test_spend_history_append_totals_stored_usd(tmp_path: Path) -> None:
    import json as _json

    history = tmp_path / "spend-history.json"
    history.write_text(
        _json.dumps(
            {
                "ceiling_usd": 0.15,
                "runs": [
                    {
                        "run_utc": "2026-09-30T01:05:26Z",
                        "attempt": 3,
                        "model": "gpt-6-luna",
                        "entries": [{"label": "t1", "decision": "reconciled", "usd": 0.001}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ledger = SpendLedger(model="gpt-6-luna", ceiling_usd=0.15)
    assert ledger.reserve("t2", input_tokens=1000, max_output=500) is True
    ledger.reconcile("t2", reported_in=100, reported_out=50)
    total = live_run.append_spend_history(
        history, model="gpt-6-luna", entries=list(ledger.entries), ceiling_usd=0.15
    )
    body = _json.loads(history.read_text(encoding="utf-8"))
    stored = sum(e.get("usd", 0.0) for r in body["runs"] for e in r["entries"])
    assert total == stored
    assert body["runs"][-1]["attempt"] == 4  # one past the highest stored attempt


def test_projection_carries_errors_stop_reason(tmp_path: Path) -> None:
    project = live_run._project_trajectory_event
    long_errors = [f"e{i}:" + "x" * 500 for i in range(6)]
    reject = project("agent_validation_reject", {"errors": long_errors})
    assert reject is not None
    assert len(reject["errors"]) == 5
    assert all(len(e) == 300 for e in reject["errors"])
    assert "stop" in reject and "reason" in reject
    for event_type, payload in [
        ("tool_call", {"tool": "search_recipes", "outcome": "ok", "reason": None}),
        ("provider_error", {"reason": "provider_timeout"}),
        ("agent_step", {"note": "n"}),
        ("agent_question", {"question_id": "q", "question_text": "t", "question_options": []}),
        ("agent_finished", {"note": "n", "options": [], "stop_reason": "agent_max_steps"}),
        ("agent_answer", {"question_id": "q"}),
    ]:
        projected = project(event_type, dict(payload))
        assert projected is not None
        assert "stop" in projected and "reason" in projected
    finished = project("agent_finished", {"note": "n", "stop_reason": "agent_max_steps"})
    assert finished is not None and finished["stop"] == "agent_max_steps"


def test_no_answer_run_reports_no_answer_status(engine, tmp_path: Path) -> None:
    assert live_run._final_answered({"options": [{"title": "x"}]}) is True
    assert live_run._final_answered({"plan": {"steps": []}}) is True
    assert live_run._final_answered({"question": {"question_id": "q"}}) is False
    assert live_run._final_answered(None) is False
    assert live_run._final_answered({}) is False


def test_question_stop_reports_no_answer_and_na_constraint(engine, tmp_path: Path) -> None:
    # Checkpoint B condition 6 via the runner: a stop that only asks is
    # "completed: no-answer", and constraint adherence is "n/a" when
    # nothing was offered.
    from culinary_copilot.config import Settings
    from culinary_copilot.llm.client import NativeTurnResult
    from culinary_copilot.services.session_store import PostgresSessionStore

    class _Ask:
        async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
            return NativeTurnResult(
                tool_calls=[],
                parsed={
                    "decision": "ask_user",
                    "question": {"question_id": "q1", "question_text": "Which allergy?"},
                    "note": "need the name",
                },
                chain_items=[],
                input_tokens=5,
                output_tokens=5,
            )

    settings = Settings(_env_file=None, epicure_enabled=True)
    store = PostgresSessionStore(engine)
    scenario = {
        "key": "ask-only",
        "request": "Dinner for a friend with an allergy.",
        "session": {"constraints": {"dietary_constraints": ["vegan"]}},
        "settings": {},
        "scripted_answers": [],
        "flow": ["recommend"],
        "fake_flow": "direct",
        "expected": {"stop_reason": "agent_needs_user_input"},
    }
    report = live_run.run_scenario_live(
        engine=engine,
        store=store,
        settings=settings,
        scenario=scenario,
        ledger=_ledger(),
        provider_factory=lambda s: _Ask(),
        context_factory=lambda s, sc: live_run._fake_context(s, settings, sc),
        raw_dir=tmp_path,
        max_attempts=1,
        recipe_resolver=None,
    )
    assert report["stop_reason"] == "agent_needs_user_input"
    assert report["status"] == "completed: no-answer (agent_needs_user_input)"
    assert report["grades"]["constraint_adherence"] == "n/a"

    settings = Settings(_env_file=None, epicure_enabled=True)
    store = PostgresSessionStore(engine)

    class _Empty:
        async def complete_native_tool_turn(self, **kwargs: Any) -> Any:
            return NativeTurnResult(
                tool_calls=[], parsed=None, chain_items=[], input_tokens=5, output_tokens=5
            )

    report = live_run.run_scenario_live(
        engine=engine,
        store=store,
        settings=settings,
        scenario=_live_scenario(),
        ledger=_ledger(),
        provider_factory=lambda s: _Empty(),
        context_factory=lambda s, sc: live_run._fake_context(s, settings, sc),
        raw_dir=tmp_path,
        max_attempts=1,
        recipe_resolver=None,
    )
    assert report["stop_reason"] == "agent_no_progress"
    assert report["status"] == "completed: no-answer (agent_no_progress)"


def test_fake_path_uses_effective_epicure_settings(engine, tmp_path: Path) -> None:
    from culinary_copilot.config import Settings
    from culinary_copilot.services.session_store import PostgresSessionStore

    store = PostgresSessionStore(engine)
    scenario = _live_scenario(fake_flow="direct")
    disabled = live_run._run_fake_scenario(
        engine, store, Settings(_env_file=None), scenario, _ledger(), tmp_path, 1
    )
    assert disabled["status"] == "completed: no-answer (agent_validation_failed)"
    enabled = live_run._run_fake_scenario(
        engine,
        store,
        Settings(_env_file=None, epicure_enabled=True),
        scenario,
        _ledger(),
        tmp_path,
        1,
    )
    assert enabled["status"] == "completed: answered"
    assert enabled["stop_reason"] == "agent_sufficient_evidence"


def test_scenario_filter_and_attempts_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("EPICURE_ENABLED", "true")
    from culinary_copilot.config import Settings

    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    db_url = f"{head}/culinary_test_live_filter"
    raw_dir = tmp_path / "raw"
    summary_out = tmp_path / "summary.json"
    rc = live_run.main(
        [
            "--fake",
            "--database-url",
            db_url,
            "--raw-dir",
            str(raw_dir),
            "--summary-out",
            str(summary_out),
            "--scenarios",
            "live-direct-lentil",
            "--max-attempts",
            "1",
        ]
    )
    assert rc == 0
    summary = json.loads(summary_out.read_text(encoding="utf-8"))
    assert [s["key"] for s in summary["scenarios"]] == ["live-direct-lentil"]
    assert summary["scenario_keys"] == ["live-direct-lentil"]
    assert summary["max_attempts"] == 1
    assert summary["scenarios_file"].endswith("live_scenarios_v2.json")
    assert (
        summary["scenarios_sha256"]
        == (
            live_run.load_scenarios(
                Path(__file__).resolve().parents[1]
                / "evals"
                / "phase3_agent"
                / "live_scenarios_v2.json"
            )["freeze_sha256"]
        )
    )
    entry = summary["scenarios"][0]
    assert entry["stop_reason"] == "agent_sufficient_evidence"
    assert entry["expected_stop_matched"] is True
    out = capsys.readouterr().out
    assert "1 matched expected stop" in out


def test_unknown_scenario_key_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    rc = live_run.main(["--fake", "--scenarios", "no-such-scenario"])
    assert rc == 2
    assert "unknown scenario keys" in capsys.readouterr().err


def test_max_attempts_rejects_three(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        live_run.main(["--fake", "--max-attempts", "3"])
    assert excinfo.value.code == 2


def test_grader_requires_plan_when_expected(engine) -> None:
    from culinary_copilot.domain.sessions import SessionState
    from culinary_copilot.services.session_store import PostgresSessionStore

    store = PostgresSessionStore(engine)
    state = store.create(SessionState(id="ses-grade-plan"))
    # Attempt-7 chicken shape: options final, plan expected.
    scenario = _live_scenario(
        expected={
            "stop_reason": "agent_sufficient_evidence",
            "epicure": "consulted",
            "min_options": 2,
            "plan": True,
        }
    )
    options_final = {
        "options": [
            {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
            {"dataset_id": "odunola/foodie", "source_id": "lentil-2"},
        ]
    }
    grades = live_run.grade_attempt(
        scenario, options_final, "agent_sufficient_evidence", store, state.id
    )
    assert grades["termination"] is True
    assert grades["task_completion"] is False
    plan_final = {"plan": {"source": {"dataset_id": "odunola/foodie", "source_id": "curry-1"}}}
    free = _live_scenario(
        expected={"stop_reason": "agent_sufficient_evidence", "min_options": 0, "plan": True}
    )
    grades = live_run.grade_attempt(free, plan_final, "agent_sufficient_evidence", store, state.id)
    assert grades["task_completion"] is True


def test_grader_requires_technique_answer_kind(engine) -> None:
    from culinary_copilot.domain.sessions import SessionState
    from culinary_copilot.services.session_store import PostgresSessionStore

    store = PostgresSessionStore(engine)
    state = store.create(SessionState(id="ses-grade-kind"))
    scenario = _live_scenario(
        expected={
            "stop_reason": "agent_sufficient_evidence",
            "epicure": "skip:simple_technique_question",
            "kind": "technique_answer",
            "min_options": 0,
            "plan": False,
        }
    )
    answered = {"technique_answer": {"text": "Simmer.", "technique_refs": [], "attribution": []}}
    grades = live_run.grade_attempt(
        scenario, answered, "agent_sufficient_evidence", store, state.id
    )
    assert grades["task_completion"] is True
    options_only = {"options": [{"dataset_id": "odunola/foodie", "source_id": "curry-1"}]}
    grades = live_run.grade_attempt(
        scenario, options_only, "agent_sufficient_evidence", store, state.id
    )
    assert grades["task_completion"] is False


def test_grader_enforces_epicure_lines_minimum(engine) -> None:
    from culinary_copilot.domain.sessions import SessionState
    from culinary_copilot.services.session_store import PostgresSessionStore

    store = PostgresSessionStore(engine)
    state = store.create(SessionState(id="ses-grade-lines"))
    scenario = _live_scenario(
        expected={
            "stop_reason": "agent_sufficient_evidence",
            "epicure": "consulted",
            "min_options": 0,
            "plan": False,
            "min_epicure_lines": 3,
        }
    )
    final: dict[str, Any] = {"options": []}
    store.append_event(state.id, "agent_finished", {"note": "n", "epicure_lines": ["a", "b"]})
    grades = live_run.grade_attempt(scenario, final, "agent_sufficient_evidence", store, state.id)
    assert grades["task_completion"] is False
    store.append_event(state.id, "agent_finished", {"note": "n", "epicure_lines": ["a", "b", "c"]})
    grades = live_run.grade_attempt(scenario, final, "agent_sufficient_evidence", store, state.id)
    assert grades["task_completion"] is True


def test_projection_carries_per_turn_tokens() -> None:
    project = live_run._project_trajectory_event
    tool = project(
        "tool_call",
        {"tool": "search_recipes", "outcome": "ok", "input_tokens": 120, "output_tokens": 8},
    )
    assert tool is not None
    assert tool["input_tokens"] == 120
    assert tool["output_tokens"] == 8
    bare = project("agent_step", {"note": "n"})
    assert bare is not None
    assert bare["input_tokens"] is None
    assert bare["output_tokens"] is None


def test_projection_carries_finish_review_keys() -> None:
    project = live_run._project_trajectory_event
    finished = project(
        "agent_finished",
        {
            "note": "1 option(s) were removed because they failed source checks: Ghost.",
            "model_note": "Two options for you",
            "options": 2,
            "epicure_lines": ["used pork: crisp contrast"],
            "dropped_options": [{"index": 2}],
            "single_option_reason": None,
            "plan_source": {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
            "steps_source": "source",
            "stop_reason": "agent_sufficient_evidence",
            "input_tokens": 500,
            "output_tokens": 60,
        },
    )
    assert finished is not None
    assert finished["note"].startswith("1 option(s) were removed")
    assert finished["model_note"] == "Two options for you"
    assert finished["epicure_lines"] == ["used pork: crisp contrast"]
    assert finished["dropped_options"] == [{"index": 2}]
    assert finished["single_option_reason"] is None
    assert finished["steps_source"] == "source"
    assert finished["input_tokens"] == 500
    assert finished["output_tokens"] == 60


def test_projection_carries_search_events_minimized() -> None:
    project = live_run._project_trajectory_event
    cases = [
        (
            "search_slot_claimed",
            {"call_id": "web-abc", "slots_used": 1, "slots_max": 3, "extra": "drop"},
            {"call_id", "slots_used", "slots_max"},
        ),
        (
            "search_requested",
            {"call_id": "web-abc", "minimized_query": "okonomiyaki recipe", "permission": True},
            {"call_id", "minimized_query", "permission"},
        ),
        (
            "search_results_retrieved",
            {
                "call_id": "web-abc",
                "urls": ["https://example.com/a"],
                "web_search_call_ids": ["ws_1"],
                "retrieved_at": "2026-10-03T00:00:00Z",
            },
            {"call_id", "urls", "web_search_call_ids", "retrieved_at"},
        ),
        (
            "evidence_evaluated",
            {
                "call_id": "web-abc",
                "evaluations": [
                    {"url": "https://example.com/a", "classification": "recipe", "decision": "kept"}
                ],
            },
            {"call_id", "evaluations"},
        ),
        (
            "search_outcome",
            {"call_id": "web-abc", "outcome": "ok"},
            {"call_id", "outcome"},
        ),
        (
            "search_operations",
            {
                "call_id": "web-abc",
                "latency_ms": 12.5,
                "model": "gpt-6-luna",
                "input_tokens": 9230,
                "output_tokens": 310,
                "estimate_status": "reconciled",
            },
            {"call_id", "latency_ms", "model", "input_tokens", "output_tokens", "estimate_status"},
        ),
    ]
    for event_type, payload, keys in cases:
        projected = project(event_type, dict(payload))
        assert projected is not None, event_type
        assert projected["type"] == event_type
        for key in keys:
            assert projected[key] == payload[key], (event_type, key)
        assert "extra" not in projected
        assert "stop" in projected and "reason" in projected


def test_projection_search_tool_call_carries_result_facts() -> None:
    project = live_run._project_trajectory_event
    facts = {"source_count": 3, "classifications": ["recipe", "reference", "video"]}
    projected = project(
        "tool_call",
        {"tool": "search_web", "outcome": "ok", "result_facts": dict(facts)},
    )
    assert projected is not None
    assert projected["result_facts"] == facts
    other = project(
        "tool_call",
        {
            "tool": "search_recipes",
            "outcome": "ok",
            "result_facts": {"source_count": 9},
        },
    )
    assert other is not None
    assert "result_facts" not in other


def test_search_web_result_facts_shape() -> None:
    from culinary_copilot.tools.registry import _result_facts

    facts = _result_facts(
        "search_web",
        {
            "ok": True,
            "sources": [
                {
                    "url": "https://example.com/a",
                    "title": "Guide A",
                    "classification": "recipe",
                },
                {
                    "url": "https://example.org/b",
                    "title": "",
                    "citation_title": "Cited B",
                    "classification": "reference",
                },
            ],
        },
    )
    assert facts == {
        "source_count": 2,
        "classifications": ["recipe", "reference"],
        "titles": ["Guide A", "Cited B"],
        "hosts": ["example.com", "example.org"],
    }
    assert _result_facts("search_web", {"ok": False}) is None
