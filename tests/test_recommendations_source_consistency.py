"""Incomplete-source presentation policy (Phase 3 close-out; offline).

FOLLOWUP-01 selected a source whose steps use ingredients its ingredient
list omits. The stored row carries no machine-readable defect for that, so
the production path cannot block it and no automatic detector exists.
These tests use isolated synthetic fixtures (never the stored row) to pin:

- honest unknowns (unknown quantities, a quantity-count mismatch flag, no
  recorded defect) stay recommendable, and the response says the
  ingredient list was not checked against the steps;
- a recorded error-severity inconsistency blocks presentation as a
  ready-to-cook recipe and surfaces only as an explicitly unverified
  discovery pointer that names the defect;
- proposed enrichment fixtures under ``evals/`` are never loaded by
  production code.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from test_recommendations import (
    FOODCOM,
    _doc,
    _run,
    _selection,
    _settings,
    _store_with_group,
)

from culinary_copilot.llm.client import FakeApplicationProvider
from culinary_copilot.recommendations.epicure import FakeEpicureAdapter
from culinary_copilot.recommendations.evidence import build_candidate
from culinary_copilot.recommendations.service import RecommendationFailure, recommend_for_group

# Synthetic identities: production logic never special-cases a source id.
STEP_ONLY_ID = "900001"
CLEAN_ID = "000159"

# Proposed quality signal (see evals/phase3_review/enrichment_proposals.json,
# ENR-03). It is a fixture here; no stored row carries it.
INCONSISTENCY = {
    "code": "ingredient_list_inconsistent",
    "severity": "error",
    "field": "ingredients",
    "message": "Steps use ingredients that the ingredient list omits.",
}


def _unknown(name: str) -> dict[str, Any]:
    return {
        "canonical": name,
        "original": name,
        "amount": None,
        "amount_text": None,
        "quantity_text": None,
        "unit": None,
        "unit_text": None,
        "notes": None,
        "optional": None,
    }


def _step_only_doc(**overrides: Any) -> dict[str, Any]:
    """Legacy-shaped source: unknown quantities, steps use unlisted items."""
    base = _doc(
        title="Fixture Curry Chicken",
        durations_minutes={"TotalTime": 20.0, "PrepTime": 20.0, "CookTime": None},
        ingredients=[_unknown("chicken wings"), _unknown("curry powder"), _unknown("onion")],
        instructions=[
            "Season the wings with curry powder.",
            "Cut potatoes into cubes.",
            "Brown the wings, onion and potatoes in oil.",
            "Add water and simmer.",
        ],
        flags=["ingredient_quantity_mismatch", "units_unknown", "quantity_unknown"],
        provenance={"dataset_id": FOODCOM, "source_id": STEP_ONLY_ID},
    )
    # Stored Food.com rows predate quality metadata: no key at all.
    base.pop("quality_issues")
    base.pop("capabilities")
    base.pop("available_fields")
    base.update(overrides)
    return base


def _step_only_selection() -> dict[str, Any]:
    return _selection(
        sid=STEP_ONLY_ID,
        ingredient_refs=["ing-0", "ing-1", "ing-2"],
        step_refs=["step-0", "step-1", "step-2", "step-3"],
    )


def _recommend_with(docs: dict[str, dict[str, Any]], fake: FakeApplicationProvider):
    store, state, group = _store_with_group()
    rows = [{"dataset_id": FOODCOM, "source_id": sid, "title": sid} for sid in docs]

    def _get(_engine: Any, source_id: str, *, dataset_id: str | None = None) -> Any:
        assert dataset_id == FOODCOM  # exact-pair lookup only
        return docs.get(source_id)

    with (
        patch("culinary_copilot.recipes.repository.search_all", return_value=rows),
        patch("culinary_copilot.recipes.repository.get_recipe", side_effect=_get),
    ):
        return _run(
            recommend_for_group(
                store=store,
                engine=object(),
                settings=_settings(),
                provider=fake,
                epicure=FakeEpicureAdapter(),
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
            )
        )


def test_honest_unknowns_without_recorded_defect_remain_recommendable() -> None:
    # Unknown quantities and a quantity-count mismatch are not, by
    # themselves, a broken ingredient list. With no recorded defect the
    # source is admitted, which is also why the stored FOLLOWUP-01 row is
    # still presentable today: nothing detects step-only ingredients.
    fake = FakeApplicationProvider(script=[_step_only_selection()])
    result = _recommend_with({STEP_ONLY_ID: _step_only_doc()}, fake)
    assert result["outcome"] == "recommendation"
    assert all(i["amount"] == "unknown" for i in result["recipe"]["ingredients"])
    assert all(i["quantity_text"] == "unknown" for i in result["recipe"]["ingredients"])
    checks = result["source_checks"]
    assert checks["structural_admission"] == "passed"
    assert checks["recorded_blocking_defects"] == []
    assert checks["ingredient_list_vs_steps"] == "not_checked"
    assert "ingredient_quantity_mismatch" in checks["source_flags"]
    assert "do not establish" in checks["note"]
    assert "completeness" in result["constraints_not_verified"]


def test_warning_severity_signal_is_context_not_a_block() -> None:
    warning = {**INCONSISTENCY, "severity": "warning"}
    candidate = build_candidate(
        dataset_id=FOODCOM,
        source_id=STEP_ONLY_ID,
        title=None,
        doc=_step_only_doc(quality_issues=[warning]),
    )
    assert candidate["recommendable"] is True
    assert candidate["defects_blocking"] == []
    assert candidate["quality_context"][0]["code"] == "ingredient_list_inconsistent"


def test_recorded_inconsistency_blocks_ready_to_cook_presentation() -> None:
    fake = FakeApplicationProvider(script=[_step_only_selection()])
    doc = _step_only_doc(quality_issues=[INCONSISTENCY])
    result = _recommend_with({STEP_ONLY_ID: doc}, fake)
    assert result["outcome"] == "insufficient_evidence"
    assert result["insufficient_reason"] == "incomplete_source_only"
    assert result["recipe"] is None
    assert fake.calls == []  # never offered to the model
    [pointer] = result["discovery"]
    assert pointer["source_id"] == STEP_ONLY_ID
    assert pointer["unverified"] is True
    assert pointer["ready_to_cook"] is False
    assert pointer["why"] == "blocking source defect"
    assert pointer["source_defects"] == ["ingredient_list_inconsistent"]
    # A pointer carries identity and provenance, never recipe content.
    assert "ingredients" not in pointer and "instructions" not in pointer


def test_structural_gap_pointer_is_labeled_apart_from_defects() -> None:
    fake = FakeApplicationProvider(script=[])
    result = _recommend_with({STEP_ONLY_ID: _step_only_doc(instructions=[])}, fake)
    [pointer] = result["discovery"]
    assert pointer["why"] == "structurally incomplete source"
    assert pointer["source_defects"] == []


def test_recorded_inconsistency_is_never_offered_beside_a_clean_source() -> None:
    docs = {
        STEP_ONLY_ID: _step_only_doc(quality_issues=[INCONSISTENCY]),
        CLEAN_ID: _doc(),
    }
    fake = FakeApplicationProvider(script=[_selection()])
    result = _recommend_with(docs, fake)
    assert result["outcome"] == "recommendation"
    assert result["selection"]["source_id"] == CLEAN_ID
    offered = [c["source_id"] for c in result["evidence"]["offered_candidates"]]
    assert offered == [CLEAN_ID]
    sent = fake.calls[0]["system"] + fake.calls[0]["user"]
    assert "Fixture Curry Chicken" not in sent and STEP_ONLY_ID not in sent

    # Proposing the blocked identity anyway is an unknown identity.
    blocked = FakeApplicationProvider(script=[_step_only_selection()])
    with pytest.raises(RecommendationFailure) as exc_info:
        _recommend_with(docs, blocked)
    assert exc_info.value.detail.get("validation_reason") == "unknown_identity"


def test_production_code_never_loads_review_fixtures() -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "phase3_review" not in text, path
        assert "enrichment_proposals" not in text, path
