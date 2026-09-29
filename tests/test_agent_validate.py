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
    assert any("Epicure consulted" in e for e in errors)


def test_unretrieved_valid_id_rejected() -> None:
    errors = validate_options(
        [_option(quantities=[])],
        resolve=_resolve,
        hard_keys=set(),
        honored=[],
        allow_single=True,
        retrieved=set(),
        full=set(),
    )
    assert any("was not retrieved in this session" in e for e in errors)


def test_cross_dataset_id_rejected() -> None:
    retrieved = {("other/dataset", "curry-1")}
    errors = validate_options(
        [_option(quantities=[])],
        resolve=_resolve,
        hard_keys=set(),
        honored=[],
        allow_single=True,
        retrieved=retrieved,
        full=set(),
    )
    assert any("was not retrieved in this session" in e for e in errors)


def test_retrieved_search_only_option_without_quantities_passes() -> None:
    errors = validate_options(
        [_option(quantities=[])],
        resolve=_resolve,
        hard_keys=set(),
        honored=[],
        allow_single=True,
        retrieved={("odunola/foodie", "curry-1")},
        full=set(),
    )
    assert errors == []


def test_search_only_option_with_quantities_rejected() -> None:
    errors = validate_options(
        [_option()],
        resolve=_resolve,
        hard_keys=set(),
        honored=[],
        allow_single=True,
        retrieved={("odunola/foodie", "curry-1")},
        full=set(),
    )
    assert any("need a get_recipe result" in e for e in errors)


def test_full_evidence_option_with_quantities_passes() -> None:
    errors = validate_options(
        [_option()],
        resolve=_resolve,
        hard_keys=set(),
        honored=[],
        allow_single=True,
        retrieved={("odunola/foodie", "curry-1")},
        full={("odunola/foodie", "curry-1")},
    )
    assert errors == []


def test_plan_needs_full_recipe_evidence() -> None:
    plan = {
        "source": {"dataset_id": "odunola/foodie", "source_id": "curry-1"},
        "mise_en_place": ["dice chicken"],
        "steps": ["cook"],
        "plating": "bowls",
    }
    dish = {"dataset_id": "odunola/foodie", "source_id": "curry-1"}
    errors = validate_plan(plan, selected_dish=dish, resolve=_resolve, full=set())
    assert any("needs a get_recipe result" in e for e in errors)
    assert (
        validate_plan(
            plan,
            selected_dish=dish,
            resolve=_resolve,
            full={("odunola/foodie", "curry-1")},
        )
        == []
    )


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


def _resolve_technique(doc_id: str, chunk_id: int) -> dict[str, object] | None:
    if (doc_id, chunk_id) == ("tech-sear-01", 0):
        return {
            "doc_id": doc_id,
            "chunk_id": chunk_id,
            "url": "https://en.wikipedia.org/wiki/Searing",
            "licence": "CC-BY-SA-4.0",
            "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
            "attribution_text": '"Searing" — test attribution',
        }
    return None


def test_technique_refs_valid_when_returned_and_resolving() -> None:
    from culinary_copilot.agent.validate import validate_technique_refs

    errors, resolved = validate_technique_refs(
        [{"doc_id": "tech-sear-01", "chunk_id": 0}],
        resolve_technique=_resolve_technique,
        returned={("tech-sear-01", 0)},
    )
    assert errors == []
    assert len(resolved) == 1
    assert resolved[0]["attribution_text"]


def test_technique_refs_reject_unreturned_unresolved_and_recipe_keys() -> None:
    from culinary_copilot.agent.validate import validate_technique_refs

    errors, _ = validate_technique_refs(
        [{"doc_id": "tech-sear-01", "chunk_id": 9}],
        resolve_technique=_resolve_technique,
        returned={("tech-sear-01", 0)},
    )
    assert any("not returned in this session" in e for e in errors)
    errors, _ = validate_technique_refs(
        [{"doc_id": "tech-nope-01", "chunk_id": 0}],
        resolve_technique=_resolve_technique,
        returned={("tech-nope-01", 0)},
    )
    assert any("does not resolve" in e for e in errors)
    errors, _ = validate_technique_refs(
        [{"doc_id": "tech-sear-01", "chunk_id": 0, "dataset_id": "x", "source_id": "y"}],
        resolve_technique=_resolve_technique,
        returned={("tech-sear-01", 0)},
    )
    assert any("never recipe sources" in e for e in errors)
    errors, _ = validate_technique_refs(
        "not-a-list", resolve_technique=_resolve_technique, returned=set()
    )
    assert errors != []


def test_options_reject_technique_keys() -> None:
    errors = validate_options(
        [_option(doc_id="tech-sear-01", chunk_id=0)],
        resolve=_resolve,
        hard_keys=set(),
        honored=[],
        allow_single=True,
    )
    assert any("never be an option" in e or "not options" in e for e in errors)
