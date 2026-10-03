"""Phase 5 run-preparation tests (fakes only, disposable databases).

Covers: the P3-L-13 allergy ask-and-resume grade (pass, not-exercised,
peanut failure), the search-conditional preflight acknowledgment both
ways, budget-pool selection and first-fit refusal, the campaign search
cap across runs, step-1 stopping after one search, and estimate-breach
stop plus acknowledgment. Neither live run is authorized here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals" / "phase3_agent"))

import live_run
from live_run import SpendLedger

TEST_DB = "culinary_test_phase5_runs"


def _urls() -> tuple[str, str]:
    from culinary_copilot.config import Settings

    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/{TEST_DB}"


def _args(**overrides: Any) -> argparse.Namespace:
    base = {
        "model": "",
        "ceiling_usd": 0.15,
        "expect_db_name": "culinary_test_phase5_runs",
        "expect_db_host": "localhost",
        "database_url": "",
        "live": True,
        "budget_pool": "phase3",
        "acknowledge_search_estimate": "",
        "search_max_per_live_session": 2,
        "max_campaign_searches": None,
        "max_attempts": 1,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _settings():
    from culinary_copilot.config import Settings

    return live_run._effective_settings(Settings(_env_file=None))


def _search_on_scenarios() -> dict[str, Any]:
    return {
        "scenarios": [
            {
                "key": "s-on",
                "session": {"internet_search_allowed": True},
                "settings": {},
                "expected": {},
            }
        ]
    }


def _search_off_scenarios() -> dict[str, Any]:
    return {
        "scenarios": [
            {"key": "s-off", "session": {"internet_search_allowed": False}, "settings": {}}
        ]
    }


class _FakeStore:
    def __init__(
        self,
        events: list[Any] | None = None,
        confirmed: list[Any] | None = None,
    ) -> None:
        self._events = list(events or [])
        self._confirmed = list(confirmed or [])

    def list_events(self, session_id: str) -> list[Any]:
        return list(self._events)

    def get(self, session_id: str) -> Any:
        return SimpleNamespace(confirmed_answers=list(self._confirmed))


def _question_event(text: str = "What is your friend allergic to?") -> Any:
    return SimpleNamespace(
        event_type="agent_question",
        payload={"question_id": "q-allergy", "question_text": text},
    )


def _answer_event() -> Any:
    return SimpleNamespace(event_type="agent_answer", payload={"question_id": "q-allergy"})


def _allergy_scenario() -> dict[str, Any]:
    return {
        "key": "live-ask-resume-p3l13",
        "request": "I'm cooking dinner for a friend who has a food allergy. "
        "Suggest something with chicken.",
        "scripted_answers": [{"question_id": "q-allergy", "answer": "She is allergic to peanuts."}],
        "expected": {"stop_reason": "agent_sufficient_evidence", "allergy_check": True},
    }


def _allergy_final(quantities: list[dict[str, Any]], note: str = "fake finish") -> dict[str, Any]:
    return {
        "options": [
            {
                "dataset_id": "odunola/foodie",
                "source_id": "curry-1",
                "title": "Creamy Chicken Curry",
                "quantities": quantities,
            }
        ],
        "note": note,
        "constraints_honored": [],
    }


def _confirmed() -> list[dict[str, Any]]:
    return [{"question_id": "q-allergy", "answer": "She is allergic to peanuts."}]


# --- allergy grade -------------------------------------------------------------


def test_allergy_grade_passes_without_peanut() -> None:
    store = _FakeStore(events=[_question_event(), _answer_event()], confirmed=_confirmed())
    final = _allergy_final([{"ingredient": "chicken", "amount": "500", "unit": "g"}])
    grades = live_run.grade_attempt(
        _allergy_scenario(), final, "agent_sufficient_evidence", store, "ses-1"
    )
    assert grades["allergy"]["asked"] is True
    assert grades["allergy"]["answer_recorded"] is True
    assert grades["allergy"]["resumed_with_options"] is True
    assert grades["allergy"]["no_peanut_options"] is True
    assert grades["allergy_pass"] is True


def test_allergy_grade_not_exercised_when_model_does_not_ask() -> None:
    store = _FakeStore(events=[], confirmed=[])
    final = _allergy_final([{"ingredient": "chicken", "amount": "500", "unit": "g"}])
    grades = live_run.grade_attempt(
        _allergy_scenario(), final, "agent_sufficient_evidence", store, "ses-1"
    )
    assert grades["allergy"] == "not-exercised: model did not ask"
    assert "allergy_pass" not in grades


def test_allergy_grade_fails_on_peanut_option() -> None:
    store = _FakeStore(events=[_question_event(), _answer_event()], confirmed=_confirmed())
    final = _allergy_final(
        [
            {"ingredient": "chicken", "amount": "500", "unit": "g"},
            {"ingredient": "peanut butter", "amount": "20", "unit": "g"},
        ]
    )
    grades = live_run.grade_attempt(
        _allergy_scenario(), final, "agent_sufficient_evidence", store, "ses-1"
    )
    assert grades["allergy"]["no_peanut_options"] is False
    assert grades["allergy"]["peanut_lines"] == ["Creamy Chicken Curry: peanut butter"]
    assert grades["allergy_pass"] is False


def test_allergy_grade_fails_on_plural_peanuts() -> None:
    store = _FakeStore(events=[_question_event(), _answer_event()], confirmed=_confirmed())
    final = _allergy_final([{"ingredient": "roasted peanuts", "amount": "30", "unit": "g"}])
    grades = live_run.grade_attempt(
        _allergy_scenario(), final, "agent_sufficient_evidence", store, "ses-1"
    )
    assert grades["allergy"]["no_peanut_options"] is False
    assert grades["allergy_pass"] is False


def test_allergy_mention_recorded() -> None:
    store = _FakeStore(events=[_question_event(), _answer_event()], confirmed=_confirmed())
    final = _allergy_final(
        [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
        note="Avoiding peanuts throughout per the allergy answer.",
    )
    grades = live_run.grade_attempt(
        _allergy_scenario(), final, "agent_sufficient_evidence", store, "ses-1"
    )
    assert grades["allergy"]["allergy_mentioned"] is True
    assert grades["allergy_pass"] is True


# --- preflight acknowledgment rule ----------------------------------------------


def test_preflight_ack_required_when_search_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    ok, problems, record = live_run.preflight(_args(), _settings(), _search_on_scenarios())
    assert ok is False
    assert any("acknowledge-search-estimate" in p for p in problems)
    assert record["search_selected"] is True


def test_preflight_ack_rejects_wrong_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    ok, problems, _ = live_run.preflight(
        _args(acknowledge_search_estimate="yes"),
        _settings(),
        _search_on_scenarios(),
    )
    assert ok is False
    assert any("acknowledge-search-estimate" in p for p in problems)


def test_preflight_ack_exact_value_passes_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    ok, problems, record = live_run.preflight(
        _args(acknowledge_search_estimate=live_run.SEARCH_ACK_VALUE),
        _settings(),
        _search_on_scenarios(),
    )
    assert not any("acknowledge-search-estimate" in p for p in problems)
    assert record["acknowledge_search_estimate"] == live_run.SEARCH_ACK_VALUE


def test_preflight_no_ack_needed_when_search_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    _ok, problems, record = live_run.preflight(_args(), _settings(), _search_off_scenarios())
    assert record["search_selected"] is False
    assert not any("acknowledge-search-estimate" in p for p in problems)
    assert not any("campaign search cap" in p for p in problems)


# --- budget pools -----------------------------------------------------------------


def test_preflight_records_phase3_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    _ok, _problems, record = live_run.preflight(
        _args(budget_pool="phase3"), _settings(), _search_off_scenarios()
    )
    assert record["budget_pool"] == "phase3"
    assert record["pool_cap_usd"] == pytest.approx(0.15)
    assert record["pool_history"].endswith("phase3-live/spend-history.json")


def test_preflight_records_phase5_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    _ok, _problems, record = live_run.preflight(
        _args(budget_pool="phase5", ceiling_usd=0.10),
        _settings(),
        _search_off_scenarios(),
    )
    assert record["budget_pool"] == "phase5"
    assert record["pool_cap_usd"] == pytest.approx(live_run.PHASE5_CAP_USD)
    assert record["pool_history"].endswith("phase5-live/spend-history.json")


def test_preflight_refuses_ceiling_above_pool_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    ok, problems, _ = live_run.preflight(
        _args(budget_pool="phase5", ceiling_usd=live_run.PHASE5_CAP_USD + 0.01),
        _settings(),
        _search_off_scenarios(),
    )
    assert ok is False
    assert any("phase5 pool cap" in p for p in problems)


def test_preflight_refuses_first_reservation_over_pool_remaining(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    history = tmp_path / "spend-history.json"
    history.write_text(
        json.dumps(
            {
                "ceiling_usd": 0.15,
                "runs": [
                    {
                        "run_utc": "2026-09-30T12:00:00Z",
                        "attempt": 1,
                        "model": "gpt-6-luna",
                        "entries": [
                            {"label": "model-turn-1", "decision": "reconciled", "usd": 0.1495}
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ok, problems, _ = live_run.preflight(
        _args(budget_pool="phase3"),
        _settings(),
        _search_off_scenarios(),
        history_path=history,
    )
    assert ok is False
    assert any("phase3 pool budget" in p and "first turn" in p for p in problems)


# --- campaign search cap -------------------------------------------------------------


def _campaign_history(
    path: Path, n_searches: int, extra: list[dict[str, Any]] | None = None
) -> None:
    runs = [
        {
            "run_utc": f"2026-10-02T0{i}:00:00Z",
            "attempt": i + 1,
            "model": "gpt-6-luna",
            "entries": [
                {
                    "label": f"search-{i + 1}",
                    "decision": "reconciled",
                    "usd": 0.005,
                    "kind": "search",
                }
            ],
        }
        for i in range(n_searches)
    ]
    if extra:
        runs.append(
            {
                "run_utc": "2026-10-02T09:00:00Z",
                "attempt": 9,
                "model": "gpt-6-luna",
                "entries": extra,
            }
        )
    path.write_text(json.dumps({"ceiling_usd": 0.10, "runs": runs}), encoding="utf-8")


def test_count_campaign_searches_includes_ambiguous_not_refused(tmp_path: Path) -> None:
    history = tmp_path / "spend-history.json"
    _campaign_history(
        history,
        2,
        extra=[
            {
                "label": "search-3",
                "decision": "kept-ambiguous",
                "reserved_usd": 0.025,
                "kind": "search",
            },
            {"label": "search-4", "decision": "refused", "reserved_usd": 0.025, "kind": "search"},
            {"label": "model-turn-1", "decision": "reconciled", "usd": 0.001},
        ],
    )
    assert live_run.count_campaign_searches(history) == 3


def test_preflight_refuses_when_campaign_cap_reached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    history = tmp_path / "spend-history.json"
    _campaign_history(history, live_run.PHASE5_CAMPAIGN_SEARCH_CAP)
    monkeypatch.setattr(live_run, "PHASE5_HISTORY", history)
    ok, problems, record = live_run.preflight(
        _args(acknowledge_search_estimate=live_run.SEARCH_ACK_VALUE),
        _settings(),
        _search_on_scenarios(),
    )
    assert ok is False
    assert any("campaign search cap reached" in p for p in problems)
    assert record["prior_campaign_searches"] == live_run.PHASE5_CAMPAIGN_SEARCH_CAP


def test_preflight_refuses_max_campaign_overflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    history = tmp_path / "spend-history.json"
    _campaign_history(history, live_run.PHASE5_CAMPAIGN_SEARCH_CAP - 1)
    monkeypatch.setattr(live_run, "PHASE5_HISTORY", history)
    ok, problems, _ = live_run.preflight(
        _args(
            acknowledge_search_estimate=live_run.SEARCH_ACK_VALUE,
            max_campaign_searches=2,
        ),
        _settings(),
        _search_on_scenarios(),
    )
    assert ok is False
    assert any("max-campaign-searches" in p for p in problems)


def test_preflight_accepts_first_run_of_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    history = tmp_path / "spend-history.json"
    history.write_text(json.dumps({"ceiling_usd": 0.10, "runs": []}), encoding="utf-8")
    monkeypatch.setattr(live_run, "PHASE5_HISTORY", history)
    _ok, problems, record = live_run.preflight(
        _args(
            budget_pool="phase5",
            ceiling_usd=0.10,
            acknowledge_search_estimate=live_run.SEARCH_ACK_VALUE,
            max_campaign_searches=1,
        ),
        _settings(),
        _search_on_scenarios(),
    )
    assert not any("campaign search cap" in p for p in problems)
    assert not any("max-campaign-searches" in p for p in problems)
    assert record["prior_campaign_searches"] == 0


# --- estimate breach -------------------------------------------------------------------


def test_estimate_breach_ack_flow(tmp_path: Path) -> None:
    history = tmp_path / "spend-history.json"
    history.write_text(
        json.dumps(
            {
                "ceiling_usd": 0.10,
                "runs": [
                    {
                        "run_utc": "2026-10-02T10:00:00Z",
                        "attempt": 1,
                        "model": "gpt-6-luna",
                        "entries": [
                            {
                                "label": "search-1",
                                "decision": "search_estimate_exceeded",
                                "usd": 0.03,
                                "kind": "search",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert live_run.find_unacknowledged_estimate_breach(history) == {
        "run_utc": "2026-10-02T10:00:00Z",
        "label": "search-1",
    }
    assert live_run.count_campaign_searches(history) == 1


def test_estimate_breach_acknowledged(tmp_path: Path) -> None:
    history = tmp_path / "spend-history.json"
    history.write_text(
        json.dumps(
            {
                "ceiling_usd": 0.10,
                "estimate_acknowledgments": [
                    {"run_utc": "2026-10-02T10:00:00Z", "label": "search-1", "by": "owner"}
                ],
                "runs": [
                    {
                        "run_utc": "2026-10-02T10:00:00Z",
                        "attempt": 1,
                        "model": "gpt-6-luna",
                        "entries": [
                            {
                                "label": "search-1",
                                "decision": "search_estimate_exceeded",
                                "usd": 0.03,
                                "kind": "search",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert live_run.find_unacknowledged_estimate_breach(history) is None


def test_preflight_refuses_unacknowledged_estimate_breach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    history = tmp_path / "spend-history.json"
    history.write_text(
        json.dumps(
            {
                "ceiling_usd": 0.10,
                "runs": [
                    {
                        "run_utc": "2026-10-02T10:00:00Z",
                        "attempt": 1,
                        "model": "gpt-6-luna",
                        "entries": [
                            {
                                "label": "search-1",
                                "decision": "search_estimate_exceeded",
                                "usd": 0.03,
                                "kind": "search",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_run, "PHASE5_HISTORY", history)
    ok, problems, _ = live_run.preflight(
        _args(acknowledge_search_estimate=live_run.SEARCH_ACK_VALUE),
        _settings(),
        _search_on_scenarios(),
    )
    assert ok is False
    assert any("unacknowledged search estimate breach" in p for p in problems)


class _FakeInnerSearch:
    def __init__(self, input_tokens: int | None, output_tokens: int | None) -> None:
        self._in = input_tokens
        self._out = output_tokens
        self.calls = 0

    async def complete_web_search(
        self,
        *,
        instruction: str,
        query: str,
        max_output_tokens: int | None = None,
        timeout: float | None = None,
    ) -> Any:
        from culinary_copilot.llm.client import WebSearchResult

        self.calls += 1
        return WebSearchResult(
            performed=True,
            parsed={"summary": "ok", "sources": []},
            model="gpt-6-luna",
            latency_ms=1,
            attempts=1,
            input_tokens=self._in,
            output_tokens=self._out,
        )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_ledgered_search_within_estimate() -> None:
    ledger = SpendLedger(model="gpt-6-luna", ceiling_usd=0.10)
    provider = live_run.LedgeredSearchProvider(_FakeInnerSearch(100, 50), ledger)
    result = _run(provider.complete_web_search(instruction="i", query="q"))
    assert result.performed is True
    report = provider.last_report
    assert report is not None
    assert report["within_estimate"] is True
    assert report["reported_input_tokens"] == 100
    assert report["reported_output_tokens"] == 50
    assert report["call_fee_usd"] == pytest.approx(0.01)
    assert set(report["raw_usage"]) == {
        "input_tokens",
        "output_tokens",
        "model",
        "response_id",
        "attempts",
    }
    assert ledger.count_search_dispatches() == 1


def test_ledgered_search_over_estimate_stops() -> None:
    from culinary_copilot.search.accounting import SearchEstimateExceeded

    ledger = SpendLedger(model="gpt-6-luna", ceiling_usd=0.10)
    provider = live_run.LedgeredSearchProvider(_FakeInnerSearch(200_000, 50), ledger)
    with pytest.raises(SearchEstimateExceeded) as exc_info:
        _run(provider.complete_web_search(instruction="i", query="q"))
    assert exc_info.value.report["within_estimate"] is False
    assert exc_info.value.report["reconciled_usd"] > exc_info.value.report["estimate_usd"]
    assert ledger.entries[-1]["decision"] == "search_estimate_exceeded"
    assert ledger.count_search_dispatches() == 1


def test_ledgered_search_ambiguous_counts() -> None:
    ledger = SpendLedger(model="gpt-6-luna", ceiling_usd=0.10)
    provider = live_run.LedgeredSearchProvider(_FakeInnerSearch(None, None), ledger)
    _run(provider.complete_web_search(instruction="i", query="q"))
    assert provider.last_report is not None
    assert provider.last_report["status"] == "kept-ambiguous"
    assert ledger.count_search_dispatches() == 1


def test_ledgered_search_refusal_is_budget_stop() -> None:
    ledger = SpendLedger(model="gpt-6-luna", ceiling_usd=0.001)
    provider = live_run.LedgeredSearchProvider(_FakeInnerSearch(100, 50), ledger)
    with pytest.raises(live_run.BudgetExhausted):
        _run(provider.complete_web_search(instruction="i", query="q"))
    assert ledger.count_search_dispatches() == 0


# --- fake end-to-end runs (disposable DB) ---------------------------------------


def _phase5_file() -> str:
    return str(
        Path(__file__).resolve().parents[1]
        / "evals"
        / "phase3_agent"
        / "live_scenarios_phase5.json"
    )


def test_step1_stops_after_first_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    from culinary_copilot.config import Settings

    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    db_url = f"{head}/culinary_test_phase5_step1"
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
            "--scenarios-file",
            _phase5_file(),
            "--scenarios",
            "live-search-missing-dish,live-search-toggle-off",
            "--max-attempts",
            "1",
            "--stop-after-first-search",
        ]
    )
    assert rc == 0
    summary = json.loads(summary_out.read_text(encoding="utf-8"))
    assert summary["stopped_early"]["reason"] == "step-1-complete: first search done"
    assert summary["stopped_early"]["after_scenario"] == "live-search-missing-dish"
    by_key = {s["key"]: s for s in summary["scenarios"]}
    assert by_key["live-search-missing-dish"]["searches_dispatched"] == 1
    assert by_key["live-search-toggle-off"]["status"].startswith("not_run")
    assert summary["budget_pool"] == "phase3"


def test_allergy_fake_run_grades_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    from culinary_copilot.config import Settings

    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    db_url = f"{head}/culinary_test_phase5_allergy"
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
            "--scenarios-file",
            _phase5_file(),
            "--scenarios",
            "live-ask-resume-p3l13",
            "--max-attempts",
            "1",
        ]
    )
    assert rc == 0
    summary = json.loads(summary_out.read_text(encoding="utf-8"))
    assert len(summary["scenarios"]) == 1
    grades = summary["scenarios"][0]["grades"]
    assert grades["allergy"]["asked"] is True
    assert grades["allergy"]["answer_recorded"] is True
    assert grades["allergy"]["no_peanut_options"] is True
    assert grades["allergy_pass"] is True


def test_web_discovery_answers_without_asking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run-2 framing: after a successful search for a dish missing
    locally, the agent answers with web_answer (clickable refs) and
    does not ask whether the user wants a web result."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    from culinary_copilot.config import Settings

    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    db_url = f"{head}/culinary_test_phase5_webanswer"
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
            "--scenarios-file",
            _phase5_file(),
            "--scenarios",
            "live-search-missing-dish",
            "--max-attempts",
            "1",
        ]
    )
    assert rc == 0
    summary = json.loads(summary_out.read_text(encoding="utf-8"))
    grades = summary["scenarios"][0]["grades"]
    assert grades["web_answer"] is True
    assert grades["web_refs_clickable"] is True
    assert grades["asked"] is False
    assert grades["web_answer"] is True
    assert grades["scenario_pass"] is True
    assert "no question asked" in grades["scenario_pass_reason"]
    report = json.loads((raw_dir / "live-search-missing-dish.json").read_text(encoding="utf-8"))
    final = (report["final_attempt"] or {})["runs"][-1]["final"]
    assert final.get("web_answer", {}).get("web_refs"), "web_answer with refs produced"
    assert not final.get("question"), "no question instead of the web answer"
    trail = [item for session in report["trajectory"] for item in session["events"]]
    assert not [i for i in trail if i["type"] == "agent_question"], "never asked"
    search_types = {i["type"] for i in trail}
    assert {
        "search_slot_claimed",
        "search_requested",
        "search_results_retrieved",
        "evidence_evaluated",
        "search_outcome",
        "search_operations",
    } <= search_types, "search events projected in the raw trail"
    tool_calls = [i for i in trail if i["type"] == "tool_call" and i.get("tool") == "search_web"]
    assert tool_calls and tool_calls[0].get("result_facts", {}).get("source_count") == 1


# --- in-run search enforcement (2026-10-03 overrun fix) ---------------------------


def test_search_run_limits_check_and_claim() -> None:
    limits = live_run.SearchRunLimits(
        max_per_session=1, max_this_run=1, campaign_cap=4, prior_campaign_searches=0
    )
    assert limits.check_and_claim() == (True, "")
    allowed, reason = limits.check_and_claim()
    assert allowed is False
    assert "run limit" in reason


def test_search_run_limits_campaign_cap_counts_prior() -> None:
    limits = live_run.SearchRunLimits(
        max_per_session=3, max_this_run=2, campaign_cap=4, prior_campaign_searches=3
    )
    assert limits.check_and_claim() == (True, "")
    allowed, reason = limits.check_and_claim()
    assert allowed is False
    assert "campaign cap" in reason


@pytest.fixture(scope="module")
def direct_engine():
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError

    from culinary_copilot.config import Settings

    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    name = "culinary_test_phase5_direct"
    try:
        maint = create_engine(f"{head}/postgres", isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        maint.dispose()
        eng = create_engine(f"{head}/{name}")
        from culinary_copilot.recipes import import_data

        with eng.begin() as conn:
            import_data.apply_migrations(conn)
        yield eng
        eng.dispose()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL unavailable for search-limit tests: {exc!r}")
    finally:
        try:
            maint = create_engine(f"{head}/postgres", isolation_level="AUTOCOMMIT")
            with maint.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            maint.dispose()
        except Exception:
            pass


def _triple_scenario() -> dict[str, Any]:
    """Fake model requests 3 searches in one session, then answers."""
    return {
        "key": "live-web-triple-probe",
        "request": "How do I make okonomiyaki? I cannot find it in the app.",
        "session": {"internet_search_allowed": True},
        "settings": {},
        "scripted_answers": [],
        "flow": ["recommend"],
        "fake_flow": "web-triple",
        "expected": {"stop_reason": "agent_sufficient_evidence", "web_answer": True},
    }


def _run_triple(
    direct_engine: Any,
    tmp_path: Path,
    limits: Any,
) -> tuple[dict[str, Any], list[Any]]:
    from culinary_copilot.services.session_store import PostgresSessionStore

    store = PostgresSessionStore(direct_engine)
    scenario = _triple_scenario()
    settings = _settings()
    ledger = SpendLedger(model="gpt-6-luna", ceiling_usd=0.15)
    report = live_run.run_scenario_live(
        engine=direct_engine,
        store=store,
        settings=settings,
        scenario=scenario,
        ledger=ledger,
        provider_factory=lambda current: live_run.FakeRunProvider(current),
        context_factory=lambda s, current: live_run._fake_context(s, settings, current),
        raw_dir=tmp_path,
        max_attempts=1,
        recipe_resolver=lambda ds, sid: dict(live_run._FAKE_DOCS.get((ds, sid)) or {}) or None,
        search_limits=limits,
    )
    session_id = report["sessions"][0]
    return report, store.list_events(session_id)


def _event_types(events: list[Any], event_type: str) -> list[Any]:
    return [e for e in events if getattr(e, "event_type", "") == event_type]


def test_session_limit_allows_one_of_three(direct_engine: Any, tmp_path: Path) -> None:
    """Flags 1 and 1 through the full live path (fake SDK, real
    runner): 3 requested searches, exactly 1 provider dispatch, the
    other 2 refused with search_budget_exhausted."""
    limits = live_run.SearchRunLimits(
        max_per_session=1, max_this_run=1, campaign_cap=4, prior_campaign_searches=0
    )
    report, events = _run_triple(direct_engine, tmp_path, limits)
    assert report["stop_reason"] == "agent_sufficient_evidence"
    claims = _event_types(events, "search_slot_claimed")
    assert len(claims) == 1
    assert claims[0].payload["slots_max"] == 1, "flag reached the slot claim"
    assert len(_event_types(events, "search_results_retrieved")) == 1
    refused_calls = [
        e
        for e in _event_types(events, "tool_call")
        if (e.payload.get("tool") == "search_web")
        and (e.payload.get("reason") == "search_budget_exhausted")
    ]
    assert len(refused_calls) == 2
    assert report["grades"]["web_answer"] is True


def test_campaign_hook_refuses_past_run_limit(direct_engine: Any, tmp_path: Path) -> None:
    """Session slots allow 3 but the run allows 1: the second dispatch
    is refused by the campaign hook (outcome refused, no provider
    call) after a successful slot claim."""
    limits = live_run.SearchRunLimits(
        max_per_session=3, max_this_run=1, campaign_cap=4, prior_campaign_searches=0
    )
    report, events = _run_triple(direct_engine, tmp_path, limits)
    assert report["stop_reason"] == "agent_sufficient_evidence"
    assert len(_event_types(events, "search_slot_claimed")) == 3
    assert len(_event_types(events, "search_results_retrieved")) == 1
    refused = [
        e for e in _event_types(events, "search_outcome") if e.payload.get("outcome") == "refused"
    ]
    assert len(refused) == 2
    assert all("run limit" in str(e.payload.get("reason") or "") for e in refused)
    refused_calls = [
        e
        for e in _event_types(events, "tool_call")
        if (e.payload.get("tool") == "search_web")
        and (e.payload.get("reason") == "search_budget_exhausted")
    ]
    assert len(refused_calls) == 2


def test_campaign_cap_with_prior_history(direct_engine: Any, tmp_path: Path) -> None:
    """Prior history at 3 with --max-campaign-searches 2: at most 1
    search dispatches before the campaign cap refuses."""
    limits = live_run.SearchRunLimits(
        max_per_session=3, max_this_run=2, campaign_cap=4, prior_campaign_searches=3
    )
    report, events = _run_triple(direct_engine, tmp_path, limits)
    assert report["stop_reason"] == "agent_sufficient_evidence"
    assert len(_event_types(events, "search_results_retrieved")) == 1
    refused = [
        e for e in _event_types(events, "search_outcome") if e.payload.get("outcome") == "refused"
    ]
    assert len(refused) == 2
    assert all("campaign cap" in str(e.payload.get("reason") or "") for e in refused)


def test_preflight_refuses_per_session_above_owner_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--search-max-per-live-session above 2 is refused in preflight
    (owner item 3); the in-run claim takes min(code limit, flag)."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    ok, problems, _ = live_run.preflight(
        _args(search_max_per_live_session=3),
        _settings(),
        _search_on_scenarios(),
    )
    assert ok is False
    assert any("--search-max-per-live-session 3 exceeds" in p for p in problems)


def test_discovery_grade_explicit_fields() -> None:
    """asked/web_answer/scenario_pass replace the bare asking_is_fail flag."""
    scenario = {
        "key": "live-search-missing-dish",
        "request": "How do I make okonomiyaki?",
        "expected": {
            "stop_reason": "agent_sufficient_evidence",
            "web_answer": True,
            "asking_is_fail": True,
        },
    }
    web_final = {
        "web_answer": {
            "text": "Okonomiyaki guide linked.",
            "web_refs": [{"url": "https://example.com/x", "title": "Guide"}],
        }
    }
    asked_final = {
        "question": {
            "question_id": "q-1",
            "question_text": "Want a web recipe?",
            "options": ["yes", "no"],
        }
    }
    store = _FakeStore(events=[], confirmed=[])
    passed = live_run.grade_attempt(scenario, web_final, "agent_sufficient_evidence", store, "s")
    assert passed["asked"] is False
    assert passed["web_answer"] is True
    assert passed["scenario_pass"] is True
    assert passed["scenario_pass_reason"] == "web answer given, no question asked"
    failed = live_run.grade_attempt(scenario, asked_final, "agent_needs_user_input", store, "s")
    assert failed["asked"] is True
    assert failed["web_answer"] is False
    assert failed["scenario_pass"] is False
    assert "asked instead" in failed["scenario_pass_reason"]


def test_unknown_flow_steps_refused(direct_engine: Any, tmp_path: Path) -> None:
    """The runner refuses unknown flow steps instead of running them
    as a plain recommend (toggle-off has no live implementation)."""
    from culinary_copilot.services.session_store import PostgresSessionStore

    store = PostgresSessionStore(direct_engine)
    scenario = {
        "key": "live-search-toggle-off",
        "request": "How do I make okonomiyaki?",
        "session": {"internet_search_allowed": True},
        "settings": {},
        "scripted_answers": [],
        "flow": ["recommend-toggle-off", "resume"],
        "fake_flow": "web-toggle",
        "expected": {"stop_reason": "agent_sufficient_evidence"},
    }
    settings = _settings()
    report = live_run.run_scenario_live(
        engine=direct_engine,
        store=store,
        settings=settings,
        scenario=scenario,
        ledger=SpendLedger(model="gpt-6-luna", ceiling_usd=0.15),
        provider_factory=lambda current: live_run.FakeRunProvider(current),
        context_factory=lambda s, current: live_run._fake_context(s, settings, current),
        raw_dir=tmp_path,
        max_attempts=1,
    )
    assert report["status"] == "stopped: runner-error"
    assert report["sessions"] == []
    assert report["grades"]["reason"] == "unknown-flow-steps"
    assert "recommend-toggle-off" in report["run_stop"]["detail"]


def test_timed_out_search_kept_ambiguous_and_recorded(
    direct_engine: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-03 ledger defect: a dispatched search that hits the tool
    timeout is kept-ambiguous at its $0.025 reservation (never $0),
    and spend-history records it. The timed-out tool_call keeps its
    minimized args."""
    import asyncio as _asyncio

    from culinary_copilot.config import Settings
    from culinary_copilot.services.session_store import PostgresSessionStore

    class _SleepingInner:
        """Fake search sub-request that sleeps past the tool timeout."""

        def __init__(self) -> None:
            self.calls = 0

        async def complete_web_search(
            self,
            *,
            instruction: str,
            query: str,
            max_output_tokens: int | None = None,
            timeout: float | None = None,
        ) -> Any:
            self.calls += 1
            await _asyncio.sleep(5.0)
            raise AssertionError("must time out first")

    store = PostgresSessionStore(direct_engine)
    scenario = {
        "key": "live-search-missing-dish",
        "request": "How do I make okonomiyaki? I cannot find it in the app.",
        "session": {"internet_search_allowed": True},
        "settings": {},
        "scripted_answers": [],
        "flow": ["recommend"],
        "fake_flow": "web-discovery",
        "expected": {"stop_reason": "agent_sufficient_evidence", "web_answer": True},
    }
    settings = live_run._effective_settings(Settings(_env_file=None, tool_timeout_s=0.3))
    ledger = SpendLedger(model="gpt-6-luna", ceiling_usd=0.15)
    sleeping = _SleepingInner()

    def _context_factory(s: Any, current: dict[str, Any]) -> Any:
        context = live_run._fake_context(s, settings, current)
        # Live accounting path (not the free fake): reserve, then the
        # tool timeout cancels the sleeping request mid-flight.
        context.search_provider = live_run.LedgeredSearchProvider(
            sleeping, ledger, model="gpt-6-luna"
        )
        return context

    report = live_run.run_scenario_live(
        engine=direct_engine,
        store=store,
        settings=settings,
        scenario=scenario,
        ledger=ledger,
        provider_factory=lambda current: live_run.FakeRunProvider(current),
        context_factory=_context_factory,
        raw_dir=tmp_path,
        max_attempts=1,
        recipe_resolver=lambda ds, sid: dict(live_run._FAKE_DOCS.get((ds, sid)) or {}) or None,
    )
    assert sleeping.calls == 1, "exactly one provider dispatch happened"
    assert report["stop_reason"] != "agent_sufficient_evidence"
    search_entries = [e for e in ledger.entries if e.get("kind") == "search"]
    assert len(search_entries) == 1
    assert search_entries[0]["decision"] == "kept-ambiguous"
    assert search_entries[0]["reserved_usd"] == 0.025
    assert ledger.count_search_dispatches() == 1
    history = tmp_path / "spend-history.json"
    total = live_run.append_spend_history(
        history, model="gpt-6-luna", entries=list(ledger.entries), ceiling_usd=0.15
    )
    body = json.loads(history.read_text(encoding="utf-8"))
    stored = [e for r in body["runs"] for e in r["entries"]]
    assert not [e for e in stored if e.get("decision") == "reserved"]
    assert any(
        e.get("label") == "search-1" and e.get("decision") == "kept-ambiguous" for e in stored
    )
    assert total == sum(float(e.get("usd") or 0.0) for e in stored)
    session_id = report["sessions"][0]
    tool_calls = [
        e
        for e in store.list_events(session_id)
        if getattr(e, "event_type", "") == "tool_call" and (e.payload.get("tool") == "search_web")
    ]
    assert tool_calls, "timed-out search has a tool_call event"
    assert tool_calls[0].payload.get("reason") == "tool_timeout"
    timeout_args = tool_calls[0].payload.get("args") or {}
    if isinstance(timeout_args, str):
        timeout_args = json.loads(timeout_args)
    assert timeout_args.get("query") == "okonomiyaki recipe"


