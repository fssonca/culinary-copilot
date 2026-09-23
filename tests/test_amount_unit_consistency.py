"""Amount/unit source-consistency: units and amounts must be evidenced.

Regression suite for the foodie-019380 failure, where merge inherited
unit 'lb' from a chicken occurrence onto lemon, salt and eggs sharing
one glued source line. Fixtures 19380-source.txt / 19380-parsed.json are
verbatim evaluation artifacts (real source, real saved model response);
all other cases are labeled synthetic constructions testing the rule.
"""

import hashlib
import json
from pathlib import Path

from culinary_copilot.recipes import routing as routing_mod
from culinary_copilot.recipes.adapters.foodie import (
    FOODIE_ADAPTER_VERSION,
    normalize_foodie_text,
)
from culinary_copilot.recipes.llm_batch import (
    _is_loadable,
    block_line_ids,
    build_request_brief,
    prose_line_ids,
)
from culinary_copilot.recipes.llm_contracts import ExtractionResponse, numbered_source
from culinary_copilot.recipes.llm_validate import (
    current_version_map,
    merge_response,
    validate_response,
)

FIX = Path(__file__).parent / "fixtures" / "amount_unit"


def _ctx(texts, row=19380):
    _, line_map = numbered_source(texts)
    ing, steps, headings, _ = block_line_ids(texts)
    prose, _ = prose_line_ids(texts)
    versions = {
        **current_version_map(
            adapter_version=FOODIE_ADAPTER_VERSION, routing_version=routing_mod.ROUTING_VERSION
        ),
        "model": "gpt-6-luna",
        "reasoning_effort": "low",
    }
    try:
        det = normalize_foodie_text(texts, row)
    except ValueError:
        det = None
    brief = build_request_brief(
        texts,
        {"route": "needs_llm", "reason_codes": ["quantity_unknown"]},
        det,
        line_map,
    )
    baseline = {
        "requested_ingredient_line_ids": brief["requested_ingredient_line_ids"]
        if det is not None
        else None,
        "has_deterministic": det is not None,
        "servings": (det or {}).get("servings"),
        "batch_yield_count": ((det or {}).get("batch_yield") or {}).get("count"),
        "has_steps": bool((det or {}).get("instructions")),
        "has_groups": bool((det or {}).get("ingredient_groups")),
        "has_title": bool((det or {}).get("title")),
    }
    return {
        "source_id": f"foodie-{row:06d}",
        "content_hash": hashlib.sha256(texts.encode()).hexdigest(),
        "line_map": line_map,
        "ingredient_line_ids": ing,
        "step_line_ids": steps,
        "heading_line_ids": headings,
        "prose_line_ids": prose,
        "baseline": baseline,
        "request_versions": versions,
        "current_versions": versions,
    }, det


def _item(
    line_id,
    excerpt,
    name,
    amount_text=None,
    amount_value=None,
    unit=None,
    is_range=False,
    uncertain=False,
    qualitative=False,
):
    return {
        "source_line_id": line_id,
        "name": name,
        "amount_text": amount_text,
        "amount_value": amount_value,
        "unit_text": None,
        "unit_normalized": unit,
        "qualitative": qualitative,
        "optional": False,
        "alternatives": [],
        "notes": None,
        "group": "main",
        "is_range": is_range,
        "compound": False,
        "equivalent": None,
        "uncertain": uncertain,
        "evidence": {"line_ids": [line_id], "excerpt": excerpt},
    }


def _response(texts, row, items, title="T", status="resolved"):
    _, line_map = numbered_source(texts)
    lids = sorted(line_map)
    step_ids = [lid for lid in lids if lid not in {i["source_line_id"] for i in items}][:1]
    return {
        "status": status,
        "source_id": f"foodie-{row:06d}",
        "content_hash": hashlib.sha256(texts.encode()).hexdigest(),
        "title": title,
        "description": None,
        "ingredients": items,
        "steps": [
            {
                "source_line_id": lid,
                "text": line_map[lid],
                "evidence": {"line_ids": [lid], "excerpt": line_map[lid]},
            }
            for lid in step_ids
        ],
        "headings": [],
        "notes": [],
        "servings": None,
        "servings_text": None,
        "batch_yield_count": None,
        "batch_yield_text": None,
        "temperatures": [],
        "durations": [],
        "uncertainty_notes": [],
        "unclassified_line_ids": [],
        "second_recipe_boundary": None,
    }


def test_19380_glued_line_prior_does_not_leak_units():
    """Verbatim 19380 source + verbatim saved model response.

    The deterministic parse degenerately succeeds (one 'lb' occurrence on
    the shared blob line). Merge must not attribute that unit to other
    ingredients on the same line; the model's own 'lb' (chicken) stays.
    """
    texts = (FIX / "19380-source.txt").read_text()
    parsed = json.loads((FIX / "19380-parsed.json").read_text())
    ctx, det = _ctx(texts)
    assert det is not None  # degenerate success is the trigger
    report = validate_response(parsed, **ctx)
    assert "unit_unsupported_by_line" not in {p["code"] for p in report["problems"]}
    merged = merge_response(
        ExtractionResponse.model_validate(parsed),
        deterministic=det,
        validation=report,
        line_map=ctx["line_map"],
    )
    by_name = {i["name"]: i.get("unit") for i in merged["ingredients"]}
    assert by_name["chicken thighs and drumsticks"] == "lb"
    assert by_name["lemon, juiced"] is None
    assert by_name["salt"] is None
    assert by_name["hard-boiled eggs, peeled"] is None


