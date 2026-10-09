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


# --- P3-L-07 minimum hard-constraint control ---------------------------------


def _doc_with(*canonicals: str) -> dict[str, Any]:
    return {"ingredients": [{"canonical": name} for name in canonicals]}


def test_dietary_chicken_stock_dropped_for_vegetarian() -> None:
    from culinary_copilot.agent.validate import check_dietary_option

    errors, entry = check_dietary_option(0, {}, _doc_with("chicken", "chicken stock"), "vegetarian")
    assert len(errors) == 2
    assert all("violates dietary constraint 'vegetarian'" in e for e in errors)
    assert entry["status"] == "violated"
    assert entry["terms"] == ["chicken"]


def test_dietary_vegetable_bouillon_passes() -> None:
    from culinary_copilot.agent.validate import check_dietary_option

    errors, entry = check_dietary_option(0, {}, _doc_with("vegetable bouillon"), "vegetarian")
    assert errors == []
    assert entry == {"status": "checked", "value": "vegetarian"}


def test_dietary_bare_bouillon_unverified() -> None:
    from culinary_copilot.agent.validate import check_dietary_option

    errors, entry = check_dietary_option(0, {}, _doc_with("bouillon"), "vegetarian")
    assert errors == []
    assert entry == {"status": "unverified", "value": "vegetarian", "terms": ["bouillon"]}


def test_dietary_eggplant_passes_vegan() -> None:
    from culinary_copilot.agent.validate import check_dietary_option

    errors, entry = check_dietary_option(
        0, {}, _doc_with("eggplant", "coconut milk", "peanut butter"), "vegan"
    )
    assert errors == []
    assert entry == {"status": "checked", "value": "vegan"}


def test_dietary_vegan_flags_dairy_eggs_honey() -> None:
    from culinary_copilot.agent.validate import check_dietary_option

    errors, entry = check_dietary_option(
        0, {}, _doc_with("milk", "egg", "honey", "cream of tartar"), "vegan"
    )
    assert len(errors) == 3
    assert entry["status"] == "violated"
    assert sorted(entry["terms"]) == ["egg", "honey", "milk"]


def test_dietary_unknown_value_not_checked() -> None:
    from culinary_copilot.agent.validate import check_dietary_option

    errors, entry = check_dietary_option(0, {}, _doc_with("chicken"), "gluten-free")
    assert errors == []
    assert entry == {"status": "not_checked", "value": "gluten-free"}


# --- P3-L-09 minimum plan evidence --------------------------------------------


def test_plan_raw_chicken_needs_safety_ref() -> None:
    from culinary_copilot.agent.validate import check_plan_evidence

    errors, steps_source = check_plan_evidence(
        {
            "adaptations": [],
            "steps": ["Cook."],
            "step_sources": [0],
        },
        {"ingredients": [{"canonical": "chicken breast"}], "instructions": ["Cook."]},
        [],
    )
    assert steps_source == "source"
    assert errors == [
        "plan uses raw chicken: cite a food-safety chunk "
        "(search_techniques for safe internal temperatures) with a technique_ref"
    ]


def test_plan_raw_chicken_accepted_with_safety_ref() -> None:
    from culinary_copilot.agent.validate import check_plan_evidence

    errors, steps_source = check_plan_evidence(
        {
            "adaptations": [],
            "steps": ["Cook."],
            "step_sources": [0],
        },
        {"ingredients": [{"canonical": "chicken breast"}], "instructions": ["Cook."]},
        [{"doc_id": "tech-fda-safe-32", "chunk_id": 0}],
    )
    assert (errors, steps_source) == ([], "source")


def test_plan_fully_cooked_fillets_need_no_ref() -> None:
    from culinary_copilot.agent.validate import check_plan_evidence

    errors, steps_source = check_plan_evidence(
        {
            "adaptations": [],
            "steps": ["Heat."],
            "step_sources": [0],
        },
        {
            "ingredients": [{"canonical": "fully cooked chicken fillets"}],
            "instructions": ["Heat."],
        },
        [],
    )
    assert (errors, steps_source) == ([], "source")


def test_plan_ingredient_only_record_needs_admission() -> None:
    from culinary_copilot.agent.validate import check_plan_evidence

    doc = {"ingredients": [{"canonical": "red lentils"}]}
    errors, steps_source = check_plan_evidence({"adaptations": []}, doc, [])
    assert steps_source == "model_adaptation"
    assert errors == [
        "steps_source is model_adaptation (the source has no directions): "
        "add a plan adaptation stating the steps are not from the source"
    ]
    errors, _ = check_plan_evidence(
        {
            "adaptations": [
                {
                    "label": "adaptation",
                    "description": "Steps are model-created: not from the source.",
                }
            ]
        },
        doc,
        [],
    )
    assert errors == []


# --- unnamed restriction + allergen avoidance (P3-L-13) ---------------------------


def test_unnamed_allergy_needs_asking() -> None:
    from culinary_copilot.agent.validate import unresolved_unnamed_restriction

    assert unresolved_unnamed_restriction(["a friend with a food allergy"], {}, []) is True
    assert unresolved_unnamed_restriction(["she can't eat some things"], {}, []) is True
    assert unresolved_unnamed_restriction([], {"note": "has a food allergy"}, []) is True


def test_named_restriction_does_not_trigger() -> None:
    from culinary_copilot.agent.validate import unresolved_unnamed_restriction

    assert unresolved_unnamed_restriction(["peanut allergy, avoid it"], {}, []) is False
    assert unresolved_unnamed_restriction(["allergic to shellfish"], {}, []) is False
    assert unresolved_unnamed_restriction(["I'm vegetarian, suggest dinner"], {}, []) is False


def test_naming_answer_resolves() -> None:
    from culinary_copilot.agent.validate import unresolved_unnamed_restriction

    confirmed = [{"question_id": "q-allergy", "answer": "She is allergic to peanuts."}]
    assert unresolved_unnamed_restriction(["a friend with a food allergy"], {}, confirmed) is False


def test_dietary_needs_paraphrase_not_caught() -> None:
    # Documented narrowness gap: no listed pattern word, so the
    # deterministic check stays silent (the framing still tells the
    # model to ask).
    from culinary_copilot.agent.validate import unresolved_unnamed_restriction

    assert unresolved_unnamed_restriction(["dietary needs for a guest"], {}, []) is False


def test_allergen_answer_naming() -> None:
    from culinary_copilot.agent.validate import allergens_named_in_answers

    assert allergens_named_in_answers(["She is allergic to peanuts."]) == ["peanut"]
    assert allergens_named_in_answers(["dairy and sesame please"]) == ["milk/dairy", "sesame"]
    assert allergens_named_in_answers(["strawberries"]) == []


def _allergen_doc(lines: list[str]) -> dict[str, Any]:
    return {
        "dataset_id": "odunola/foodie",
        "source_id": "x-1",
        "ingredients": [{"canonical": line} for line in lines],
    }


def test_allergen_option_violated() -> None:
    from culinary_copilot.agent.validate import check_allergen_option

    doc = _allergen_doc(["2 tbsp peanut butter", "1 cup flour"])
    errors, entry = check_allergen_option(0, _option(), doc, "peanut")
    assert entry["status"] == "violated"
    assert entry["terms"] == ["peanut"]
    assert entry["disclaimer"] == "term list is incomplete; not an allergen-free guarantee"
    assert any("peanut" in e and "confirmed allergy" in e for e in errors)