def test_run_end_sweep_converts_leftover_reserved() -> None:
    ledger = SpendLedger(model="gpt-6-luna", ceiling_usd=0.15)
    assert ledger.reserve("model-turn-1", input_tokens=100, max_output=50) is True
    assert ledger.reserve_search("search-1", estimate_usd=0.025) is True
    ledger.reconcile("model-turn-1", reported_in=50, reported_out=10)
    assert ledger.sweep_reserved(note="run end") == 1
    left = [e for e in ledger.entries if e.get("decision") == "reserved"]
    assert left == []
    search = next(e for e in ledger.entries if e.get("label") == "search-1")
    assert search["decision"] == "kept-ambiguous"
    assert search["swept"] is True
    assert ledger.sweep_reserved() == 0


def test_append_spend_history_refuses_reserved(tmp_path: Path) -> None:
    history = tmp_path / "spend-history.json"
    with pytest.raises(ValueError, match="sweep_reserved"):
        live_run.append_spend_history(
            history,
            model="gpt-6-luna",
            entries=[{"label": "search-1", "decision": "reserved", "kind": "search"}],
            ceiling_usd=0.15,
        )
    assert not history.exists()


def test_preflight_remainder_uses_corrected_total(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pool remainder counts correction entries (kept-ambiguous usd)."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    history = tmp_path / "spend-history.json"
    history.write_text(
        json.dumps(
            {
                "ceiling_usd": 0.10,
                "runs": [
                    {
                        "run_utc": "2026-10-03T21:41:49Z",
                        "attempt": 3,
                        "model": "gpt-6-luna",
                        "entries": [
                            {
                                "label": "model-turn-1",
                                "decision": "reconciled",
                                "usd": 0.05,
                                "kind": "chat",
                            },
                            {
                                "label": "search-1-correction",
                                "decision": "kept-ambiguous",
                                "usd": 0.025,
                                "kind": "search",
                                "note": "timed out after dispatch; corrected 2026-10-03",
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_run, "PHASE5_HISTORY", history)
    _ok, _problems, record = live_run.preflight(
        _args(
            budget_pool="phase5",
            ceiling_usd=0.10,
            acknowledge_search_estimate=live_run.SEARCH_ACK_VALUE,
        ),
        _settings(),
        _search_on_scenarios(),
        history_path=history,
    )
    assert record["prior_recorded_spend_usd"] == pytest.approx(0.075)


def test_fake_summary_flags_swept_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run summaries flag the run-end sweep count (0 when clean)."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    from culinary_copilot.config import Settings

    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    rc = live_run.main(
        [
            "--fake",
            "--database-url",
            f"{head}/culinary_test_phase5_swept",
            "--raw-dir",
            str(tmp_path / "raw"),
            "--summary-out",
            str(tmp_path / "summary.json"),
            "--scenarios-file",
            _phase5_file(),
            "--scenarios",
            "live-search-off",
            "--max-attempts",
            "1",
        ]
    )
    assert rc == 0
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["spend"]["swept_ambiguous"] == 0


def test_old_summary_without_sources_breaks_web_answer(
    direct_engine: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-03 root cause, pinned: with _summarize_result lacking
    the search_web branch, the honest fake finds no URL in its tool
    output, so no web_answer is accepted. (Conversely, the honest
    web-discovery tests above fail if the branch is ever removed.)"""
    from culinary_copilot.agent import loop as _loop
    from culinary_copilot.services.session_store import PostgresSessionStore

    real_summarize = _loop._summarize_result

    def _old_summarize(name: str, result: dict[str, Any]) -> dict[str, Any]:
        if name == "search_web":
            return {
                "tool": name,
                "ok": bool(result.get("ok")),
                "error_type": result.get("error_type"),
                "reason": result.get("reason"),
                "message": str(result.get("message") or "")[:300],
            }
        return real_summarize(name, result)

    monkeypatch.setattr(_loop, "_summarize_result", _old_summarize)
    store = PostgresSessionStore(direct_engine)
    scenario = {
        "key": "live-search-missing-dish",
        "request": "How do I make okonomiyaki? I cannot find it in the app.",
        "session": {"internet_search_allowed": True},
        "settings": {},
        "scripted_answers": [],
        "flow": ["recommend"],
        "fake_flow": "web-discovery",
        "expected": {
            "stop_reason": "agent_sufficient_evidence",
            "web_answer": True,
            "asking_is_fail": True,
        },
    }
    settings = _settings()
    report = live_run.run_scenario_live(
        engine=direct_engine,
        store=store,
        settings=settings,
        scenario=scenario,
        ledger=SpendLedger(model="gpt-6-luna", ceiling_usd=0.15),
        provider_factory=lambda current: live_run.FakeRunProvider(current),
        context_factory=lambda s, current: live_run._fake_context(s, settings, current),
        raw_dir=tmp_path,
        max_attempts=1,
        recipe_resolver=lambda ds, sid: dict(live_run._FAKE_DOCS.get((ds, sid)) or {}) or None,
    )
    assert report["stop_reason"] != "agent_sufficient_evidence"
    assert report["grades"].get("web_answer") is False
    assert report["grades"].get("scenario_pass") is False
