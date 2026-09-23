"""Source-backed regression cases for targeted ingestion (offline only)."""

import copy
import json
from pathlib import Path

import pytest

from culinary_copilot.recipes.adapters.foodie import normalize_foodie_text
from culinary_copilot.recipes.llm_batch import build_request_brief
from culinary_copilot.recipes.llm_contracts import ExtractionResponse, numbered_source
from culinary_copilot.recipes.llm_validate import merge_response, validate_response
from culinary_copilot.recipes.source_scope import ingredient_sources, requested_lines


def case(row):
    f = json.loads((Path(__file__).parent / "fixtures/llm_expected" / f"{row}.json").read_text())
    text = f["source"]["texts"]
    _, lines = numbered_source(text)
    try:
        det = normalize_foodie_text(text, row)
    except ValueError:
        det = None
    ctx = dict(
        source_id=f["expected"]["source_id"],
        content_hash=f["source"]["content_hash"],
        line_map=lines,
        ingredient_line_ids=list(lines),
        step_line_ids=[],
        request_versions={},
        current_versions={},
        baseline={
            "has_deterministic": det is not None,
            "has_steps": det is not None,
            "has_groups": det is not None,
            "requested_ingredient_line_ids": requested_lines(det, lines) if det else None,
        },
    )
    return f["expected"], det, ctx


def test_only_requested_ingredients_need_coverage_and_overlay():
    response, det, ctx = case(3423)
    before = copy.deepcopy(det)
    assert ctx["baseline"]["requested_ingredient_line_ids"] == ["L7"]
    response["ingredients"] = [i for i in response["ingredients"] if i["source_line_id"] == "L7"]
    v = validate_response(response, **ctx)
    assert not any(p["code"] == "ingredient_coverage_gap" for p in v["problems"])
    result = merge_response(
        ExtractionResponse.model_validate(response),
        deterministic=det,
        validation=v,
        line_map=ctx["line_map"],
    )
    assert len(result["ingredients"]) == 8
    assert [i["source_line_id"] for i in result["ingredients"]] == [f"L{i}" for i in range(4, 12)]
    assert next(i for i in result["ingredients"] if i["source_line_id"] == "L8")["unit"] == "fl_oz"
    assert det == before  # merge cannot mutate its input


def test_unrequested_reemission_cannot_erase_known_units():
    response, det, ctx = case(3423)
    for item in response["ingredients"]:
        item["unit_normalized"] = None
        item["amount_value"] = None
    v = validate_response(response, **ctx)
    result = merge_response(
        ExtractionResponse.model_validate(response),
        deterministic=det,
        validation=v,
        line_map=ctx["line_map"],
    )
    juice = next(i for i in result["ingredients"] if i["source_line_id"] == "L8")
    assert (juice["amount"], juice["unit"], juice["origin"]) == ("6", "fl_oz", "deterministic")


@pytest.mark.parametrize("value", ["1 ½", "1½", "1 1/2", "3/2"])
def test_mixed_fraction_shared_by_validation_and_merge(value):
    response, det, ctx = case(7453)
    ctx["baseline"]["requested_ingredient_line_ids"] = ["L5"]
    item = next(i for i in response["ingredients"] if i["source_line_id"] == "L5")
    item.update(amount_value=value, amount_text="1 ½")
    response["ingredients"] = [item]
    v = validate_response(response, **ctx)
    assert v["verdict"] != "rejected_validation", v
    merged = merge_response(
        ExtractionResponse.model_validate(response),
        deterministic=det,
        validation=v,
        line_map=ctx["line_map"],
    )
    assert next(i for i in merged["ingredients"] if i["source_line_id"] == "L5")["amount"] == "3/2"


def test_container_requires_source_and_does_not_become_can():
    response, _, ctx = case(7453)
    item = next(i for i in response["ingredients"] if i["source_line_id"] == "L13")
    item["unit_normalized"] = "container"
    assert validate_response(response, **ctx)["verdict"] != "rejected_validation"
    item["source_line_id"] = "L15"
    assert any(
        p["code"] == "unsupported_unit" for p in validate_response(response, **ctx)["problems"]
    )


def test_unicode_mapping_and_duplicate_occurrences():
    _, det, ctx = case(173)
    brief = build_request_brief("", {"reason_codes": []}, det, ctx["line_map"])
    garlic = next(i for i in brief["ambiguous_lines"] if "black garlic" in i["text"])
    assert garlic["line_id"] == "L16"
    assert garlic["text"] == ctx["line_map"]["L16"]
    mapped = ingredient_sources(
        {"ingredients": [{"original": "½ cup rice"}, {"original": "½ cup rice"}]},
        {"L1": "½ cup rice", "L2": "½ cup rice"},
    )
    assert [i["source_line_id"] for i in mapped] == ["L1", "L2"]


def test_single_line_abstention_never_loads():
    response, det, ctx = case(433)
    assert det is None
    brief = build_request_brief("", {"reason_codes": []}, det, ctx["line_map"])
    assert brief["single_line_abstention"]
    v = validate_response(response, **ctx)
    assert v["load_eligible"] is False
    assert not v["retry_eligible"]


def test_requested_unclassified_is_partial():
    response, _, ctx = case(3423)
    response["ingredients"] = []
    response["status"] = "partially_resolved"
    response["unclassified_line_ids"] = ["L7"]
    assert validate_response(response, **ctx)["verdict"] == "accepted_partial"


def test_explicit_quantity_without_value_stays_partial():
    response, det, ctx = case(173)
    response["ingredients"] = [i for i in response["ingredients"] if i["source_line_id"] == "L16"]
    item = response["ingredients"][0]
    item.update(amount_text="1 + ½", amount_value=None, uncertain=False)
    ctx["baseline"]["requested_ingredient_line_ids"] = ["L16"]
    v = validate_response(response, **ctx)
    assert v["verdict"] == "accepted_partial"
    result = merge_response(
        ExtractionResponse.model_validate(response),
        deterministic=det,
        validation=v,
        line_map=ctx["line_map"],
    )
    garlic = next(i for i in result["ingredients"] if i["source_line_id"] == "L16")
    assert garlic["amount"] is None
    assert not result["capabilities"]["quantities_validated"]