def test_allergen_option_no_terms_found_when_clean() -> None:
    from culinary_copilot.agent.validate import check_allergen_option

    errors, entry = check_allergen_option(0, _option(), dict(DOC), "peanut")
    assert errors == []
    assert entry["status"] == "no_listed_terms_found"
    assert entry["value"] == "peanut"
    assert entry["terms"] == []
    assert entry["unverified_terms"] == []
    assert entry["disclaimer"] == "term list is incomplete; not an allergen-free guarantee"


def test_allergen_option_unmapped_label_not_checked() -> None:
    from culinary_copilot.agent.validate import check_allergen_option

    errors, entry = check_allergen_option(0, _option(), dict(DOC), "strawberry")
    assert errors == []
    assert entry["status"] == "not_checked"
    assert entry["disclaimer"] == "term list is incomplete; not an allergen-free guarantee"


def test_allergen_dairy_scrub_keeps_almond_milk() -> None:
    from culinary_copilot.agent.validate import check_allergen_option

    doc = _allergen_doc(["1 cup almond milk"])
    errors, entry = check_allergen_option(0, _option(), doc, "milk/dairy")
    assert errors == []
    assert entry["status"] == "no_listed_terms_found"
    nut_errors, nut_entry = check_allergen_option(0, _option(), doc, "tree nuts")
    assert nut_entry["status"] == "violated"
    assert nut_errors != []


# --- allergen tiers, review rework -------------------------------------------------
#
# Every line from the review table gets its tier below: violated drops
# the option, unverified keeps it but is listed, anything else is
# "no_listed_terms_found" (never "checked") with the disclaimer.


def _tier(line: str, label: str) -> tuple[str, dict[str, Any]]:
    from culinary_copilot.agent.validate import check_allergen_option

    errors, entry = check_allergen_option(0, _option(), _allergen_doc([line]), label)
    return ("violated" if errors else entry["status"]), entry


def test_allergen_review_table_violated() -> None:
    cases = [
        ("2 cups all-purpose flour", "wheat/gluten"),
        ("8 oz spaghetti", "wheat/gluten"),
        ("1 cup panko breadcrumbs", "wheat/gluten"),
        ("2 tbsp soy sauce", "wheat/gluten"),
        ("1/2 cup mayonnaise", "egg"),
        ("2 tsp aioli", "egg"),
        ("meringue topping", "egg"),
        ("1/2 cup grated parmesan", "milk/dairy"),
        ("50 g gruyere", "milk/dairy"),
        ("1 cup custard", "milk/dairy"),
        ("2 scoops vanilla ice cream", "milk/dairy"),
        ("100 g mozzarella", "milk/dairy"),
        ("1 tbsp fish sauce", "fish"),
        ("1 tbsp oyster sauce", "shellfish"),
        ("2 tbsp soy sauce", "soy"),
        ("1 tbsp tamari", "soy"),
        ("100 g tempeh", "soy"),
        ("30 g macadamia nuts", "tree nuts"),
        ("1/4 cup brazil nuts", "tree nuts"),
        ("2 tbsp pine nuts", "tree nuts"),
        ("praline paste", "tree nuts"),
        ("marzipan layer", "tree nuts"),
        ("nutella spread", "tree nuts"),
        ("2 tbsp peanut butter", "peanut"),
    ]
    for line, label in cases:
        status, entry = _tier(line, label)
        assert status == "violated", (line, label, entry)
        assert entry["terms"], (line, label)
        assert entry["disclaimer"] == "term list is incomplete; not an allergen-free guarantee", (
            line,
            label,
        )


def test_allergen_review_table_unverified() -> None:
    cases = [
        ("2 chicken stock cubes", "wheat/gluten"),
        ("1 tbsp bouillon", "wheat/gluten"),
        ("1 tsp malt vinegar", "wheat/gluten"),
        ("1 tsp seasoning", "wheat/gluten"),
        ("1 cup rolled oats", "wheat/gluten"),
        ("1 cup custard", "egg"),
        ("200 g fresh pasta", "egg"),
        ("1 cup batter", "egg"),
        ("1 tsp Worcestershire sauce", "fish"),
        ("2 tbsp Caesar dressing", "fish"),
        ("1 cup seafood stock", "shellfish"),
        ("1 tsp XO sauce", "shellfish"),
        ("2 tbsp vegetable oil", "soy"),
        ("1 tsp lecithin", "soy"),
        ("1 tsp furikake", "sesame"),
        ("1 tsp za'atar", "sesame"),
    ]
    for line, label in cases:
        status, entry = _tier(line, label)
        assert status == "unverified", (line, label, entry)
        assert entry["unverified_terms"], (line, label)
        assert entry["terms"] == [], (line, label)
        assert entry["disclaimer"] == "term list is incomplete; not an allergen-free guarantee", (
            line,
            label,
        )


def test_allergen_exemptions_and_edges() -> None:
    cases = [
        # (line, label, expected status)
        ("1 cup almond flour", "wheat/gluten", "no_listed_terms_found"),
        ("1 cup almond flour", "tree nuts", "violated"),
        ("200 g rice noodles", "wheat/gluten", "no_listed_terms_found"),
        ("200 g glass noodles", "wheat/gluten", "no_listed_terms_found"),
        ("8 corn tortillas", "wheat/gluten", "no_listed_terms_found"),
        ("8 flour tortillas", "wheat/gluten", "violated"),
        ("gluten-free bread", "wheat/gluten", "no_listed_terms_found"),
        ("tamari soy sauce", "wheat/gluten", "no_listed_terms_found"),
        ("tamari soy sauce", "soy", "violated"),
        ("1 eggplant", "egg", "no_listed_terms_found"),
    ]
    for line, label, expected in cases:
        status, entry = _tier(line, label)
        assert status == expected, (line, label, entry)


def test_allergen_tables_share_p3l07() -> None:
    from culinary_copilot.agent.validate import (
        ALLERGEN_VIOLATED_TERMS,
        VEGAN_EXTRA_TERMS,
        VEGETARIAN_VIOLATION_TERMS,
        _alt_flour_phrases,
    )
    from culinary_copilot.recommendations.policy import COMPOUND_EXCEPTIONS

    # Dairy reuses the P3-L-07 vegan list: named cheeses and paneer
    # come through the shared table, not a duplicate.
    dairy = set(ALLERGEN_VIOLATED_TERMS["milk/dairy"])
    for cheese in (
        "mozzarella",
        "parmesan",
        "cheddar",
        "feta",
        "ricotta",
        "mascarpone",
        "brie",
        "paneer",
    ):
        assert cheese in dairy and cheese in VEGAN_EXTRA_TERMS, cheese
    # Alternative-flour exemptions reuse the shared compound table;
    # oat flour stays exempt from "flour" and is marked unverified via
    # the "oat" term instead (Phase 7 review).
    shared_flours = {c for c in COMPOUND_EXCEPTIONS if c.endswith(" flour")}
    assert {"rice flour", "almond flour", "coconut flour", "oat flour"} <= shared_flours
    assert shared_flours <= set(_alt_flour_phrases())
    # Fish/shellfish overlap with the P3-L-07 vegetarian list.
    assert set(ALLERGEN_VIOLATED_TERMS["fish"]) <= set(VEGETARIAN_VIOLATION_TERMS)
    assert set(ALLERGEN_VIOLATED_TERMS["shellfish"]) - {"shellfish"} <= set(
        VEGETARIAN_VIOLATION_TERMS
    )


