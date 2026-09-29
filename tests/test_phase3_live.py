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
from live_run import SpendLedger, estimate_input_tokens, load_scenarios, verify_isolation

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


def test_input_bound_dominates_chars_per_4() -> None:
    payload = {"input_items": [{"role": "user", "content": "hi" * 500}], "tools": [], "schema": {}}
    bound = estimate_input_tokens(payload)
    flat = json.dumps(payload, sort_keys=True, default=str)
    assert bound >= len(flat) // 4
    assert bound == max(len(flat.encode()) // 3, len(flat) // 4)


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
    assert not Path("evals/phase3_agent/live-summary.json").exists()
    assert not (
        Path(__file__).resolve().parents[1] / "evals" / "phase3_agent" / "live-summary.json"
    ).exists()
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
                return NativeTurnResult(
                    tool_calls=[
                        NativeToolCall(
                            call_id="c1",
                            name="search_recipes",
                            arguments=json.dumps({"query": "lentil"}),
                        ),
                        NativeToolCall(
                            call_id="c2",
                            name="get_recipe",
                            arguments=json.dumps(
                                {"dataset_id": "odunola/foodie", "source_id": "lentil-2"}
                            ),
                        ),
                        NativeToolCall(
                            call_id="c3",
                            name="find_balanced_pairings",
                            arguments=json.dumps({"ingredient": "lentils"}),
                        ),
                    ],
                    parsed=None,
                    chain_items=[],
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
