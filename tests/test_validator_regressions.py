"""Saved live failures, not model-authored expected recipe ground truth."""

import json
from pathlib import Path

import pytest

from culinary_copilot.recipes.llm_validate import _batch_yield_numbers, _norm, validate_response


def saved_case(row):
    path = Path(__file__).parent / "fixtures/validator_regressions" / f"foodie-{row:06d}.json"
    return json.loads(path.read_text())


@pytest.mark.parametrize("row", [15068, 15907])
def test_exact_saved_false_positive(row):
    case = saved_case(row)
    assert case["before"]["verdict"] == "rejected_validation"
    report = validate_response(case["response"], **case["context"])
    assert report["verdict"] in ("accepted", "accepted_partial"), report


def test_fraction_equivalence_does_not_allow_invention():
    case = saved_case(15068)
    item = case["response"]["ingredients"][8]
    item["alternatives"] = ["3/4 teaspoon cayenne pepper or more"]
    report = validate_response(case["response"], **case["context"])
    assert report["verdict"] == "rejected_validation"
    assert "invented_alternative" in {p["code"] for p in report["problems"]}
    item["evidence"]["excerpt"] = "14.5 ounce imaginary ingredient"
    report = validate_response(case["response"], **case["context"])
    assert "fabricated_evidence" in {p["code"] for p in report["problems"]}


@pytest.mark.parametrize("a,b", [("½", "1/2"), ("1½", "1 1/2"), ("2¼ cups", "2 1/4 cups")])
def test_fraction_notation_only(a, b):
    assert _norm(a) == _norm(b)
    assert _norm(a) != _norm("14.5")
    assert _norm("14 ounce") != _norm("14.5 ounce")


@pytest.mark.parametrize(
    "line,count",
    [
        ("Makes 12 rolls.", 12),
        ("This recipe yields 8 buns.", 8),
        ("Ideally, you should be able to get 12 rolls from this recipe.", 12),
        ("Makes 2 dozen cookies.", 24),
        ("Makes 1.5 dozen cookies.", 18),
    ],
)
def test_explicit_item_yields(line, count):
    assert _batch_yield_numbers({"L1": line}) == {count}


@pytest.mark.parametrize(
    "line",
    [
        "Serves 12.",
        "Bake for 12 minutes.",
        "12 rolls",
        "Makes 12 servings.",
        "Makes 12-14 rolls.",
        "Makes 0 rolls.",
        "Makes 12 cups flour.",
        "Get 12 rolls from the shop.",
        "You will not get 12 rolls from this recipe.",
        "Makes 1.1 dozen cookies.",
    ],
)
def test_non_yield_and_inexact_dozen_rejected(line):
    assert _batch_yield_numbers({"L1": line}) == set()


def test_yield_number_must_match_and_does_not_create_servings():
    case = saved_case(15907)
    assert case["response"]["servings"] is None
    case["response"]["batch_yield_count"] = 999
    report = validate_response(case["response"], **case["context"])
    assert report["verdict"] == "rejected_validation"
    assert "batch_yield_mismatch" in {p["code"] for p in report["problems"]}
