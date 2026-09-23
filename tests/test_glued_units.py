"""Glued-unit regressions and final evidence checks, including deterministic fields."""

import pytest

from culinary_copilot.recipes.adapters.foodie import parse_ingredient_line
from culinary_copilot.recipes.llm_batch import _is_loadable
from culinary_copilot.recipes.llm_validate import merged_ingredient_problems


@pytest.mark.parametrize(
    "source,amount,unit,name",
    [
        ("3garlic clove", "3", "count", "garlic clove"),
        ("2ginger roots", "2", "count", "ginger roots"),
        ("3green peppers", "3", "count", "green peppers"),
        ("6leek", "6", "count", "leek"),
        ("415 gcorn bread", "415", "g", "corn bread"),
        ("170 gspinach leaves", "170", "g", "spinach leaves"),
        ("170gplain flour", "170", "g", "plain flour"),
        ("100 g garlic", "100", "g", "garlic"),
        ("1½ cups flour", "3/2", "cup", "flour"),
    ],
)
def test_glued_count_and_grams(source, amount, unit, name):
    item = parse_ingredient_line(source)
    assert (item["amount"], item["unit"], item["canonical"]) == (amount, unit, name)
    assert not merged_ingredient_problems({"ingredients": [item]})


@pytest.mark.parametrize("origin", ["deterministic", "llm"])
def test_final_gate_blocks_unsupported_fields_from_either_origin(origin):
    recipe = {
        "title": "Example",
        "instructions": ["Mix."],
        "ingredients": [
            {
                "original": "3garlic cloves",
                "name": "arlic cloves",
                "amount": "3",
                "unit": "g",
                "origin": origin,
            }
        ],
    }
    assert not _is_loadable(recipe)
    recipe["ingredients"][0].update(unit=None, name="garlic cloves", amount="999")
    assert not _is_loadable(recipe)
    recipe["ingredients"][0]["amount"] = "3"
    assert _is_loadable(recipe)


def test_missing_source_is_not_loadable():
    assert not _is_loadable({"title": "Example", "ingredients": [{"name": "garlic"}]})


@pytest.mark.parametrize(
    "source,amount",
    [
        ("1-3/4 cups flour", "7/4"),
        ("1 1 /2 teaspoons salt", "3/2"),
        ("One pound corn", "1"),
        ("4 and 1/2 cup flour", "9/2"),
    ],
)
def test_source_number_notation_is_preserved(source, amount):
    assert not merged_ingredient_problems(
        {"ingredients": [{"original": source, "amount": amount, "unit": None}]}
    )


@pytest.mark.parametrize(
    "source,amount",
    [
        ("3-4 Large Eggs", "3/4"),
        ("3–4 ice cubes", "3/4"),
        ("1 (3 1/2) pound chicken", "3/2"),
    ],
)
def test_ranges_and_package_sizes_are_not_fabricated_fractions(source, amount):
    assert merged_ingredient_problems(
        {"ingredients": [{"original": source, "amount": amount, "unit": None}]}
    )


@pytest.mark.parametrize(
    "source",
    [
        "2 gluten-free bread slices",
        "2 garlic-stuffed green olives",
        "4 gluten-free wafer cookies",
        "2 ground cloves",
    ],
)
def test_hyphenated_words_are_not_grams(source):
    from culinary_copilot.recipes.llm_validate import gram_source_evidence

    item = parse_ingredient_line(source)
    assert item["unit"] != "g"
    assert not gram_source_evidence(source)
    assert not merged_ingredient_problems({"ingredients": [item]})
    item["unit"] = "g"  # Synthetic invalid old/model interpretation.
    assert merged_ingredient_problems({"ingredients": [item]})


@pytest.mark.parametrize(
    "source",
    [
        "170gplain flour",
        "415 gcorn bread",
        "170 gspinach leaves",
        "100 gSugar",
        "200 grams flour",
        "100g flour",
        "5 gm sugar",
        "20 gsemi-salted butter",
        "20 ggrated cheese",
    ],
)
def test_raw_gram_evidence(source):
    from culinary_copilot.recipes.llm_validate import gram_source_evidence

    assert gram_source_evidence(source)


@pytest.mark.parametrize(
    "source", ["400 grbeef-best cut", "200 ggrained", "500 glow-fat", "125 gground", "100 ggroats"]
)
def test_ambiguous_gram_glue_held(source):
    assert merged_ingredient_problems({"ingredients": [parse_ingredient_line(source)]})


@pytest.mark.parametrize(
    "source", ["100 gramsstrawberriescut", "150 gramsgranulated sugar", "20 gramsItalian Parsley"]
)
def test_full_grams_word_glued_to_ingredient(source):
    from culinary_copilot.recipes.llm_validate import gram_source_evidence

    assert gram_source_evidence(source)
