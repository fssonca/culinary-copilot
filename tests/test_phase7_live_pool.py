"""Phase 7 budget pool preflight (offline, no model calls)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals" / "phase3_agent"))

import live_run

from culinary_copilot.config import Settings


def _settings() -> Settings:
    return live_run._effective_settings(
        Settings(
            _env_file=None,
            llm_recommendation_enabled=True,
            epicure_enabled=True,
            llm_app_max_retries=0,
            llm_rec_max_retries=0,
            embed_max_retries=0,
        )
    )


def _args(**over: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "model": None,
        "max_attempts": 1,
        "live": True,
        "budget_pool": "phase7",
        "ceiling_usd": 0.50,
        "acknowledge_live_run": "",
        "acknowledge_search_estimate": live_run.SEARCH_ACK_VALUE,
        "search_max_per_live_session": 2,
        "max_campaign_searches": None,
        "max_model_turns": None,
        "database_url": None,
        "expect_db_name": "dummy",
        "expect_db_host": "localhost",
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_phase7_refuses_without_ack(monkeypatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    args = _args()
    ok, problems, _ = live_run.preflight(
        args, _settings(), scenarios={"scenarios": []}, history_path=None
    )
    assert not ok
    assert any("acknowledge-live-run" in p for p in problems)


def test_phase7_accepts_with_ack(monkeypatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    args = _args(
        acknowledge_live_run=live_run.PHASE7_ACK_VALUE,
        live=False,
        expect_db_name="dummy",
    )
    # Non-live with ack still records the flag; refusal is live-only.
    ok, problems, record = live_run.preflight(
        args, _settings(), scenarios={"scenarios": []}, history_path=None
    )
    assert record["acknowledge_live_run"] == live_run.PHASE7_ACK_VALUE
    assert not any("acknowledge-live-run" in p for p in problems)
    assert ok or any("DB guard" in p for p in problems)


def test_phase7_refuses_ceiling_above_cap(monkeypatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    args = _args(acknowledge_live_run=live_run.PHASE7_ACK_VALUE, ceiling_usd=5.0)
    ok, problems, _ = live_run.preflight(
        args, _settings(), scenarios={"scenarios": []}, history_path=None
    )
    assert not ok
    assert any("exceeds $0.50 phase7 pool cap" in p for p in problems)


def test_phase7_pool_registered() -> None:
    assert live_run.BUDGET_POOLS["phase7"]["cap_usd"] == 0.50
    assert str(live_run.BUDGET_POOLS["phase7"]["history"]).endswith(
        "phase7-live/spend-history.json"
    )


def test_phase7_search_cap_enforced_inside_run() -> None:
    # Same SearchRunLimits machinery the live run uses: the run-level
    # flag stops the third dispatch inside the run, not only at preflight.
    limits = live_run.SearchRunLimits(
        max_this_run=2, campaign_cap=live_run.PHASE7_CAMPAIGN_SEARCH_CAP
    )
    assert limits.check_and_claim() == (True, "")
    assert limits.check_and_claim() == (True, "")
    ok, reason = limits.check_and_claim()
    assert ok is False
    assert "run limit reached" in reason


def test_preflight_refuses_embeddings_off_with_db_embeddings() -> None:
    # Phase 7 live fix (vegan run): Checkpoint 0 lets the agent choose
    # vector, so a live run with embeddings off misrepresents while the
    # database has recipe embeddings.
    args = _args(live=True)
    ok, problems, record = live_run.preflight(
        args, _settings(), {"scenarios": []}, history_path=None, recipe_embeddings=16033
    )
    assert ok is False
    assert any("EMBEDDINGS_ENABLED is false" in p for p in problems)
    assert record["recipe_embeddings"] == 16033


def test_preflight_embeddings_gate_passes_when_on_or_empty() -> None:
    from culinary_copilot.config import Settings as _Settings

    on = live_run._effective_settings(_Settings(_env_file=None, embeddings_enabled=True))
    ok, problems, _ = live_run.preflight(
        _args(live=True), on, {"scenarios": []}, history_path=None, recipe_embeddings=16033
    )
    assert not any("EMBEDDINGS_ENABLED is false" in p for p in problems)
    ok, problems, _ = live_run.preflight(
        _args(live=False), _settings(), {"scenarios": []}, history_path=None
    )
    assert not any("EMBEDDINGS_ENABLED is false" in p for p in problems)


def test_phase7_ledger_refuses_over_cap() -> None:
    ledger = live_run.SpendLedger(model="gpt-6-luna", ceiling_usd=0.01)
    assert ledger.reserve(label="t1", input_tokens=100, max_output=100) is True
    # A reservation that does not fit the remainder refuses inside the run.
    assert ledger.reserve(label="t2", input_tokens=10_000_000, max_output=10_000_000) is False


def test_phase7_estimate_breach_reads_pool_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Known gap, fixed: the breach check reads the selected pool's
    # history, so a Phase 7 breach blocks Phase 7 runs.
    import json as _json

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    breach = {
        "ceiling_usd": 0.50,
        "runs": [
            {
                "run_utc": "2026-10-04T10:00:00Z",
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
    phase7_history = tmp_path / "phase7-spend-history.json"
    phase7_history.write_text(_json.dumps(breach), encoding="utf-8")
    monkeypatch.setattr(live_run, "PHASE7_HISTORY", phase7_history)
    args = _args(acknowledge_live_run=live_run.PHASE7_ACK_VALUE)
    scenarios = {
        "scenarios": [
            {
                "key": "s",
                "session": {"internet_search_allowed": True},
                "settings": {},
                "scripted_answers": [],
                "flow": ["recommend"],
                "expected": {"stop_reason": "agent_sufficient_evidence"},
            }
        ]
    }
    ok, problems, _ = live_run.preflight(args, _settings(), scenarios, history_path=None)
    assert ok is False
    assert any("unacknowledged search estimate breach" in p for p in problems)
    assert any(str(phase7_history) in p for p in problems)
