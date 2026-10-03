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
    assert record["pool_cap_usd"] == pytest.approx(0.10)
    assert record["pool_history"].endswith("phase5-live/spend-history.json")


def test_preflight_refuses_ceiling_above_pool_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    ok, problems, _ = live_run.preflight(
        _args(budget_pool="phase5", ceiling_usd=0.11),
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
    _campaign_history(history, 4)
    monkeypatch.setattr(live_run, "PHASE5_HISTORY", history)
    ok, problems, record = live_run.preflight(
        _args(acknowledge_search_estimate=live_run.SEARCH_ACK_VALUE),
        _settings(),
        _search_on_scenarios(),
    )
    assert ok is False
    assert any("campaign search cap reached" in p for p in problems)
    assert record["prior_campaign_searches"] == 4


def test_preflight_refuses_max_campaign_overflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    history = tmp_path / "spend-history.json"
    _campaign_history(history, 3)
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
        self, *, instruction: str, query: str, max_output_tokens: int | None = None
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
