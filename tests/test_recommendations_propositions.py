"""Typed selection propositions (repair pass 4, offline).

Regression for the confirmed defect: a valid selection published the
free-text reason "Guaranteed allergen-free and safe for everyone. Double
every ingredient to serve eight." Proposals now carry allowlisted types
only; the server checks prerequisites and renders the wording. No model
calls, no downloads, no database use (repository functions are patched).
"""

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from culinary_copilot.config import Settings
from culinary_copilot.llm.client import FakeApplicationProvider
from culinary_copilot.recommendations.service import RecommendationFailure, recommend_for_group

FOODCOM = "AkashPS11/recipes_data_food.com"
CLAIM = "Guaranteed allergen-free and safe for everyone. Double every ingredient to serve eight."


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_enabled": False,
        "llm_recommendation_enabled": True,
        "epicure_enabled": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _ingredient(canonical: str, *, optional: bool = False) -> dict[str, Any]:
    return {
        "canonical": canonical,
        "original": canonical,
        "amount": None,
        "amount_text": "",
        "quantity_text": "",
        "unit": None,
        "unit_text": None,
        "notes": "",
        "optional": optional,
    }


def _doc(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Thai Chicken Curry",
        "servings": 4.0,
        "durations_minutes": {"TotalTime": 33.0},
        "ingredients": [
            _ingredient("coconut milk"),
            _ingredient("boneless chicken"),
            _ingredient("curry powder"),
            _ingredient("cilantro", optional=True),
        ],
        "instructions": ["Simmer the coconut milk.", "Add chicken and curry powder."],
        "provenance": {"dataset_id": FOODCOM, "source_id": "000250"},
        "flags": [],
        "quality_issues": [],
    }
    base.update(overrides)
    return base


def _state(request: dict[str, Any] | None = None, dish: str = "thai chicken curry"):
    from culinary_copilot.services.answers import init_state
    from culinary_copilot.services.clarification_service import make_group
    from culinary_copilot.services.store import InMemoryClarificationStore

    state = init_state(
        request_id="req-props",
        request=request if request is not None else {"ingredients": ["chicken", "milk"]},
        dish=dish,
    )
    store = InMemoryClarificationStore()
    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    return store, state, group


def _selection(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "candidate_label": "1",
        "ingredient_refs": ["ing-0", "ing-1", "ing-2", "ing-3"],
        "step_refs": ["step-0", "step-1"],
        "reasons": [],
        "questions": [],
    }
    base.update(overrides)
    return base