def test_quantity_matching_mixed_fractions() -> None:
    # P7-MIXED-01: exact rationals via the shared ingestion parser
    # (recipes/normalize.py::quantity). No tolerance, units still exact.
    from culinary_copilot.agent.validate import _numbers_equal

    assert _numbers_equal("1 1/2", "1.5") is True
    assert _numbers_equal("1 1/2", "3/2") is True
    assert _numbers_equal("1.5", "3/2") is True
    assert _numbers_equal("0.33", "1/3") is False
    assert _numbers_equal("a lot", "1.5") is False
    assert _numbers_equal("1.5", "a lot") is False
    assert _numbers_equal("1 1/2", "2") is False
    flour_doc = {
        "ingredients": [
            {"canonical": "flour", "amount": "1.5", "unit": "cup"},
        ]
    }
    assert quantity_in_source({"ingredient": "flour", "amount": "1 1/2", "unit": "cup"}, flour_doc)
    assert not quantity_in_source(
        {"ingredient": "flour", "amount": "1 1/2", "unit": "g"}, flour_doc
    )


def test_plan_attribution_needs_full_direction_coverage() -> None:
    # Close-out review: every stored direction index must be cited by
    # at least one step. A faithful subset that drops the marinade
    # direction is model_adaptation, not source.
    from culinary_copilot.agent.validate import check_plan_evidence

    doc = {
        "ingredients": [{"canonical": "lentils"}],
        "instructions": ["Mix the spices.", "Marinate overnight.", "Cook and serve."],
    }
    partial = {
        "adaptations": [
            {
                "description": (
                    "The marinade is omitted; steps are model adaptations not from the source.",
                ),
                "label": "adaptation",
            }
        ],
        "steps": ["Mix the spices", "Cook and serve"],
        "step_sources": [0, 2],
    }
    errors, steps_source = check_plan_evidence(partial, doc, [])
    assert steps_source == "model_adaptation"
    assert errors == []
    full = dict(partial)
    full["steps"] = ["Mix the spices", "Marinate overnight", "Cook and serve"]
    full["step_sources"] = [0, 1, 2]
    full["adaptations"] = []
    errors, steps_source = check_plan_evidence(full, doc, [])
    assert (errors, steps_source) == ([], "source")


def test_allergen_oat_compounds() -> None:
    # Phase 7 review: oatmeal is unverified (single token, missed by
    # word-boundary "oat"); oat flour is unverified via "oat", never
    # violated (it is not wheat); goat cheese stays clean.
    from culinary_copilot.agent.validate import check_allergen_option

    def _status(line: str) -> tuple[str, dict[str, object]]:
        doc = {"ingredients": [{"canonical": line, "quantity_text": line}]}
        _, entry = check_allergen_option(0, {}, doc, "wheat/gluten")
        assert isinstance(entry, dict)
        return str(entry.get("status")), entry

    status, entry = _status("1 cup oatmeal")
    assert status == "unverified", entry
    assert "oatmeal" in entry.get("unverified_terms", [])
    errors, entry = check_allergen_option(
        0,
        {},
        {"ingredients": [{"canonical": "oat flour", "quantity_text": "2 tbsp oat flour"}]},
        "wheat/gluten",
    )
    assert entry.get("status") == "unverified", entry
    assert "oat" in entry.get("unverified_terms", [])
    assert not entry.get("terms"), entry
    assert not errors, entry
    errors, entry = check_allergen_option(
        0,
        {},
        {"ingredients": [{"canonical": "flour", "quantity_text": "2 tbsp flour"}]},
        "wheat/gluten",
    )
    assert entry.get("status") == "violated", entry
    assert errors, entry
    status, entry = _status("30 g goat cheese")
    assert status == "no_listed_terms_found", entry


def test_allergen_safety_claim_detector() -> None:
    from culinary_copilot.agent.validate import (
        allergen_claim_allowed,
        allergen_safety_claim,
    )

    assert allergen_safety_claim("These options are peanut-free.") == "peanut-free"
    assert allergen_safety_claim("Safe for her allergy.") == "Safe for her allergy"
    assert allergen_safety_claim("All gluten-free options here.") == "gluten-free"
    assert allergen_safety_claim("The chicken is tender.") is None
    assert allergen_claim_allowed("peanut-free", ["peanut-free cookies"]) is True
    assert allergen_claim_allowed("peanut-free", ["chicken and rice"]) is False


def test_vegetarian_rennet_cheeses_are_unverified() -> None:
    """Parmesan and similar cheeses keep the option but are never "checked".

    Demo finding (2026-10-05): three "vegetarian" pasta options used
    Parmesan, traditionally made with animal rennet.
    """
    from culinary_copilot.agent.validate import check_dietary_option

    def status(line: str, diet: str = "vegetarian") -> dict:
        return check_dietary_option(0, {}, {"ingredients": [{"canonical": line}]}, diet)

    errors, entry = status("grated parmesan cheese, or more to taste")
    assert errors == []
    assert entry == {"status": "unverified", "value": "vegetarian", "terms": ["parmesan"]}
    assert status("Pecorino Romano")[1]["terms"] == ["pecorino", "romano"]
    assert status("vegetarian parmesan-style cheese")[1]["status"] == "checked"
    assert status("part-skim ricotta cheese")[1]["status"] == "checked"
    assert status("animal rennet cheese")[1]["status"] == "violated"
    assert status("grated parmesan", "vegan")[1]["status"] == "violated"


def test_plan_rejection_names_the_words_missing_from_each_direction() -> None:
    # 2026-10-06 live session: a faithful plan with "drizzled" for
    # "drizzle" failed twice on a message naming only the step index.
    from culinary_copilot.agent.validate import check_plan_evidence

    doc = {
        "ingredients": [{"canonical": "tofu"}],
        "directions": ["Simmer the sauce for 5 minutes.", "Serve hot and drizzle with sauce."],
    }
    plan = {
        "steps": ["Simmer the sauce for 5 minutes.", "Serve hot, drizzled with sauce."],
        "step_sources": [0, 1],
        "adaptations": [],
    }
    errors, steps_source = check_plan_evidence(plan, doc, [])
    assert steps_source == "model_adaptation"
    assert len(errors) == 1
    assert "step 1 uses words not in direction 1: 'drizzled'" in errors[0]
    assert "cited direction's own words" in errors[0]
    plan["steps"][1] = "Serve hot and drizzle with sauce."
    assert check_plan_evidence(plan, doc, []) == ([], "source")


def test_attribution_folds_accents_and_ignores_bare_citation_tags() -> None:
    # 2026-10-07 live session: "jalapeños" split into "jalape" and "os",
    # and "[Source direction N]" tags made faithful steps ungrounded.
    from culinary_copilot.agent.validate import direction_supports_step

    direction = "Whisk soy sauce, jalapeno peppers, and garlic together."
    assert direction_supports_step(direction, "Whisk soy sauce, jalapeño peppers and garlic.")
    assert direction_supports_step(direction, "Whisk soy sauce and garlic. [Source direction 1]")
    assert direction_supports_step(direction, "Whisk the garlic (directions 1-2).")
    # A tag carrying other words is content and still has to match.
    assert not direction_supports_step(
        direction, "Whisk soy sauce. [Source direction 1; temperature guidance cited below]"
    )