def test_mutated_lb_on_lemon_line_is_rejected():
    """Synthetic separate-line source: unit 'lb' invented for a line that
    never states pounds must reject with retry allowed. (On the verbatim
    19380 blob the cited line itself contains 'pounds', so line evidence
    cannot discriminate there — the merge-inheritance fix covers it.)"""
    texts = "T\nIngredients\n1 small lemon\n2 pounds chicken\nDirections\nMix.\nBake.\n"
    ctx, _ = _ctx(texts, row=22)
    lemon = next(lid for lid, t in ctx["line_map"].items() if "lemon" in t)
    chicken = next(lid for lid, t in ctx["line_map"].items() if "chicken" in t)
    response = _response(
        texts,
        22,
        [
            _item(lemon, "1 small lemon", "lemon", "1", "1", "lb"),
            _item(chicken, "2 pounds chicken", "chicken", "2", "2", "lb"),
        ],
    )
    report = validate_response(response, **ctx)
    assert "unit_unsupported_by_line" in {p["code"] for p in report["problems"]}
    assert report["verdict"] == "rejected_validation"
    assert report["retry_eligible"] is True


def test_mutated_amount_value_is_rejected():
    """Synthetic: amount_value 5 with text '5' on a '2 cups' line."""
    texts = "T\nIngredients\n2 cups flour\nDirections\nMix.\nBake.\n"
    ctx, _ = _ctx(texts, row=21)
    lid = next(lid for lid, text in ctx["line_map"].items() if "flour" in text)
    response = _response(texts, 21, [_item(lid, "2 cups flour", "flour", "5", "5", "cup")])
    report = validate_response(response, **ctx)
    assert "amount_unsupported_by_line" in {p["code"] for p in report["problems"]}
    assert report["verdict"] == "rejected_validation"


def test_valid_amounts_and_units_pass():
    """Synthetic valid extractions that must never trip the checks."""
    cases = [
        # (line, amount_text, value, unit)
        ("1 15-ounce can chickpeas", "1", "1", "can"),  # dash size
        ("½ cup sugar", "1/2", "1/2", "cup"),  # unicode fraction
        ("1 1/2 cups flour", "1 1/2", "1 1/2", "cup"),  # mixed fraction
        ("2-3 cloves garlic", "2-3", None, "clove"),  # range, no value
        ("1 (16 ounce) package lentils", "1", "1", None),  # package size kept out
        ("2 medium zucchini", "2", "2", None),  # count with adjectives
        ("3 Tablespoons pepper or 2 scotch bonnet", "3", "3", "tbsp"),  # compound alt
        ("1.5 litres water", "1.5", "1.5", "l"),  # decimal + metric
        ("2 Cooking spoons palm oil", "2", "2", "count"),  # count fallback exempt
        ("1 stick butter", "1", "1", "stick"),  # lexical container noun
        ("1 tspblack pepper", "1", "1", "tsp"),  # glued abbreviation (real 173 shape)
    ]
    for pos, (line, text, value, unit) in enumerate(cases):
        texts = f"T\nIngredients\n{line}\nDirections\nMix.\nBake.\n"
        ctx, _ = _ctx(texts, row=100 + pos)
        lid = next(lid for lid, t in ctx["line_map"].items() if line.split()[0] in t or t == line)
        response = _response(texts, 100 + pos, [_item(lid, line, "x", text, value, unit)])
        report = validate_response(response, **ctx)
        codes = {p["code"] for p in report["problems"]}
        assert "unit_unsupported_by_line" not in codes, (line, codes)
        assert "amount_unsupported_by_line" not in codes, (line, codes)
        # Ranges honestly cap at partial; everything else must be clean.
        expected = "accepted_partial" if "2-3" in line else "accepted"
        assert report["verdict"] == expected, (line, codes)


def test_short_aliases_stay_whole_token_only():
    """'cloves' must never evidence 'cl'; 'large' must never evidence 'l'."""
    texts = "T\nIngredients\n2 cloves garlic\nDirections\nMix.\nBake.\n"
    ctx, _ = _ctx(texts, row=43)
    lid = next(lid for lid, t in ctx["line_map"].items() if "garlic" in t)
    response = _response(texts, 43, [_item(lid, "2 cloves garlic", "garlic", "2", "2", "cl")])
    report = validate_response(response, **ctx)
    assert "unit_unsupported_by_line" in {p["code"] for p in report["problems"]}


def test_uncertain_range_stays_restricted():
    """Honest ranges stay uncertainty (not errors) with capped capabilities."""
    texts = "T\nIngredients\n2-3 cups broth\nDirections\nSimmer.\nServe.\n"
    ctx, det = _ctx(texts, row=42)
    lid = next(lid for lid, t in ctx["line_map"].items() if "broth" in t)
    item = _item(lid, "2-3 cups broth", "broth", "2-3", None, "cup", is_range=True)
    response = _response(texts, 42, [item])
    report = validate_response(response, **ctx)
    assert report["verdict"] == "accepted_partial"
    assert "unit_unsupported_by_line" not in {p["code"] for p in report["problems"]}
    merged = merge_response(
        ExtractionResponse.model_validate(response),
        deterministic=det,
        validation=report,
        line_map=ctx["line_map"],
    )
    assert merged["capabilities"]["scalable"] is False
    assert merged["capabilities"]["quantities_validated"] is False
    assert _is_loadable(merged) is True
