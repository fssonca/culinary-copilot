"""Unit tests for AI-authored adjudication helpers (offline, synthetic)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "retrieval_eval"))

from adjudicate import (  # noqa: E402
    adjudication_layer,
    aggregate_suitability,
    screen_ingredients,
    vegan_status,
    vegetarian_status,
)


def test_whole_word_screening_never_matches_eggplant() -> None:
    hits = screen_ingredients(["eggplant", "zucchini", "eggs", "chicken broth"])
    assert hits == {"meat_fish": ["chicken broth"], "egg_dairy": ["eggs"]}


def test_vegetarian_and_vegan_statuses() -> None:
    assert vegetarian_status(["tofu", "honey"])[0] == "supported"
    assert vegetarian_status(["tofu", "chicken"])[0] == "violated"
    assert vegan_status(["tofu", "honey"])[0] == "supported"
    assert vegan_status(["tofu", "butter"])[0] == "violated"


def test_aggregation_rules() -> None:
    assert aggregate_suitability(2, []) == "not_applicable"
    assert aggregate_suitability(0, []) == "not_suitable"
    assert (
        aggregate_suitability(2, [{"constraint": "vegan", "status": "violated"}]) == "not_suitable"
    )
    assert (
        aggregate_suitability(2, [{"constraint": "vegan", "status": "unresolved"}]) == "unresolved"
    )
    assert aggregate_suitability(2, [{"constraint": "time", "status": "supported"}]) == "supported"
    assert aggregate_suitability(None, []) == "not_applicable"


def test_layer_carries_ai_provenance_not_human() -> None:
    layer = adjudication_layer(
        topical_grade=2,
        topical_reason="same dish",
        checks=[],
        system_enforced={},
        basis="owner-accepted combined review",
    )
    assert layer["reviewer_type"] == "ai_policy_adjudication"
    assert layer["human_verified"] is False
    assert layer["suitability"] == "not_applicable"