def test_attribution_detail_does_not_call_cited_directions_uncited() -> None:
    from culinary_copilot.agent.validate import plan_attribution_note

    doc = {
        "ingredients": [{"canonical": "tofu"}],
        "directions": ["Simmer the sauce.", "Serve hot.", "Garnish with herbs."],
    }
    plan = {
        "steps": ["Simmer the sauce.", "Serve hot with rice."],
        "step_sources": [0, 1],
        "adaptations": [],
    }
    note = plan_attribution_note(plan, doc)
    assert note is not None
    assert "step 1 uses words not in direction 1: 'rice'" in note
    assert "source directions [2] are not cited by any step" in note


def test_plan_prose_amounts_must_match_the_source() -> None:
    # 2026-10-07 live session: the mise en place said "1 1/2 lb" for a
    # source amount of 5 1/2 pounds (stored as the exact fraction 11/2).
    # 2026-10-07 H2: ingredient-aware — direction-only amounts need a
    # citation, and plating must name its ingredient (generic "sauce"
    # no longer passes pooled).
    from culinary_copilot.agent.validate import plan_prose_quantity_errors, prose_quantities

    doc = {
        "ingredients": [
            {"canonical": "cut-up chicken parts", "amount": "11/2", "unit": "lb"},
            {"canonical": "annatto powder", "amount": "3/2", "unit": "tsp"},
            {"canonical": "vegetable oil, divided", "amount": "3", "unit": "tbsp"},
        ],
        "instructions": ["Heat 2 tablespoons vegetable oil; cook 4 minutes per side."],
    }
    wrong = {"mise_en_place": ["Have 1 1/2 lb cut-up chicken parts."], "steps": ["Serve."]}
    errors = plan_prose_quantity_errors(wrong, doc)
    assert len(errors) == 1 and "'1 1/2 lb'" in errors[0] and "mise_en_place" in errors[0]
    right = {
        "mise_en_place": ["Have 5 ½ pounds chicken parts and 1.5 tsp annatto powder."],
        "steps": ["Heat 2 tablespoons oil, 4 minutes per side, to 165°F."],
        "step_sources": [0],
        "plating": "Serve 3 tablespoons vegetable oil over each plate with 10 bay leaves.",
    }
    assert plan_prose_quantity_errors(right, doc) == []
    # Counts, times and temperatures are not amounts this check reads.
    assert prose_quantities("1 head garlic, 2 cloves, 20 minutes, 165 F") == []


def _h2_doc() -> dict[str, Any]:
    return {
        "ingredients": [
            {
                "canonical": "cut-up chicken parts",
                "name": "cut-up chicken parts",
                "amount": "11/2",
                "amount_text": "5 1/2",
                "unit": "lb",
                "original": "5 1/2 lb cut-up chicken parts",
            },
            {
                "canonical": "potatoes",
                "name": "potatoes",
                "amount": "3/2",
                "amount_text": "1 1/2",
                "unit": "lb",
                "original": "1 1/2 lb potatoes",
            },
            {
                "canonical": "vegetable oil",
                "name": "vegetable oil",
                "amount": "3",
                "amount_text": "3",
                "unit": "tbsp",
                "original": "3 tbsp vegetable oil",
            },
        ],
        "instructions": ["Heat 2 tablespoons vegetable oil; cook."],
    }


def test_h2_swapped_ingredients_rejected() -> None:
    # H2: pooled check let "1 1/2 lb chicken" pass for a 5 1/2 lb source
    # when potatoes were 1 1/2 lb. Ingredient-aware rejects both swaps.
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = _h2_doc()
    swapped_chicken = {
        "mise_en_place": ["Have 1 1/2 lb cut-up chicken parts."],
        "steps": ["Serve."],
        "plating": "Serve.",
    }
    errors = plan_prose_quantity_errors(swapped_chicken, doc)
    assert len(errors) == 1 and "cut-up chicken parts" in errors[0]
    assert "5 1/2" in errors[0] or "11/2" in errors[0]
    swapped_potatoes = {
        "mise_en_place": ["Have 5 1/2 lb potatoes."],
        "steps": ["Serve."],
        "plating": "Serve.",
    }
    errors = plan_prose_quantity_errors(swapped_potatoes, doc)
    assert len(errors) == 1 and "potatoes" in errors[0]
    assert (
        plan_prose_quantity_errors(
            {
                "mise_en_place": ["Have 5 1/2 lb cut-up chicken parts."],
                "steps": ["S."],
                "plating": "S.",
            },
            doc,
        )
        == []
    )
    assert (
        plan_prose_quantity_errors(
            {"mise_en_place": ["Have 1 1/2 lb potatoes."], "steps": ["S."], "plating": "S."},
            doc,
        )
        == []
    )


def test_h2_notation_variants_pass() -> None:
    # H2: 5 1/2, 5 ½, 11/2 and 5.5 are the same exact rational (11/2).
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = _h2_doc()
    for amount in ("5 1/2", "5 ½", "11/2", "5.5"):
        plan = {
            "mise_en_place": [f"Have {amount} lb cut-up chicken parts."],
            "steps": ["Serve."],
            "plating": "Serve.",
        }
        assert plan_prose_quantity_errors(plan, doc) == [], amount


def test_h2_unit_variants_pass_through_canonical_unit() -> None:
    # H2: tablespoons/tbsp/Tbsp. all canonicalize to tbsp.
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = _h2_doc()
    for unit in ("tablespoons", "tbsp", "Tbsp.", "Tablespoons"):
        plan = {
            "mise_en_place": [f"Have 3 {unit} vegetable oil."],
            "steps": ["Serve."],
            "plating": "Serve.",
        }
        assert plan_prose_quantity_errors(plan, doc) == [], unit
    wrong = {
        "mise_en_place": ["Have 3 cups vegetable oil."],
        "steps": ["Serve."],
        "plating": "Serve.",
    }
    assert plan_prose_quantity_errors(wrong, doc) != []


def test_h2_per_serving_amount_rejected() -> None:
    # H2: a per-serving amount the source never states is rejected, even
    # when the total is stated.
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = _h2_doc()
    plan = {
        "mise_en_place": ["Have 1/2 lb cut-up chicken parts per serving."],
        "steps": ["Serve."],
        "plating": "Serve.",
    }
    errors = plan_prose_quantity_errors(plan, doc)
    assert len(errors) == 1 and "cut-up chicken parts" in errors[0]


def test_h2_direction_only_amount_needs_citation() -> None:
    # H2: "Heat 2 tablespoons oil" is stated only in a direction: it
    # passes when the step cites that direction, and is rejected when it
    # does not. The rejection names the direction to cite for the retry.
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = _h2_doc()
    cited = {
        "mise_en_place": ["Prep."],
        "steps": ["Heat 2 tablespoons oil."],
        "step_sources": [0],
        "plating": "Serve.",
    }
    assert plan_prose_quantity_errors(cited, doc) == []
    uncited = {
        "mise_en_place": ["Prep."],
        "steps": ["Heat 2 tablespoons oil."],
        "step_sources": [None],
        "plating": "Serve.",
    }
    errors = plan_prose_quantity_errors(uncited, doc)
    assert len(errors) == 1 and "[0]" in errors[0] and "step_sources" in errors[0]
    nocitation = {
        "mise_en_place": ["Prep."],
        "steps": ["Heat 2 tablespoons oil."],
        "plating": "Serve.",
    }
    assert plan_prose_quantity_errors(nocitation, doc) != []


