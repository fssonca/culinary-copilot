"""H8 live-check runner support (offline: fakes and a disposable database)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals" / "phase3_agent"))

import live_run

from culinary_copilot.config import Settings

SCENARIOS = Path(__file__).resolve().parents[1] / "evals" / "h8_live" / "scenarios.json"
TEST_DB = "culinary_test_h8_live"


def _settings(**over: Any) -> Settings:
    return live_run._effective_settings(
        Settings(
            _env_file=None,
            llm_recommendation_enabled=True,
            epicure_enabled=True,
            embeddings_enabled=True,
            **over,
        )
    )


def _args(**over: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "model": None,
        "max_attempts": 1,
        "live": True,
        "budget_pool": "h8",
        "ceiling_usd": 0.15,
        "acknowledge_live_run": "",
        "acknowledge_search_estimate": "",
        "search_max_per_live_session": 2,
        "max_campaign_searches": None,
        "max_model_turns": None,
        "database_url": None,
        "expect_db_name": "dummy",
        "expect_db_host": "localhost",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _problems(args: SimpleNamespace, settings: Settings, scenarios: dict[str, Any]) -> list[str]:
    _, problems, _ = live_run.preflight(args, settings, scenarios, history_path=None)
    return problems


def test_h8_pool_is_capped_at_015() -> None:
    assert live_run.BUDGET_POOLS["h8"]["cap_usd"] == 0.15
    assert str(live_run.BUDGET_POOLS["h8"]["history"]).endswith("h8-live/spend-history.json")


def test_h8_live_needs_ack_and_fits_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    scenarios = live_run.load_scenarios(SCENARIOS)
    problems = _problems(_args(), _settings(), scenarios)
    assert any("h8 live run refused" in p for p in problems)
    acked = _problems(_args(acknowledge_live_run=live_run.H8_ACK_VALUE), _settings(), scenarios)
    assert not any("h8 live run refused" in p for p in acked)
    assert not any("web search off" in p for p in acked)
    over = _problems(
        _args(acknowledge_live_run=live_run.H8_ACK_VALUE, ceiling_usd=0.16), _settings(), scenarios
    )
    assert any("exceeds $0.15 h8 pool cap" in p for p in over)


def test_h8_refuses_web_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    args = _args(acknowledge_live_run=live_run.H8_ACK_VALUE)
    operator_on = _problems(args, _settings(web_search_enabled=True), {"scenarios": []})
    assert any("web search off" in p for p in operator_on)
    session_on = {
        "scenarios": [{"key": "s", "session": {"internet_search_allowed": True}, "settings": {}}]
    }
    assert any("web search off" in p for p in _problems(args, _settings(), session_on))


def test_h8_scenarios_are_frozen_and_use_demo_limits() -> None:
    payload = live_run.load_scenarios(SCENARIOS)  # raises on a hash mismatch
    keys = [s["key"] for s in payload["scenarios"]]
    assert keys == ["h8-party-baking", "h8-allergy-dessert"]
    for scenario in payload["scenarios"]:
        assert set(scenario["flow"]) <= live_run.KNOWN_FLOW_STEPS
        assert scenario["session"]["steps_remaining"] == 40
        assert scenario["session"]["tool_calls_remaining"] == 40
        assert scenario["session"]["internet_search_allowed"] is False
        assert scenario["settings"]["web_search_enabled"] is False
        assert scenario["settings"]["agent_input_token_ceiling"] == 300000
        assert scenario["settings"]["agent_output_token_ceiling"] == 60000
        assert set(scenario["settings"]) <= set(Settings.model_fields)
        assert scenario["followup_message"]
        assert scenario["expected"]["workflow"] == ["options", "plan", "technique_answer"]


def test_workflow_reached_reads_every_run() -> None:
    runs = [
        {"final": {"question": {"question_text": "?"}}},
        {"final": {"options": [{"title": "x"}]}},
        {"final": {"plan": {"steps": ["a"]}}},
        {"final": {"technique_answer": {"text": "t"}}},
    ]
    assert live_run.workflow_reached(runs) == {
        "asked": True,
        "options": True,
        "plan": True,
        "technique_answer": True,
    }
    assert live_run.workflow_reached(runs[:2])["plan"] is False


@pytest.fixture()
def engine():
    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    try:
        live_run._ensure_disposable_db(f"{head}/{TEST_DB}")
        eng = create_engine(f"{head}/{TEST_DB}")
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        yield eng
        eng.dispose()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL unavailable for H8 runner tests: {exc!r}")
    finally:
        try:
            live_run._drop_disposable_db(f"{head}/{TEST_DB}")
        except Exception:
            pass


def test_h8_flow_fake_end_to_end(engine, tmp_path: Path) -> None:
    from culinary_copilot.services.session_store import PostgresSessionStore

    settings = Settings(_env_file=None, epicure_enabled=True)
    store = PostgresSessionStore(engine)
    scenario = live_run.load_scenarios(SCENARIOS)["scenarios"][0]
    ledger = live_run.SpendLedger(model="gpt-6-luna", ceiling_usd=0.15)
    report = live_run.run_scenario_live(
        engine=engine,
        store=store,
        settings=live_run._scenario_settings(settings, scenario),
        scenario=scenario,
        ledger=ledger,
        provider_factory=lambda s: live_run.FakeRunProvider(s),
        context_factory=lambda s, sc: live_run._fake_context(s, settings, sc),
        raw_dir=tmp_path,
        max_attempts=1,
        recipe_resolver=lambda ds, sid: dict(live_run._FAKE_DOCS.get((ds, sid)) or {}) or None,
        technique_resolver=live_run._fake_technique_resolver,
    )
    first = report["first_attempt"]
    assert [q["scripted_answer"] for q in first["answered_questions"]] == [
        a["answer"] for a in scenario["scripted_answers"][:2]
    ]
    assert first["runs"][-1].get("followup") is True
    assert report["grades"]["workflow"]["plan"] is True
    assert report["grades"]["workflow_complete"] is True
    assert report["grades"]["task_completion"] is True
    assert report["stop_reason"] == "agent_sufficient_evidence"
    events = [e.event_type for e in store.list_events(report["sessions"][0])]
    assert events.count("agent_answer") == 2
    assert json.dumps(report, default=str)