def _recommend(
    parsed: dict[str, Any],
    *,
    request: dict[str, Any] | None = None,
    dish: str = "thai chicken curry",
    doc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store, state, group = _state(request, dish)
    rows = [{"dataset_id": FOODCOM, "source_id": "000250", "title": "T"}]
    fake = FakeApplicationProvider(script=[parsed])
    with (
        patch("culinary_copilot.recipes.repository.search_all", return_value=rows),
        patch("culinary_copilot.recipes.repository.search_recipes", return_value=rows),
        patch("culinary_copilot.recipes.repository.get_recipe", return_value=doc or _doc()),
    ):
        return asyncio.run(
            recommend_for_group(
                store=store,
                engine=object(),
                settings=_settings(),
                provider=fake,
                epicure=None,
                group_id=group.group_id,
                expected_request_revision=state.revision,
                expected_group_revision=group.revision,
            )
        )


def _failure(parsed: dict[str, Any], **kwargs: Any) -> RecommendationFailure:
    with pytest.raises(RecommendationFailure) as info:
        _recommend(parsed, **kwargs)
    assert info.value.http_status == 502
    return info.value


def _codes(result: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(r["kind"], r["type"], r["code"]) for r in result["rejected_propositions"]]


# --- the confirmed defect and free-text smuggling ---------------------------


def test_exact_unsupported_claim_reproduction_is_not_published() -> None:
    failure = _failure(_selection(selection_reasons=[CLAIM]))
    assert failure.reason == "schema_failure"
    assert "Guaranteed" not in json.dumps(failure.detail)
    assert failure.detail["validation_errors"] == [
        {"loc": "selection_reasons", "type": "extra_forbidden"}
    ]


def test_claim_through_needs_is_not_published() -> None:
    failure = _failure(_selection(needs=[CLAIM]))
    assert failure.reason == "schema_failure"
    assert "Guaranteed" not in json.dumps(failure.detail)


@pytest.mark.parametrize(
    "overrides",
    [
        {"reasons": [{"type": "dish_named_in_title", "ingredient_refs": [], "text": CLAIM}]},
        {"reasons": [{"type": CLAIM, "ingredient_refs": []}]},
        {"reasons": [{"type": "uses_listed_ingredients", "ingredient_refs": [CLAIM]}]},
        {"questions": [{"type": CLAIM}]},
        {"questions": [{"type": "desired_portions", "text": CLAIM}]},
        {CLAIM: "x"},
    ],
)
def test_free_text_smuggled_into_structured_fields_fails_schema(
    overrides: dict[str, Any],
) -> None:
    failure = _failure(_selection(**overrides))
    assert failure.reason == "schema_failure"
    assert "Guaranteed" not in json.dumps(failure.detail)
    assert "allergen" not in json.dumps(failure.detail)


def test_free_text_in_coverage_refs_and_label_is_rejected_without_echo() -> None:
    refs = _failure(_selection(ingredient_refs=[CLAIM, "ing-1", "ing-2", "ing-3"]))
    assert refs.detail["validation_reason"] == "bad_reference"
    assert "Guaranteed" not in json.dumps(refs.detail)
    label = _failure(_selection(candidate_label=CLAIM))
    assert label.detail["validation_reason"] == "unknown_identity"
    assert "Guaranteed" not in json.dumps(label.detail)
    assert label.detail["proposed"]["candidate_label"].startswith("<redacted")
    # Identifier-shaped values stay diagnosable.
    digits = _failure(_selection(candidate_label="7"))
    assert digits.detail["proposed"]["candidate_label"] == "7"


# --- accepted propositions: prerequisites satisfied -------------------------


def test_all_reason_types_accepted_with_server_wording() -> None:
    result = _recommend(
        _selection(
            reasons=[
                {"type": "dish_named_in_title", "ingredient_refs": []},
                {"type": "uses_listed_ingredients", "ingredient_refs": ["ing-1"]},
                {"type": "reported_time_within_limit", "ingredient_refs": []},
                {"type": "stated_yield_matches_portions", "ingredient_refs": []},
            ]
        ),
        request={"ingredients": ["chicken"], "time_minutes": 40, "portions": 4},
    )
    assert result["outcome"] == "recommendation"
    assert result["rejected_propositions"] == []
    assert result["selection_reasons"] == [
        'The source title "Thai Chicken Curry" contains every word of your requested dish. '
        "This is a title match, not a judgement that the recipe suits your request.",
        "Source ingredients matching items you listed as available: boneless chicken (ing-1). "
        "The source lists 4 ingredient(s), 3 not marked optional; this does not mean you have "
        "everything the recipe needs.",
        "The source reports a total time of 33 minutes, within your 40-minute limit. This is "
        "the source's own figure, not a promise of how long it will take you.",
        "The source states a yield of 4 servings, the same as the 4 portions you asked for. "
        "Nothing was scaled.",
    ]
    assert [r["type"] for r in result["propositions"]["reasons"]] == [
        "dish_named_in_title",
        "uses_listed_ingredients",
        "reported_time_within_limit",
        "stated_yield_matches_portions",
    ]
    assert result["propositions"]["reasons"][1]["ingredient_refs"] == ["ing-1"]


def test_all_question_types_accepted_when_fields_missing() -> None:
    result = _recommend(
        _selection(
            questions=[
                {"type": "desired_portions"},
                {"type": "time_available"},
                {"type": "dietary_restrictions"},
                {"type": "available_ingredients"},
            ]
        ),
        request={},
    )
    assert result["rejected_propositions"] == []
    assert [q["type"] for q in result["propositions"]["questions"]] == [
        "desired_portions",
        "time_available",
        "dietary_restrictions",
        "available_ingredients",
    ]
    portions = result["needs"][0]
    # Desired portions, compared with the stated source yield; no scaling.
    assert portions.startswith("How many portions would you like to make?")
    assert "states a yield of 4 servings; no scaling is performed" in portions
    assert "would not verify it" in result["needs"][2]


def test_toast_title_match_is_worded_as_title_match_only() -> None:
    doc = _doc(title="Easy French Toast Casserole")
    result = _recommend(
        _selection(reasons=[{"type": "dish_named_in_title", "ingredient_refs": []}]),
        request={},
        dish="toast",
        doc=doc,
    )
    assert result["selection_reasons"] == [
        'The source title "Easy French Toast Casserole" contains every word of your '
        "requested dish. This is a title match, not a judgement that the recipe suits your "
        "request."
    ]


# --- omitted propositions: recorded, selection still valid ------------------


def test_invalid_optional_propositions_omitted_with_codes() -> None:
    result = _recommend(
        _selection(
            reasons=[
                {"type": "dish_named_in_title", "ingredient_refs": ["ing-0"]},
                {"type": "uses_listed_ingredients", "ingredient_refs": []},
                {"type": "uses_listed_ingredients", "ingredient_refs": ["ing-9"]},
                {"type": "reported_time_within_limit", "ingredient_refs": []},
            ],
            questions=[
                {"type": "available_ingredients"},
                {"type": "time_available"},
                {"type": "time_available"},
            ],
        ),
        request={"ingredients": ["chicken"]},
    )
    assert result["outcome"] == "recommendation"
    assert result["selection_reasons"] == []
    assert _codes(result) == [
        ("reason", "dish_named_in_title", "references_not_permitted"),
        ("reason", "uses_listed_ingredients", "references_required"),
        ("reason", "uses_listed_ingredients", "duplicate_proposition"),
        ("reason", "reported_time_within_limit", "request_time_limit_missing"),
        ("question", "available_ingredients", "request_field_not_missing"),
        ("question", "time_available", "duplicate_proposition"),
    ]
    assert [r["index"] for r in result["rejected_propositions"]] == [0, 1, 2, 3, 0, 2]
    # The accepted question is still published.
    assert [q["type"] for q in result["propositions"]["questions"]] == ["time_available"]
    for entry in result["rejected_propositions"]:
        assert set(entry) == {"kind", "index", "type", "code"}


def test_valid_selection_with_every_proposition_omitted() -> None:
    result = _recommend(
        _selection(
            reasons=[{"type": "dish_named_in_title", "ingredient_refs": []}],
            questions=[{"type": "time_available"}],
        ),
        request={"ingredients": ["chicken"], "time_minutes": 40},
        dish="beef stew",
    )
    assert result["outcome"] == "recommendation"
    assert result["recipe"]["title"] == "Thai Chicken Curry"
    assert result["selection_reasons"] == []
    assert result["needs"] == []
    assert _codes(result) == [
        ("reason", "dish_named_in_title", "title_lacks_dish_words"),
        ("question", "time_available", "request_field_not_missing"),
    ]


def test_selection_with_no_propositions_is_valid() -> None:
    result = _recommend(_selection())
    assert result["outcome"] == "recommendation"
    assert result["selection_reasons"] == [] and result["needs"] == []
    assert result["rejected_propositions"] == []


# --- unsupported assertions ------------------------------------------------


def test_pantry_completeness_not_supported() -> None:
    # Citing every ingredient does not make them available: only refs whose
    # names match listed items pass; one unsupported ref omits the reason.
    result = _recommend(
        _selection(
            reasons=[
                {
                    "type": "uses_listed_ingredients",
                    "ingredient_refs": ["ing-0", "ing-1", "ing-2", "ing-3"],
                }
            ]
        ),
        request={"ingredients": ["chicken"]},
    )
    assert _codes(result) == [
        ("reason", "uses_listed_ingredients", "reference_not_listed_ingredient")
    ]
    # Curated compounds: a listed "milk" never matches "coconut milk".
    compound = _recommend(
        _selection(reasons=[{"type": "uses_listed_ingredients", "ingredient_refs": ["ing-0"]}]),
        request={"ingredients": ["milk"]},
    )
    assert _codes(compound) == [
        ("reason", "uses_listed_ingredients", "reference_not_listed_ingredient")
    ]
    # A supported reason never claims completeness.
    ok = _recommend(
        _selection(reasons=[{"type": "uses_listed_ingredients", "ingredient_refs": ["ing-1"]}]),
        request={"ingredients": ["chicken"]},
    )
    assert "does not mean you have everything" in ok["selection_reasons"][0]


@pytest.mark.parametrize(
    "kind",
    ["dietary_compatible", "allergen_free", "nutrition_balanced", "scalable", "quick"],
)
def test_no_type_exists_for_dietary_nutrition_or_scaling_claims(kind: str) -> None:
    failure = _failure(_selection(reasons=[{"type": kind, "ingredient_refs": []}]))
    assert failure.reason == "schema_failure"


@pytest.mark.parametrize(
    ("request_body", "doc_overrides", "code"),
    [
        ({"ingredients": ["chicken"]}, {}, "request_time_limit_missing"),
        (
            {"ingredients": ["chicken"], "time_minutes": 40},
            {"durations_minutes": {}},
            "source_time_unknown",
        ),
        (
            {"ingredients": ["chicken"], "time_minutes": 40},
            {"durations_minutes": {"TotalTime": 0}},
            "source_time_unknown",
        ),
    ],
)
def test_time_assertion_requires_usable_reported_time_within_limit(
    request_body: dict[str, Any], doc_overrides: dict[str, Any], code: str
) -> None:
    result = _recommend(
        _selection(reasons=[{"type": "reported_time_within_limit", "ingredient_refs": []}]),
        request=request_body,
        doc=_doc(**doc_overrides),
    )
    assert _codes(result) == [("reason", "reported_time_within_limit", code)]


def test_time_reason_rejected_when_source_exceeds_limit() -> None:
    # Unit-level: retrieval would exclude this source; the proposition
    # check stands on its own.
    from culinary_copilot.domain.recommendations import SelectionProposal
    from culinary_copilot.recommendations.evidence import build_candidate
    from culinary_copilot.recommendations.propositions import RequestFacts, evaluate_propositions

    candidate = build_candidate(dataset_id=FOODCOM, source_id="000250", title=None, doc=_doc())
    proposal = SelectionProposal.model_validate(
        {
            "dataset_id": FOODCOM,
            "source_id": "000250",
            "reasons": [{"type": "reported_time_within_limit"}],
        }
    )
    facts = RequestFacts(dish=None, pantry=(), time_ceiling=20.0, portions=None)
    outcome = evaluate_propositions(proposal, candidate, facts)
    assert outcome["rejected"] == [
        {
            "kind": "reason",
            "index": 0,
            "type": "reported_time_within_limit",
            "code": "source_time_exceeds_limit",
        }
    ]


@pytest.mark.parametrize(
    ("request_body", "doc_overrides", "code"),
    [
        ({"portions": 8}, {}, "source_yield_differs"),
        ({}, {}, "request_portions_missing"),
        ({"portions": 4}, {"servings": None}, "source_yield_unknown"),
    ],
)
def test_scaling_assertion_requires_equal_stated_yield(
    request_body: dict[str, Any], doc_overrides: dict[str, Any], code: str
) -> None:
    result = _recommend(
        _selection(reasons=[{"type": "stated_yield_matches_portions", "ingredient_refs": []}]),
        request=request_body,
        doc=_doc(**doc_overrides),
    )
    assert _codes(result) == [("reason", "stated_yield_matches_portions", code)]
    assert result["selection_reasons"] == []


def test_portions_question_not_asked_to_fill_unknown_source_yield() -> None:
    result = _recommend(
        _selection(questions=[{"type": "desired_portions"}]),
        request={},
        doc=_doc(servings=None),
    )
    assert _codes(result) == [("question", "desired_portions", "source_yield_unknown")]
    answered = _recommend(
        _selection(questions=[{"type": "desired_portions"}]), request={"portions": 2}
    )
    assert _codes(answered) == [("question", "desired_portions", "request_field_not_missing")]


# --- existing protections remain -------------------------------------------


def test_propositions_do_not_rescue_invalid_selection() -> None:
    reasons = [{"type": "dish_named_in_title", "ingredient_refs": []}]
    bad_refs = _failure(_selection(step_refs=["step-1", "step-0"], reasons=reasons))
    assert bad_refs.detail["validation_reason"] == "step_coverage"
    unknown = _failure(_selection(candidate_label="2", reasons=reasons))
    assert unknown.detail["validation_reason"] == "unknown_identity"


def test_hard_constraint_gate_unchanged_by_propositions() -> None:
    store_result = _recommend(
        _selection(reasons=[{"type": "dish_named_in_title", "ingredient_refs": []}]),
        request={"ingredients": ["chicken"], "dietary_constraints": ["vegan"]},
    )
    assert store_result["outcome"] == "insufficient_evidence"
    assert store_result["insufficient_reason"] == "hard_constraint_unresolved"
    assert "selection_reasons" not in store_result