def test_h2_plural_singular_adjective_forms_match() -> None:
    # H2: "chicken parts" and "cut-up chicken" both match the source
    # entry "cut-up chicken parts"; "potato" matches "potatoes".
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = _h2_doc()
    assert (
        plan_prose_quantity_errors(
            {"mise_en_place": ["Have 5 1/2 lb chicken parts."], "steps": ["S."], "plating": "S."},
            doc,
        )
        == []
    )
    assert (
        plan_prose_quantity_errors(
            {"mise_en_place": ["Have 5 1/2 lb cut-up chicken."], "steps": ["S."], "plating": "S."},
            doc,
        )
        == []
    )
    assert (
        plan_prose_quantity_errors(
            {"mise_en_place": ["Have 1 1/2 lb potato."], "steps": ["S."], "plating": "S."},
            doc,
        )
        == []
    )


def test_h2_name_match_needs_more_than_a_contained_word() -> None:
    # H2 review focus: a match is token-contiguous, never substring, so
    # "oil" never matches "boiled", and "chicken broth" prefers the broth
    # entry over bare "chicken" by longer match.
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = {
        "ingredients": [
            {"canonical": "vegetable oil", "amount": "3", "unit": "tbsp", "original": "3 tbsp oil"},
            {"canonical": "potatoes", "amount": "1", "unit": "lb", "original": "1 lb potatoes"},
        ],
        "instructions": ["Boil the potatoes."],
    }
    boiled = {
        "mise_en_place": ["Add 3 tbsp boiled potatoes."],
        "steps": ["Serve."],
        "plating": "Serve.",
    }
    errors = plan_prose_quantity_errors(boiled, doc)
    assert errors and "potatoes" in errors[0] and "vegetable oil" not in errors[0]
    broth_doc = {
        "ingredients": [
            {"canonical": "chicken", "amount": "1", "unit": "lb", "original": "1 lb chicken"},
            {
                "canonical": "chicken broth",
                "amount": "2",
                "unit": "cup",
                "original": "2 cups chicken broth",
            },
        ],
        "instructions": ["Simmer."],
    }
    assert (
        plan_prose_quantity_errors(
            {"mise_en_place": ["Add 2 cups chicken broth."], "steps": ["S."], "plating": "S."},
            broth_doc,
        )
        == []
    )
    errors = plan_prose_quantity_errors(
        {"mise_en_place": ["Add 1 cup chicken broth."], "steps": ["S."], "plating": "S."},
        broth_doc,
    )
    assert errors and "chicken broth" in errors[0]


def test_h2_unattributable_amounts_need_a_cited_direction() -> None:
    # H2 decision (a): "2 cups of the liquid" passes only when the same
    # amount and unit appear in a direction the step cites; mise_en_place
    # and plating have nothing to cite and are rejected. Never silent.
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = {
        "ingredients": [
            {"canonical": "flour", "amount": "2", "unit": "cup", "original": "2 cups flour"},
        ],
        "instructions": ["Add 2 cups of the liquid; stir."],
    }
    cited = {
        "mise_en_place": ["Prep."],
        "steps": ["Add 2 cups of the liquid."],
        "step_sources": [0],
        "plating": "Serve.",
    }
    assert plan_prose_quantity_errors(cited, doc) == []
    uncited_step = {
        "mise_en_place": ["Prep."],
        "steps": ["Add 2 cups of the liquid."],
        "step_sources": [None],
        "plating": "Serve.",
    }
    errors = plan_prose_quantity_errors(uncited_step, doc)
    assert len(errors) == 1 and "no source ingredient matches" in errors[0]
    mise = {
        "mise_en_place": ["Have 2 cups of the liquid."],
        "steps": ["Serve."],
        "plating": "Serve.",
    }
    errors = plan_prose_quantity_errors(mise, doc)
    assert len(errors) == 1 and "no source ingredient matches" in errors[0]


def test_h2_model_adaptation_steps_are_still_checked() -> None:
    # H2 rule: amounts in model_adaptation steps keep today's rule —
    # checked like any other step (adaptations descriptions stay
    # unchecked as labelled non-source text).
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = _h2_doc()
    plan = {
        "mise_en_place": ["Prep."],
        "steps": ["Add 9 lb cut-up chicken parts as a twist."],
        "step_sources": [None],
        "plating": "Serve.",
        "adaptations": [{"label": "adaptation", "description": "Not from the source."}],
    }
    errors = plan_prose_quantity_errors(plan, doc)
    assert len(errors) == 1 and "cut-up chicken parts" in errors[0]
    ok = {
        "mise_en_place": ["Prep."],
        "steps": ["Add 5 1/2 lb cut-up chicken parts as a twist."],
        "step_sources": [None],
        "plating": "Serve.",
        "adaptations": [{"label": "adaptation", "description": "Not from the source."}],
    }
    assert plan_prose_quantity_errors(ok, doc) == []


def test_h2r_function_words_never_match() -> None:
    # 2026-10-07 H2 revision: "of" alone never attributes "1 cup of
    # rice" to "cream of mushroom soup"; "2 cups of rice" passes.
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = {
        "ingredients": [
            {
                "canonical": "cream of mushroom soup",
                "amount": "1",
                "unit": "cup",
                "original": "1 cup cream of mushroom soup",
            },
            {"canonical": "rice", "amount": "2", "unit": "cup", "original": "2 cups rice"},
        ],
        "instructions": ["Cook."],
    }
    wrong = {"mise_en_place": ["Have 1 cup of rice."], "steps": ["S."], "plating": "S."}
    errors = plan_prose_quantity_errors(wrong, doc)
    assert len(errors) == 1 and "rice" in errors[0] and "2 cup" in errors[0]
    right = {"mise_en_place": ["Have 2 cups of rice."], "steps": ["S."], "plating": "S."}
    assert plan_prose_quantity_errors(right, doc) == []


def test_h2r_glued_prefix_needs_min_length() -> None:
    # 2026-10-07 H2 revision: bounded prefix (min 5) for glued text;
    # "oil" never matches "boiled", "salt" never matches "salted" via
    # prefix, but "butter" matches "Buttersoftened".
    from culinary_copilot.agent.validate import _token_prefix_match, plan_prose_quantity_errors

    assert not _token_prefix_match("boiled", "oil")
    assert not _token_prefix_match("oil", "boiled")
    assert not _token_prefix_match("salted", "salt")
    assert _token_prefix_match("buttersoftened", "butter")
    assert _token_prefix_match("pepperschopped", "peppers")
    doc = {
        "ingredients": [
            {"canonical": "butter", "amount": "75", "unit": "g", "original": "75 g butter"},
        ],
        "instructions": ["Cook."],
    }
    assert (
        plan_prose_quantity_errors(
            {"mise_en_place": ["Have 75 g Buttersoftened."], "steps": ["S."], "plating": "S."},
            doc,
        )
        == []
    )


def test_h2r_cited_direction_needs_same_ingredient() -> None:
    # 2026-10-07 H2 revision: pooled cited-direction bypass closed. A
    # step claiming chicken citing a potatoes direction still fails;
    # unattributed amounts keep the direction-only rule.
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = {
        "ingredients": [
            {
                "canonical": "cut-up chicken parts",
                "amount": "11/2",
                "unit": "lb",
                "original": "5 1/2 lb chicken",
            },
            {
                "canonical": "potatoes",
                "amount": "3/2",
                "unit": "lb",
                "original": "1 1/2 lb potatoes",
            },
        ],
        "instructions": ["Add 1 1/2 pounds potatoes."],
    }
    swapped = {
        "mise_en_place": ["Prep."],
        "steps": ["Add 1 1/2 lb chicken."],
        "step_sources": [0],
        "plating": "S.",
    }
    errors = plan_prose_quantity_errors(swapped, doc)
    assert len(errors) == 1 and "different ingredient" in errors[0]
    unattributed = {
        "mise_en_place": ["Prep."],
        "steps": ["Add 1 1/2 pounds of the mixture."],
        "step_sources": [0],
        "plating": "S.",
    }
    assert plan_prose_quantity_errors(unattributed, doc) == []


