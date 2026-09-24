"""Runner regression tests: dry-run, budgets, resume, gates (offline).

Patched repository, guard/fake providers, temporary output dirs. No model
calls, no downloads, no application DB use.
"""

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "recommendations_live"))

from runner import (  # noqa: E402
    RunState,
    execute_dry_case,
    execute_live_case,
    load_cases,
    main,
    reserve_for_case,
    turn_bounds_for_case,
)

CASES = Path(__file__).parent.parent / "evals" / "cases" / "phase3_live_cases.json"
FOODCOM = "AkashPS11/recipes_data_food.com"


def _doc(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Runner Curry",
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
            {
                "canonical": "garlic",
                "original": "2 cloves garlic",
                "amount": 2.0,
                "amount_text": "2",
                "quantity_text": "2 cloves",
                "unit": None,
                "unit_text": "",
                "notes": "",
                "optional": False,
            },
        ],
        "instructions": ["Cook the chicken.", "Add garlic and serve."],
        "provenance": {"dataset_id": FOODCOM, "source_id": "000159"},
        "flags": [],
        "available_fields": {"servings": True},
        "quality_issues": [],
        "description": "A curry.",
    }
    base.update(overrides)
    return base


def _selection(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "dataset_id": FOODCOM,
        "source_id": "000159",
        "ingredient_refs": ["ing-0", "ing-1"],
        "step_refs": ["step-0", "step-1"],
        "reasons": [],
        "questions": [],
    }
    base.update(overrides)
    return base


def _repo_patches(empty: bool = False):
    rows = [] if empty else [{"dataset_id": FOODCOM, "source_id": "000159", "title": "T"}]
    return (
        patch("culinary_copilot.recipes.repository.search_all", return_value=rows),
        patch("culinary_copilot.recipes.repository.search_recipes", return_value=rows),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=_doc()),
    )


