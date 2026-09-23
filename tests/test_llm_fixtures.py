"""AI-authored reference fixtures (PENDING HUMAN REVIEW) through the real path.

Each fixture in tests/fixtures/llm_expected/ carries pinned source text
whose SHA-256 is integrity-checked here, then runs the real
validate_response + merge_response with a deterministic baseline built by
the real normalizer. These pin the targeted-extraction contract; they are
not measured model performance.
"""

import hashlib
import json
from pathlib import Path

import pytest

from culinary_copilot.recipes.adapters.foodie import FOODIE_ADAPTER_VERSION, normalize_foodie_text
from culinary_copilot.recipes.llm_batch import block_line_ids, prose_line_ids
from culinary_copilot.recipes.llm_contracts import ExtractionResponse, numbered_source
from culinary_copilot.recipes.llm_validate import (
    current_version_map,
    merge_response,
    validate_response,
)
from culinary_copilot.recipes.routing import ROUTING_VERSION

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "llm_expected"
FIXTURES = sorted(str(p) for p in FIXTURE_DIR.glob("[0-9]*.json"))


def _versions():
    return {
        **current_version_map(
            adapter_version=FOODIE_ADAPTER_VERSION, routing_version=ROUTING_VERSION
        ),
        "model": "gpt-5-nano",
        "reasoning_effort": "low",
    }


def _run_fixture(path):
    fixture = json.loads(Path(path).read_text())
    assert fixture["label"].startswith("AI-authored reference fixture")
    source = fixture["source"]
    texts = source["texts"]
    assert hashlib.sha256(texts.encode()).hexdigest() == source["content_hash"]
    _, line_map = numbered_source(texts)
    ing_ids, step_ids, heading_ids, _ = block_line_ids(texts)
    prose_ids, _ = prose_line_ids(texts)
    try:
        deterministic = normalize_foodie_text(texts, source["row_number"])
    except ValueError:
        deterministic = None
    baseline = {
        "has_deterministic": deterministic is not None,
        "servings": (deterministic or {}).get("servings"),
        "batch_yield_count": ((deterministic or {}).get("batch_yield") or {}).get("count"),
        "has_steps": bool((deterministic or {}).get("instructions")),
        "has_groups": bool((deterministic or {}).get("ingredient_groups")),
        "has_title": bool((deterministic or {}).get("title")),
    }
    versions = _versions()
    validation = validate_response(
        fixture["expected"],
        source_id=fixture["expected"]["source_id"],
        content_hash=source["content_hash"],
        line_map=line_map,
        ingredient_line_ids=ing_ids,
        step_line_ids=step_ids,
        heading_line_ids=heading_ids,
        prose_line_ids=prose_ids,
        baseline=baseline,
        request_versions=versions,
        current_versions=versions,
    )
    merged = None
    if validation["verdict"] in ("accepted", "accepted_partial"):
        merged = merge_response(
            ExtractionResponse.model_validate(fixture["expected"]),
            deterministic=deterministic,
            validation=validation,
            line_map=line_map,
        )
    return fixture, deterministic, validation, merged, line_map


@pytest.mark.parametrize("path", FIXTURES, ids=[Path(p).stem for p in FIXTURES])
def test_fixture_verdict(path):
    fixture, _, validation, _, _ = _run_fixture(path)
    assert validation["verdict"] == fixture["expected_verdict"], validation["problems"]


@pytest.mark.parametrize("path", FIXTURES, ids=[Path(p).stem for p in FIXTURES])
def test_fixture_overlay_preserves_untouched_fields(path):
    """Partial overlays keep every deterministic field the model did not cover."""
    fixture, deterministic, validation, merged, line_map = _run_fixture(path)
    if merged is None or deterministic is None:
        pytest.skip("no merged record (unresolved without deterministic base)")
    # Untouched source fields survive verbatim.
    assert merged["title"] == deterministic["title"]
    assert merged["description"] == deterministic["description"]
    assert merged["instructions"] == deterministic["instructions"]
    assert merged["servings"] == deterministic["servings"]
    assert merged["source_id"] == deterministic["source_id"]
    # Every occurrence is tagged; model-covered lines replace, the rest persist.
    assert merged["ingredients"], "merged record must not lose all ingredients"
    for occurrence in merged["ingredients"]:
        assert occurrence.get("origin") in ("llm", "deterministic")
    model_lids = {item["source_line_id"] for item in fixture["expected"]["ingredients"]}
    covered_texts = {line_map[lid] for lid in model_lids if lid in line_map}
    for occurrence in merged["ingredients"]:
        if occurrence["origin"] == "deterministic":
            assert (occurrence.get("original") or "").strip() not in covered_texts


def test_fixture_7453_guacamole_on_correct_line():
    fixture, _, validation, merged, _ = _run_fixture(
        str(Path(__file__).parent / "fixtures" / "llm_expected" / "7453.json")
    )
    assert validation["verdict"] == "accepted", validation["problems"]
    guac = [i for i in merged["ingredients"] if "guacamole" in i["canonical"]]
    assert len(guac) == 1 and guac[0]["source_line_id"] == "L13"
    assert guac[0]["origin"] == "llm"
    groups = [g["heading"] for g in merged["ingredient_groups"]]
    assert "For the garnish" in groups


def test_fixture_303_equivalent_mass_pair():
    fixture, _, validation, merged, _ = _run_fixture(
        str(Path(__file__).parent / "fixtures" / "llm_expected" / "303.json")
    )
    assert validation["verdict"] == "accepted", validation["problems"]
    l4 = [i for i in merged["ingredients"] if i.get("source_line_id") == "L4"]
    assert sorted(i["amount"] for i in l4) == ["6", "600"]
    assert any(i.get("equivalent") for i in l4)


def test_fixture_433_honest_abstention():
    fixture, deterministic, validation, merged, _ = _run_fixture(
        str(Path(__file__).parent / "fixtures" / "llm_expected" / "433.json")
    )
    assert deterministic is None  # no deterministic base exists
    assert validation["verdict"] == "accepted_partial"
    assert merged is not None and merged["ingredients"] == []
    assert merged["capabilities"]["searchable"] is False