def test_h2r_name_then_amount_lists_pair_in_order() -> None:
    # 2026-10-07 H2 revision: "chicken, 5 1/2 lb, potatoes, 1 1/2 lb"
    # (correct) passes via order pairing; swapped amounts still fail.
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = _h2_doc()
    right = {
        "mise_en_place": ["chicken, 5 1/2 lb, potatoes, 1 1/2 lb"],
        "steps": ["S."],
        "plating": "S.",
    }
    assert plan_prose_quantity_errors(right, doc) == []
    wrong = {
        "mise_en_place": ["chicken, 1 1/2 lb, potatoes, 5 1/2 lb"],
        "steps": ["S."],
        "plating": "S.",
    }
    errors = plan_prose_quantity_errors(wrong, doc)
    assert len(errors) == 2


def test_h2r_ambiguous_names_candidates() -> None:
    # 2026-10-07 H2 revision: "1 cup oil" with two oils says ambiguous
    # and names candidates, not "no source ingredient matches".
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = {
        "ingredients": [
            {
                "canonical": "vegetable oil",
                "amount": "1",
                "unit": "cup",
                "original": "1 cup vegetable oil",
            },
            {
                "canonical": "olive oil",
                "amount": "2",
                "unit": "tbsp",
                "original": "2 tbsp olive oil",
            },
        ],
        "instructions": ["Cook."],
    }
    # 2026-10-07 H2R candidate-set: amount disambiguates tied names —
    # "1 cup oil" passes via vegetable oil (only it states 1 cup).
    assert (
        plan_prose_quantity_errors(
            {"mise_en_place": ["Have 1 cup oil."], "steps": ["S."], "plating": "S."}, doc
        )
        == []
    )
    # Ambiguous remains when none states it ("3 tbsp oil" matches neither
    # 1 cup nor 2 tbsp): names candidates, not "no source ingredient".
    errors = plan_prose_quantity_errors(
        {"mise_en_place": ["Have 3 tbsp oil."], "steps": ["S."], "plating": "S."}, doc
    )
    assert len(errors) == 1 and "ambiguous between" in errors[0]
    assert "vegetable oil" in errors[0] and "olive oil" in errors[0]
    assert "no source ingredient matches" not in errors[0]


def test_h2r2_same_ingredient_twice_passes_via_candidates() -> None:
    # 2026-10-07 H2R: model-style "1/2 cup butter" (stripped ", chilled")
    # passes when chilled states 1/2 cup, even though melted shares
    # "butter" (2 cups vs 2 1/4 cups flour likewise).
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = {
        "ingredients": [
            {
                "canonical": "butter, chilled",
                "amount": "1/2",
                "unit": "cup",
                "original": "1/2 cup butter, chilled",
            },
            {
                "canonical": "butter, melted",
                "amount": "3",
                "unit": "tbsp",
                "original": "3 tablespoons butter, melted",
            },
        ],
        "instructions": ["Cook."],
    }
    assert (
        plan_prose_quantity_errors(
            {"mise_en_place": ["have 1/2 cup butter"], "steps": ["S."], "plating": "S."},
            doc,
        )
        == []
    )
    assert (
        plan_prose_quantity_errors(
            {"mise_en_place": ["have 3 tablespoons butter"], "steps": ["S."], "plating": "S."},
            doc,
        )
        == []
    )


def test_h2r2_containers_never_match() -> None:
    # 2026-10-07 H2R: "can" never attributes pumpkin amounts to tomatoes.
    # Sources copy Vegetarian Pumpkin Spinach Chili originals.
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = {
        "ingredients": [
            {
                "canonical": "can diced tomatoes",
                "amount": "1",
                "unit": None,
                "original": "1 (28 ounce) can diced tomatoes",
            },
            {
                "canonical": "100% pure pumpkin",
                "amount": "1",
                "unit": "can",
                "original": "1 (14 ounce) can 100% pure pumpkin",
            },
        ],
        "instructions": ["Cook."],
    }
    wrong = {
        "mise_en_place": ["Have 1 (28 ounce) can of pure pumpkin."],
        "steps": ["S."],
        "plating": "S.",
    }
    errors = plan_prose_quantity_errors(wrong, doc)
    assert errors and "pure pumpkin" in errors[0]
    right = {
        "mise_en_place": ["Have 1 (14 ounce) can of pure pumpkin."],
        "steps": ["S."],
        "plating": "S.",
    }
    assert plan_prose_quantity_errors(right, doc) == []
    libby = {
        "ingredients": [
            {
                "canonical": "100% pure pumpkin",
                "amount": "15",
                "unit": "oz",
                "original": "1 (15 ounce) can pure pumpkin",
            }
        ],
        "instructions": ["Cook."],
    }
    assert (
        plan_prose_quantity_errors(
            {
                "mise_en_place": ["Have 1 (15 ounce) can pure pumpkin."],
                "steps": ["S."],
                "plating": "S.",
            },
            libby,
        )
        == []
    )


def test_h2r2_parenthetical_seconds_share_ingredient() -> None:
    # 2026-10-07 H2R: copies Turkey Injector Marinade originals.
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = {
        "ingredients": [
            {
                "canonical": "chicken stock",
                "amount": "3/4",
                "unit": "cup",
                "original": "¾ cup 178ml) chicken stock",
            },
            {
                "canonical": "chicken bouillon powder",
                "amount": "1",
                "unit": "tbsp",
                "original": "1 tablespoon (36g) chicken bouillon powder",
            },
        ],
        "instructions": ["Cook."],
    }
    assert (
        plan_prose_quantity_errors(
            {
                "mise_en_place": ["Have 3/4 cup (178 ml) of chicken stock."],
                "steps": ["S."],
                "plating": "S.",
            },
            doc,
        )
        == []
    )
    errors = plan_prose_quantity_errors(
        {
            "mise_en_place": ["Have 3/4 cup (178 ml) of chicken bouillon powder."],
            "steps": ["S."],
            "plating": "S.",
        },
        doc,
    )
    assert errors and "chicken bouillon powder" in errors[0]


def test_h2r2_glued_prefix_needs_prep_or_ingredient() -> None:
    # 2026-10-07 H2R: "1 cup buttermilk" (butter 1 cup, flour 2 cups)
    # must be rejected, not credited to butter via "butter"+"milk".
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = {
        "ingredients": [
            {"canonical": "butter", "amount": "1", "unit": "cup", "original": "1 cup butter"},
            {"canonical": "flour", "amount": "2", "unit": "cup", "original": "2 cups flour"},
        ],
        "instructions": ["Cook."],
    }
    errors = plan_prose_quantity_errors(
        {"mise_en_place": ["Have 1 cup buttermilk."], "steps": ["S."], "plating": "S."}, doc
    )
    assert errors and "no source ingredient matches" in errors[0]


def _mise_errors(lines: list[str], *ingredients: tuple[str, str | None, str, str]) -> list[str]:
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = {
        "ingredients": [
            {"amount": amount, "unit": unit, "canonical": canonical, "original": original}
            for amount, unit, canonical, original in ingredients
        ],
        "instructions": ["Cook."],
    }
    return plan_prose_quantity_errors(
        {"mise_en_place": lines, "steps": ["S."], "plating": "S."}, doc
    )


