"""Runner hardening regression tests (offline; run3 crash findings).

Zero live calls. Local PostgreSQL reads only (SELECT via repository),
in-memory clarification stores, fixture boundaries, mock transports.
"""

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "recommendations_live"))

import runner as runner_module  # noqa: E402
from runner import RunState, execute_live_case, load_cases, main  # noqa: E402

from culinary_copilot.config import Settings  # noqa: E402
from culinary_copilot.llm.client import FakeApplicationProvider  # noqa: E402

CASES = Path(__file__).parent.parent / "evals" / "cases" / "phase3_live_cases.json"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_enabled": False,
        "llm_recommendation_enabled": True,
        "epicure_enabled": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _fixture_selection() -> dict[str, Any]:
    return {
        "candidate_label": "1",
        "ingredient_refs": ["ing-0", "ing-1"],
        "step_refs": ["step-0", "step-1", "step-2"],
        "reasons": [],
        "questions": [],
    }


def test_fixture_fidelity_does_not_crash(tmp_path: Path) -> None:
    """run3 crash: fidelity re-fetch through the real repo on SYNTHETIC ids."""
    cases = load_cases(CASES)
    case = next(c for c in cases if c.case_id == "LIVE-01")
    assert case.synthetic_fixture
    fake = FakeApplicationProvider(script=[_fixture_selection()])
    state = RunState(tmp_path / "state.json")
    state.load()
    record = execute_live_case(
        case=case,
        settings=_settings(),
        engine=object(),
        provider=fake,
        out_dir=tmp_path,
        state=state,
        prices={"input": 0.05, "output": 0.40},
        dry_payload_bytes=2338,
    )
    assert record["status_code"] == 200
    assert record["outcome"] == "recommendation"
    fidelity = record["fidelity"]
    assert fidelity["checked"] is True
    assert fidelity["match"] is True
    assert fidelity["synthetic"] is True


def test_selection_outside_corpus_is_grading_failure(tmp_path: Path) -> None:
    response = {
        "outcome": "recommendation",
        "selection": {"dataset_id": "ELSEWHERE/evil", "source_id": "x-1"},
        "recipe": {"ingredients": [], "instructions": []},
    }
    cases = load_cases(CASES)
    case = next(c for c in cases if c.case_id == "LIVE-01")
    fidelity = runner_module.check_fidelity(object(), response, case=case)
    assert fidelity["checked"] is True
    assert fidelity["match"] is False
    assert "outside" in str(fidelity.get("reason", ""))


def test_crash_after_http_persists_raw_and_marks_case(tmp_path: Path) -> None:
    """Grading crash must leave the raw response on disk, the case marked,
    and a summary behind — never a silent empty directory."""
    cases = load_cases(CASES)
    case = next(c for c in cases if c.case_id == "LIVE-02")

    from culinary_copilot.llm.client import FakeApplicationProvider as Fake

    script = [
        {
            "dataset_id": "AkashPS11/recipes_data_food.com",
            "source_id": "000159",
            "ingredient_refs": ["ing-0"],
            "step_refs": ["step-0"],
            "reasons": [],
            "questions": [],
        }
    ]

    def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("injected grading crash")

    search_all, search_recipes, get_recipe = _repo_patches_foodcom()
    state = RunState(tmp_path / "state.json")
    state.load()
    with (
        search_all,
        search_recipes,
        get_recipe,
        patch.object(runner_module, "check_fidelity", side_effect=_boom),
    ):
        record = execute_live_case(
            case=case,
            settings=_settings(),
            engine=object(),
            provider=Fake(script=script),
            out_dir=tmp_path,
            state=state,
            prices={"input": 0.05, "output": 0.40},
            dry_payload_bytes=6400,
        )
    assert record.get("grading_error", {}).get("type") == "RuntimeError"
    assert record.get("stop") is True
    on_disk = json.loads((tmp_path / "case-LIVE-02-live.json").read_text())
    assert on_disk["status_code"] == 200
    assert on_disk["grading_error"]["type"] == "RuntimeError"
    assert on_disk["response"]["outcome"] == "recommendation"
    assert state.case_record("LIVE-02")["live"]["grading_error"]["type"] == "RuntimeError"


