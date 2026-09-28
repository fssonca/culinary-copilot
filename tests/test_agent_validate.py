"""Deterministic agent-output validators (offline, no database)."""

from __future__ import annotations

from typing import Any

from culinary_copilot.agent.validate import (
    hard_constraint_keys,
    quantity_in_source,
    validate_options,
    validate_plan,
)

DOC = {
    "dataset_id": "odunola/foodie",
    "source_id": "curry-1",
    "title": "Creamy Chicken Curry",
    "servings": 4.0,
    "ingredients": [
        {"canonical": "chicken", "amount": "500", "unit": "g"},
        {"canonical": "yogurt", "amount": "1", "unit": "cup"},
        {"canonical": "salt", "amount": None, "unit": None},
    ],
}


def _resolve(dataset_id: str, source_id: str) -> dict[str, Any] | None:
    if (dataset_id, source_id) == ("odunola/foodie", "curry-1"):
        return dict(DOC)
    return None


def _option(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "dataset_id": "odunola/foodie",
        "source_id": "curry-1",
        "title": "Creamy Chicken Curry",
        "quantities": [{"ingredient": "chicken", "amount": "500", "unit": "g"}],
        "adaptations": [],
    }
    base.update(overrides)
    return base


def test_valid_options_pass() -> None:
    assert (
        validate_options(
            [_option(), _option(title="Second")],
            resolve=_resolve,
            hard_keys=set(),
            honored=[],
            allow_single=False,
        )
        == []
    )


def test_unsourced_ids_rejected() -> None:
    errors = validate_options(
        [_option(dataset_id="evil", source_id="666")],
        resolve=_resolve,
        hard_keys=set(),
        honored=[],
        allow_single=True,
    )
    assert any("not in corpus" in e for e in errors)


def test_invented_quantities_rejected() -> None:
    errors = validate_options(
        [_option(quantities=[{"ingredient": "chicken", "amount": "9999", "unit": "kg"}])],
        resolve=_resolve,
        hard_keys=set(),
        honored=[],
        allow_single=True,
    )
    assert any("invented" in e for e in errors)


def test_unknown_quantity_stays_unknown() -> None:
    # salt has no parsed amount: any stated quantity for it is invented.
    errors = validate_options(
        [_option(quantities=[{"ingredient": "salt", "amount": "1", "unit": "tsp"}])],
        resolve=_resolve,
        hard_keys=set(),
        honored=[],
        allow_single=True,
    )
    assert any("invented" in e for e in errors)


def test_unlabelled_adaptation_rejected() -> None:
    errors = validate_options(
        [_option(adaptations=[{"description": "use tofu", "label": "fact"}])],
        resolve=_resolve,
        hard_keys=set(),
        honored=[],
        allow_single=True,
    )
    assert any("adaptation" in e for e in errors)
    ok = validate_options(
        [_option(adaptations=[{"description": "use tofu", "label": "adaptation"}])],
        resolve=_resolve,
        hard_keys=set(),
        honored=[],
        allow_single=True,
    )
    assert ok == []


def test_dropped_hard_constraint_rejected() -> None:
    assert hard_constraint_keys({"dietary_constraints": ["vegetarian"]}) == {"dietary_constraints"}
    assert hard_constraint_keys({"dietary_constraints": []}) == set()
    errors = validate_options(
        [_option()],
        resolve=_resolve,
        hard_keys={"dietary_constraints"},
        honored=[],
        allow_single=True,
    )
    assert any("dropped hard constraint" in e for e in errors)
    ok = validate_options(
        [_option()],
        resolve=_resolve,
        hard_keys={"dietary_constraints"},
        honored=["dietary_constraints"],
        allow_single=True,
    )
    assert ok == []


def test_single_option_needs_direct_lookup() -> None:
    errors = validate_options(
        [_option()], resolve=_resolve, hard_keys=set(), honored=[], allow_single=False
    )
    assert any("direct_recipe_lookup" in e for e in errors)


def test_quantity_matching_numeric() -> None:
    assert quantity_in_source({"ingredient": "Chicken", "amount": "500.0", "unit": "g"}, DOC)
    assert not quantity_in_source({"ingredient": "chicken", "amount": "500", "unit": "kg"}, DOC)
    assert not quantity_in_source({"ingredient": "tofu", "amount": "1", "unit": "g"}, DOC)


def test_plan_validation() -> None:
    dish = {"dataset_id": "odunola/foodie", "source_id": "curry-1", "title": "T"}
    plan = {
        "source": {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
        "mise_en_place": ["chop"],
        "steps": ["cook"],
        "plating": "bowl",
    }
    assert validate_plan(plan, selected_dish=dish, resolve=_resolve) == []
    assert validate_plan(plan, selected_dish=None, resolve=_resolve) != []
    wrong = dict(plan, source={"dataset_id": "x", "source_id": "y"})
    assert any(
        "selected dish" in e for e in validate_plan(wrong, selected_dish=dish, resolve=_resolve)
    )
    empty = dict(plan, steps=[])
    assert any("steps" in e for e in validate_plan(empty, selected_dish=dish, resolve=_resolve))