def test_h2r3_descriptor_words_never_carry_a_swap() -> None:
    # 2026-10-07 H2 third revision: a descriptor the model adds ("chopped",
    # "ground", "fresh") must not attribute the amount to the ingredient
    # whose name holds that descriptor.
    pecans = ("1/2", "cup", "chopped pecans", "1/2 cup chopped pecans")
    walnuts = ("1", "cup", "walnuts", "1 cup walnuts")
    assert _mise_errors(["1/2 cup chopped walnuts"], pecans, walnuts)
    assert not _mise_errors(["1 cup chopped walnuts"], pecans, walnuts)
    cumin = ("1", "tbsp", "ground cumin", "1 tablespoon ground cumin")
    cinnamon = ("1", "tsp", "cinnamon", "1 teaspoon cinnamon")
    assert _mise_errors(["1 tbsp ground cinnamon"], cumin, cinnamon)
    assert not _mise_errors(["1 tsp ground cinnamon"], cumin, cinnamon)
    basil = ("2", "cup", "fresh basil leaves", "2 cups fresh basil leaves")
    parsley = ("1/4", "cup", "parsley", "1/4 cup parsley")
    assert _mise_errors(["2 cups fresh parsley"], basil, parsley)
    assert not _mise_errors(["1/4 cup fresh parsley"], basil, parsley)


def test_h2r3_descriptor_ignored_when_names_carry_trailing_notes() -> None:
    # 2026-10-08 review: the head noun is the last distinctive token of
    # the whole name ("chunks" in "carrots, cut into large chunks"), so
    # no line mention held a head and "cut" sent "3 pound cut carrots" to
    # the corned beef ("..., cut in half").
    carrots = (
        "1",
        "pound",
        "carrots, cut into large chunks",
        "1 pound carrots, cut into large chunks",
    )
    beef = (
        "3",
        "pound",
        "corned beef brisket with spice packet, cut in half",
        "1 (3 pound) corned beef brisket with spice packet, cut in half",
    )
    assert _mise_errors(["3 pound cut carrots"], carrots, beef)
    assert not _mise_errors(["1 pound cut carrots"], carrots, beef)
    milk = ("3/4", "cup", "warm 2% milk, or as needed", "3/4 cup warm 2% milk, or as needed")
    butter = (
        "1/2",
        "cup",
        "softened butter, cut into chunks",
        "1/2 cup softened butter, cut into chunks",
    )
    assert _mise_errors(["1/2 cup softened warm 2% milk"], milk, butter)
    assert not _mise_errors(["3/4 cup warm 2% milk"], milk, butter)


def test_h2r3_size_words_never_match() -> None:
    # 2026-10-08 review: "inch" in "(1/4 inch)" attributed the celery
    # root amount to "apples, cut into 1/2-inch cubes".
    apples = (
        "2",
        None,
        "large crisp, sweet apples, cut into 1/2-inch cubes",
        "2 large crisp, sweet apples, cut into 1/2-inch cubes",
    )
    celery = ("1", "cup", "cubed celery root", "1 cup cubed (1/4 inch) celery root, drained well")
    assert not _mise_errors(["1 cup cubed (1/4 inch) celery root"], apples, celery)
    assert _mise_errors(["2 cups cubed (1/4 inch) celery root"], apples, celery)


def test_app_written_attribution_note() -> None:
    from culinary_copilot.agent.validate import check_plan_evidence, plan_attribution_note

    doc = {
        "ingredients": [{"canonical": "tofu"}],
        "directions": ["Simmer the sauce for 5 minutes.", "Serve hot and drizzle with sauce."],
    }
    plan = {
        "steps": ["Simmer the sauce for 5 minutes.", "Serve hot, drizzled with sauce."],
        "step_sources": [0, 1],
        "adaptations": [],
    }
    errors, steps_source = check_plan_evidence(plan, doc, [], auto_label=True)
    assert (errors, steps_source) == ([], "model_adaptation")
    note = plan_attribution_note(plan, doc)
    assert note is not None and note.startswith("Labelled by the app")
    assert "'drizzled'" in note
    # No note for a source plan, a plan that already admits it, or an
    # ingredient-only source (which still needs the model's admission).
    faithful = dict(plan, steps=["Simmer the sauce for 5 minutes.", "Serve hot."])
    assert plan_attribution_note(faithful, doc) is None
    admitted = dict(
        plan, adaptations=[{"label": "adaptation", "description": "Not from the source."}]
    )
    assert plan_attribution_note(admitted, doc) is None
    bare = {"ingredients": [{"canonical": "tofu"}]}
    assert plan_attribution_note(plan, bare) is None
    errors, _ = check_plan_evidence(plan, bare, [], auto_label=True)
    assert errors and "the source has no directions" in errors[0]


def test_generic_allergen_names_map_to_checked_labels() -> None:
    # 2026-10-07 live session: the answer "Tree nuts" named no mapped
    # allergen, so the options were never checked for tree nuts.
    from culinary_copilot.agent.validate import allergens_named_in_answers

    assert allergens_named_in_answers(["Tree nuts"]) == ["tree nuts"]
    assert allergens_named_in_answers(["tree-nut allergy"]) == ["tree nuts"]
    assert allergens_named_in_answers(["nuts"]) == ["peanut", "tree nuts"]
    assert allergens_named_in_answers(["celiac"]) == ["wheat/gluten"]
    assert allergens_named_in_answers(["coconut and nutmeg are fine"]) == []


def test_generic_nut_lines_fail_tree_nuts_and_stay_unverified_for_peanut() -> None:
    from culinary_copilot.agent.validate import check_allergen_option

    doc = {"ingredients": [{"canonical": "chopped nuts"}, {"canonical": "nutmeg"}]}
    errors, entry = check_allergen_option(0, {"title": "Nut Bars"}, doc, "tree nuts")
    assert errors and entry["status"] == "violated"
    errors, entry = check_allergen_option(0, {"title": "Nut Bars"}, doc, "peanut")
    assert not errors and entry["status"] == "unverified"
    clean = {"ingredients": [{"canonical": "nutmeg"}, {"canonical": "coconut milk"}]}
    errors, entry = check_allergen_option(0, {"title": "Spiced Rice"}, clean, "tree nuts")
    assert not errors and entry["status"] != "violated"


def _adaptation_plan(**overrides: Any) -> dict[str, Any]:
    # 2026-10-08 H3 part 2: a model_adaptation plan shape for fidelity
    # tests (steps differ from the source, so the label is adaptation).
    base: dict[str, Any] = {
        "mise_en_place": ["Chop the onion."],
        "steps": ["Brown the chicken with extra garlic.", "Serve hot."],
        "plating": "In warm bowls.",
        "adaptations": [],
    }
    base.update(overrides)
    return base


def test_plan_fidelity_note_claim_rejected() -> None:
    # H3 part 2: a model_adaptation plan whose note claims fidelity is
    # rejected, naming the field and the claim for the retry.
    from culinary_copilot.agent.validate import plan_fidelity_errors

    plan = _adaptation_plan()
    errors = plan_fidelity_errors(
        plan, "This follows the original recipe exactly.", "model_adaptation"
    )
    assert errors and "note" in errors[0]
    assert "follows the original recipe" in errors[0]
    assert "remove the claim" in errors[0]