def _repo_patches_foodcom():
    doc = {
        "title": "T",
        "servings": 4.0,
        "durations_minutes": {"TotalTime": 25.0},
        "ingredients": [
            {
                "canonical": "chicken",
                "original": "1 lb chicken",
                "amount": 1.0,
                "amount_text": "1",
                "quantity_text": "1 lb",
                "unit": "lb",
                "unit_text": "lb",
                "notes": "",
                "optional": False,
            },
        ],
        "instructions": ["Cook the chicken."],
        "provenance": {
            "dataset_id": "AkashPS11/recipes_data_food.com",
            "source_id": "000159",
        },
        "flags": [],
        "available_fields": {"servings": True},
        "quality_issues": [],
        "description": "T",
    }
    rows = [
        {
            "dataset_id": "AkashPS11/recipes_data_food.com",
            "source_id": "000159",
            "title": "T",
        }
    ]
    return (
        patch("culinary_copilot.recipes.repository.search_all", return_value=rows),
        patch("culinary_copilot.recipes.repository.search_recipes", return_value=rows),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=doc),
    )


def test_resume_refuses_submitting_without_flag(tmp_path: Path, monkeypatch: Any) -> None:
    """A crash between reservation and response leaves 'submitting': resume
    must refuse to resubmit (possible silent paid duplicate) without an
    explicit human reset."""
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key-for-resume-test")
    state = RunState(tmp_path / "state.json")
    state.load()
    # Seed the FIRST case: refusal must trigger before any submission.
    state.case_record("LIVE-01")["live"] = {
        "status": "submitting",
        "reservation_usd": 0.005,
    }
    state.save()
    rc = main(
        [
            "--cases",
            str(CASES),
            "--out",
            str(tmp_path),
            "--live",
            "--ceiling-usd",
            "1.00",
            "--price-input-per-1m",
            "0.05",
            "--price-output-per-1m",
            "0.40",
        ]
    )
    assert rc == 7
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["stop"] == "needs-review"
    assert summary["cases"]["LIVE-01"].get("status") == "submitting"


def test_rehearse_success_path(tmp_path: Path, monkeypatch: Any) -> None:
    """Full runner path, zero network: no key required, all 10 cases."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rc = main(
        [
            "--cases",
            str(CASES),
            "--out",
            str(tmp_path),
            "--rehearse",
        ]
    )
    assert rc == 0
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["stop"] is None
    assert len(summary["cases"]) == 10
    live01 = json.loads((tmp_path / "case-LIVE-01-live.json").read_text())
    assert live01["outcome"] == "recommendation"
    assert live01["fidelity"]["checked"] is True
    assert live01["fidelity"]["match"] is True
    assert live01["synthetic_fixture"] is True
    live10 = json.loads((tmp_path / "case-LIVE-10-live.json").read_text())
    assert live10["outcome"] == "recommendation"
    for cid in ("LIVE-03", "LIVE-04", "LIVE-05"):
        rec = json.loads((tmp_path / f"case-{cid}-live.json").read_text())
        assert rec["outcome"] == "insufficient_evidence"
        assert rec["paid"] is False


@pytest.mark.parametrize(
    "scenario,check_case,expected",
    [
        ("incomplete", "LIVE-02", {"error_reason": "truncated_incomplete_response"}),
        ("unavailable", "LIVE-02", {"error_reason": "provider_unavailable"}),
        ("bad-request", "LIVE-02", {"error_reason": "provider_bad_request"}),
        # internal-error stops at the first provider-reaching case (LIVE-01).
        ("internal", "LIVE-01", {"error_reason": "provider_internal_error"}),
    ],
)
def test_rehearse_failure_envelopes(
    tmp_path: Path, monkeypatch: Any, scenario: str, check_case: str, expected: dict[str, str]
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rc = main(
        [
            "--cases",
            str(CASES),
            "--out",
            str(tmp_path),
            "--rehearse",
            "--rehearse-scenario",
            scenario,
        ]
    )
    assert rc in (4, 5, 6)  # a stop rule fired, run did not silently pass
    record = json.loads((tmp_path / f"case-{check_case}-live.json").read_text())
    assert record["error_reason"] == expected["error_reason"]
    if scenario != "internal":
        assert record["provider_reached"] is True
    else:
        assert record["stop"] is True
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["stop"] is not None
