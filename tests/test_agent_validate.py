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