def test_plan_fidelity_negated_claim_passes() -> None:
    # H3 part 2: "does not follow the source exactly" is an honest
    # exclusion, not a fidelity claim (negation handling reuses the
    # loop.py::_negated_mention approach).
    from culinary_copilot.agent.validate import plan_fidelity_errors

    plan = _adaptation_plan()
    assert (
        plan_fidelity_errors(
            plan,
            "This does not follow the source exactly; it is an adaptation.",
            "model_adaptation",
        )
        == []
    )
    assert plan_fidelity_errors(plan, "Doesn't match the source.", "model_adaptation") == []


def test_plan_fidelity_source_plan_unaffected() -> None:
    # H3 part 2: a source-labelled plan is not affected, even with the
    # same fidelity wording.
    from culinary_copilot.agent.validate import plan_fidelity_errors

    plan = _adaptation_plan()
    assert plan_fidelity_errors(plan, "This follows the source.", "source") == []
    assert (
        plan_fidelity_errors(
            {"mise_en_place": [], "steps": ["Follows the source."], "plating": "Hot."},
            "",
            "source",
        )
        == []
    )


def test_plan_fidelity_adaptation_and_step_text_rejected() -> None:
    # H3 part 2: adaptations text and plan text are checked as well as
    # the note.
    from culinary_copilot.agent.validate import plan_fidelity_errors

    plan = _adaptation_plan(
        adaptations=[{"label": "adaptation", "description": "Matches the source."}]
    )
    errors = plan_fidelity_errors(plan, "An adaptation.", "model_adaptation")
    assert errors and "adaptation 0" in errors[0]
    step_plan = _adaptation_plan(steps=["Serve, identical to the source."])
    step_errors = plan_fidelity_errors(step_plan, "An adaptation.", "model_adaptation")
    assert step_errors and "steps 0" in step_errors[0]
    mise_errors = plan_fidelity_errors(
        _adaptation_plan(mise_en_place=["Verbatim from the source."]),
        "An adaptation.",
        "model_adaptation",
    )
    assert mise_errors and "mise_en_place 0" in mise_errors[0]


def test_plan_fidelity_honest_notes_pass() -> None:
    # H3 part 2: adaptation language without a fidelity verb never
    # matches, and the app-written attribution note (which contains a
    # negated "not from the source verbatim") is not flagged.
    from culinary_copilot.agent.validate import fidelity_claim, plan_fidelity_errors

    honest = [
        "An adaptation with extra garlic.",
        "Adapted from the source with added herbs.",
        "Based on the source.",
        "plan from selected source",
        "Both demo options use pantry staples.",
        "Labelled by the app: these steps are not from the source verbatim "
        "(plan steps [0] are not all grounded).",
    ]
    for text in honest:
        assert fidelity_claim(text) is None, text
    plan = _adaptation_plan()
    for text in honest:
        assert plan_fidelity_errors(plan, text, "model_adaptation") == [], text
    # Every documented verb family is caught.
    for text in (
        "matches the source",
        "reproduces the source recipe",
        "same as the original",
        "word for word from the recipe",
        "faithful to the original",
        "true to the source",
        "exact copy of the source",
        "exactly as in the original recipe",
        "no changes to the source",
    ):
        assert fidelity_claim(text) is not None, text


def test_plan_fidelity_negation_must_govern_the_claim() -> None:
    # 2026-10-08 review: an eight-word negation window let 5 of 8 claims
    # through ("Without changing anything, this follows ..."); a claim
    # scoped to numbered steps is honest; common paraphrases are claims.
    from culinary_copilot.agent.validate import fidelity_claim

    claims = [
        "Without changing anything, this follows the original recipe exactly.",
        "No substitutions needed, so the steps follow the source exactly.",
        "Nothing is skipped and every step matches the source.",
        "I didn't change a thing, it reproduces the original recipe.",
        "Avoids extra steps and follows the source recipe faithfully.",
        "The plan sticks to the original recipe exactly.",
        "Steps are taken directly from the source with no modifications.",
        "The method is the source recipe without any changes.",
        "Copied verbatim from the source.",
    ]
    honest = [
        "It doesn't exactly follow the original recipe.",
        "Steps 1-3 follow the source; step 4 is my addition.",
        "Step 2 matches the source; the rest is adapted.",
        "Not a verbatim copy of the source.",
        "Based on the source, with a shorter simmer.",
    ]
    assert [text for text in claims if fidelity_claim(text) is None] == []
    assert [text for text in honest if fidelity_claim(text) is not None] == []


def _list_doc() -> dict[str, Any]:
    # Synthetic baking source: a unit-less count (eggs) makes the
    # amounts and ingredient mentions unequal, so order pairing is off.
    def entry(canonical: str, amount: str, amount_text: str, unit: str | None, original: str):
        return {
            "canonical": canonical,
            "name": canonical,
            "amount": amount,
            "amount_text": amount_text,
            "unit": unit,
            "original": original,
        }

    return {
        "title": "Synthetic Cocoa Muffins",
        "ingredients": [
            entry("all-purpose flour", "2", "2", "cup", "2 cups all-purpose flour"),
            entry("white sugar", "3/2", "1 1/2", "cup", "1 1/2 cups white sugar"),
            entry("butter, softened", "3", "3", "tbsp", "3 tablespoons butter, softened"),
            entry("large eggs", "2", "2", None, "2 large eggs"),
            entry("milk", "1", "1", "cup", "1 cup milk"),
        ],
        "instructions": ["Mix.", "Bake."],
    }


def test_h8_amount_leads_its_ingredient_past_a_prep_word() -> None:
    # H8 attempt 3 (2026-10-08): "..., 1 1/2 cup white sugar, 3 tbsp
    # softened butter, ..." attached "3 tbsp" to the sugar before it and
    # rejected a correct plan. Wrong amounts and swaps still fail.
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = _list_doc()
    line = (
        "Measure out 2 cup all-purpose flour, 1 1/2 cup white sugar, "
        "3 tbsp softened butter, 2 large eggs, and 1 cup milk."
    )

    def check(text: str) -> list[str]:
        return plan_prose_quantity_errors(
            {"mise_en_place": [text], "steps": ["S."], "plating": "S."}, doc
        )

    assert check(line) == []
    wrong = check(line.replace("3 tbsp softened butter", "1 cup softened butter"))
    assert len(wrong) == 1 and "'butter softened' as 3 tbsp" in wrong[0]
    swapped = check(
        line.replace(
            "1 1/2 cup white sugar, 3 tbsp softened butter",
            "3 tbsp white sugar, 1 1/2 cup softened butter",
        )
    )
    assert len(swapped) == 2


def test_h8_equipment_size_is_not_an_amount() -> None:
    # H8 attempt 4 (2026-10-08): "Grease a 12-cup muffin tin" was read as
    # 12 cups of an ingredient and a sound plan was rejected. A hyphenated
    # size before an equipment noun is skipped; ingredient amounts are not.
    from culinary_copilot.agent.validate import plan_prose_quantity_errors

    doc = _list_doc()

    def check(text: str) -> list[str]:
        return plan_prose_quantity_errors(
            {"mise_en_place": [text], "steps": ["S."], "plating": "S."}, doc
        )

    assert check("Grease a 12-cup muffin tin or line it with paper liners.") == []
    assert check("Heat the milk in a 2-quart saucepan.") == []
    assert len(check("Measure out 12-cup all-purpose flour.")) == 1
    assert len(check("Measure 12 cup all-purpose flour into a large bowl.")) == 1