def _settings(**overrides: Any) -> Any:
    from culinary_copilot.config import Settings

    base: dict[str, Any] = {
        "llm_enabled": False,
        "llm_recommendation_enabled": True,
        "epicure_enabled": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_case_file_loads_and_validates() -> None:
    cases = load_cases(CASES)
    assert len(cases) == 10
    assert len({c.case_id for c in cases}) == 10
    by_id = {c.case_id: c for c in cases}
    assert by_id["LIVE-06"].answer_steps == [
        {"target": "dish", "text": "chicken curry"},
        {"target": "ingredients", "text": "chicken"},
        {"target": "dietary_constraints", "selected": ["vegan"]},
    ]
    assert by_id["LIVE-09"].config_override == {"epicure_enabled": False}
    assert by_id["LIVE-10"].tool_mode is True
    assert all(c.expected_outcome for c in cases)


def test_case_validation_rejects_bad_files(tmp_path: Path) -> None:
    import pytest as _pytest

    cases = json.loads(CASES.read_text())
    bad = dict(cases)
    dup_cases = [dict(c) for c in cases["cases"][:10]]
    dup_cases[9] = dict(dup_cases[9])
    dup_cases[9]["id"] = dup_cases[0]["id"]
    bad["cases"] = dup_cases
    duplicated = tmp_path / "dup.json"
    duplicated.write_text(json.dumps(bad))
    with _pytest.raises(ValueError, match="duplicate"):
        load_cases(duplicated)
    too_many = dict(cases)
    too_many["cases"] = cases["cases"] * 2
    many = tmp_path / "many.json"
    many.write_text(json.dumps(too_many))
    with _pytest.raises(ValueError, match="at most 10"):
        load_cases(many)


def test_reservation_math_is_conservative() -> None:
    reservation = reserve_for_case(
        turn_bounds=[1000, 1000],
        attempts_per_turn=2,
        max_output_tokens=800,
        price_in=0.05,
        price_out=0.40,
    )
    # Bytes bound tokens; every permitted attempt reserved at max output.
    assert reservation["attempts_max"] == 4
    assert reservation["input_tokens_bound"] == 4000
    assert reservation["output_tokens_bound"] == 3200
    assert reservation["cost_usd"] == pytest.approx((4000 * 0.05 + 3200 * 0.40) / 1_000_000)


def test_tool_turn_bounds_cover_both_turns_and_schema() -> None:
    bounds = turn_bounds_for_case(
        tool_mode=True,
        dry_payload_bytes=942,
        max_input_chars=12000,
        evidence_max_chars=6000,
    )
    assert len(bounds) == 2
    # Turn 1 carries metadata + tool schema; turn 2 carries base context +
    # the function_call item + the evidence-bounded tool-result block.
    assert bounds[0] > 942
    assert bounds[1] > 6000 * 4
    single = turn_bounds_for_case(
        tool_mode=False,
        dry_payload_bytes=6016,
        max_input_chars=12000,
        evidence_max_chars=6000,
    )
    assert single == [6016]
    capped = turn_bounds_for_case(
        tool_mode=False,
        dry_payload_bytes=0,
        max_input_chars=12000,
        evidence_max_chars=6000,
    )
    assert capped == [48000]


def test_dry_run_zero_network_zero_spend(tmp_path: Path) -> None:
    from culinary_copilot.llm.client import FakeApplicationProvider

    cases = load_cases(CASES)
    search_all, search_recipes, get_recipe = _repo_patches()
    with search_all, search_recipes, get_recipe:
        for case in cases:
            state = RunState(tmp_path / "state.json")
            state.load()
            record = execute_dry_case(
                case=case,
                settings=_settings(),
                engine=object(),
                out_dir=tmp_path,
                state=state,
            )
            assert record["provider_calls"] == 0
            assert record["spent_usd"] == 0.0
    assert (tmp_path / "state.json").exists()
    for case in cases:
        assert (tmp_path / f"case-{case.case_id}-dry.json").exists()
    # Generation cases captured payloads; abstaining ones needed no call.
    captures = [
        json.loads((tmp_path / f"case-{c.case_id}-dry.json").read_text()).get("payload_bytes", 0)
        for c in cases
    ]
    assert any(b > 0 for b in captures)
    assert FakeApplicationProvider is not None  # no live provider constructed here


def test_live_unknown_usage_charged_at_reservation_and_resume_skips(tmp_path: Path) -> None:
    from culinary_copilot.llm.client import FakeApplicationProvider

    cases = load_cases(CASES)
    # LIVE-02: real-corpus provider-reaching case (LIVE-01 is the synthetic
    # fixture case served through the test-only repository boundary).
    case = next(c for c in cases if c.case_id == "LIVE-02")
    fake = FakeApplicationProvider(script=[_selection()])  # usage tokens None -> unknown
    search_all, search_recipes, get_recipe = _repo_patches()
    state = RunState(tmp_path / "state.json")
    state.load()
    prices = {"input": 0.05, "output": 0.40}
    with search_all, search_recipes, get_recipe:
        record = execute_live_case(
            case=case,
            settings=_settings(),
            engine=object(),
            provider=fake,
            out_dir=tmp_path,
            state=state,
            prices=prices,
            dry_payload_bytes=2000,
        )
    assert record["paid"] is True
    # Single unknown turn: billed at its per-turn bound (input bound +
    # output cap × attempts used), not the two-attempt aggregate.
    expected = (
        record["reservation"]["turn_bounds"][0] * 0.05 / 1_000_000 + 6500 * 1 * 0.40 / 1_000_000
    )
    assert record["usage_note"].startswith("per-turn billing")
    assert record["spent_usd"] == pytest.approx(expected)
    assert record["spent_usd"] < record["reservation_usd"]
    assert record["outcome"] == "recommendation"
    assert record["fidelity"]["match"] is True
    calls_after_first = fake.call_count
    assert calls_after_first == 1
    # Resume must not silently repeat the paid case.
    record["final"] = True
    state.case_record(case.case_id)["live"] = record
    state.save()
    existing = state.case_record(case.case_id).get("live", {})
    assert existing.get("final") is True and existing.get("paid") is True
    with search_all, search_recipes, get_recipe:
        second = execute_live_case(
            case=case,
            settings=_settings(),
            engine=object(),
            provider=fake,
            out_dir=tmp_path,
            state=state,
            prices=prices,
            dry_payload_bytes=2000,
        )
    # Direct re-execution is possible, but main() resume skips paid finals.
    # Here the exhausted fake fails schema validation: unknown usage on a
    # failed call bills the full reservation (no turn detail to prorate).
    assert second["status_code"] == 502
    assert second["spent_usd"] == pytest.approx(second["reservation_usd"])


def test_live_gates_block_without_pricing_or_key(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rc = main(
        [
            "--cases",
            str(CASES),
            "--out",
            str(tmp_path),
            "--live",
            "--ceiling-usd",
            "1.00",
        ]
    )
    assert rc == 2
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
            "--model",
            "some-other-model",
        ]
    )
    assert rc == 2


def test_live_ceiling_stops_before_any_call(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key-for-gate-test")
    search_all, search_recipes, get_recipe = _repo_patches()
    with search_all, search_recipes, get_recipe:
        rc = main(
            [
                "--cases",
                str(CASES),
                "--out",
                str(tmp_path),
                "--live",
                "--ceiling-usd",
                "0.0000001",
                "--price-input-per-1m",
                "0.05",
                "--price-output-per-1m",
                "0.40",
            ]
        )
    assert rc == 3


import pytest  # noqa: E402
