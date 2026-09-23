"""Workstreams 1–2 offline tests: no DB, no downloads, synthetic fixtures only."""

from culinary_copilot.recipes.adapters.base import fingerprint, foodie_source_id
from culinary_copilot.recipes.adapters.foodie import (
    normalize_foodie_text,
    parse_ingredient_line,
    split_sections,
)
from culinary_copilot.recipes.dataset_utils import systematic_sample
from culinary_copilot.recipes.normalize import normalize
from culinary_copilot.recipes.nutrition import observe_foodcom_nutrition
from culinary_copilot.recipes.quality import capabilities_for, is_defective
from culinary_copilot.recipes.search import SEARCH_DOCUMENT_VERSION, render_from_recipe


def foodcom_row(**changes):
    return {
        "RecipeId": "000038",
        "Name": "Low-Fat Berry Blue Frozen Dessert",
        "RecipeIngredientParts": 'c("blueberries", "sugar")',
        "RecipeIngredientQuantities": 'c("4", "1/4")',
        "RecipeInstructions": 'c("Toss berries.", "Freeze.")',
        "TotalTime": "PT24H45M",
        "RecipeServings": "4",
        "Description": "Make and share this dessert.",
        "RecipeCategory": "Frozen Desserts",
        "Keywords": 'c("Dessert", "Low Cholesterol", "Healthy")',
        "Calories": "170.9",
        "FatContent": "2.5",
        "AggregatedRating": "4.5",
        "ReviewCount": "4",
        **changes,
    }


def test_keyword_phrases_preserved_not_exploded():
    recipe = normalize(foodcom_row(), set())
    doc = render_from_recipe(recipe)
    assert "Low Cholesterol" in doc
    # The old bug expanded "a b" into "a", " ", "b" chars; renderer is one string.
    assert isinstance(doc, str)
    assert recipe["search_document_version"] == SEARCH_DOCUMENT_VERSION
    # Ingredients + instructions + category intentionally included once.
    assert "blueberries" in doc and "Freeze." in doc and "Frozen Desserts" in doc
    # Missing optionals contribute no "None" tokens.
    bare = normalize(foodcom_row(Description="NA", RecipeCategory="NA", Keywords="NA"), set())
    assert "None" not in render_from_recipe(bare)


def test_meaningful_keyword_retrieval_phrase_vs_chars():
    recipe = normalize(foodcom_row(), set())
    doc = render_from_recipe(recipe).casefold()
    assert "low cholesterol" in doc  # whole phrase discoverable
    # Character-expansion regression: joining then starring would put every
    # keyword char separated; instead phrases stay contiguous.
    assert "low cholesterol healthy" in doc.replace("  ", " ") or "healthy" in doc


def test_nutrition_unknown_basis_and_compat():
    recipe = normalize(foodcom_row(), set())
    assert recipe["nutrition"]["fat"] == 2.5  # legacy float map preserved
    obs = {o["nutrient"]: o for o in recipe["nutrition_observations"]}
    assert obs["fat"]["unit"] == "unknown" and obs["fat"]["basis"] == "unknown"
    assert obs["fat"]["source_kind"] == "reported"
    assert obs["fat"]["verification"] == "unverified"
    assert obs["fat"]["source_field"] == "FatContent"


def test_nutrition_zero_preserved_malformed_flagged():
    legacy, observations, issues = observe_foodcom_nutrition(
        {"Calories": "0", "FatContent": "nan", "SodiumContent": "-1", "SugarContent": "abc"}
    )
    assert legacy["calories"] == 0  # valid zero preserved
    codes = {i["code"] for i in issues}
    assert {"nonfinite_nutrition", "negative_nutrition", "malformed_nutrition"} <= codes
    # Malformed values become flagged invalid observations, never silent gaps.
    by_nutrient = {o["nutrient"]: o for o in observations}
    assert by_nutrient["fat"]["verification"] == "invalid"
    assert legacy["fat"] is None and legacy["sodium"] is None


def test_quality_presence_not_defective():
    recipe = normalize(foodcom_row(), set())
    assert "nutrition_present" in recipe["flags"]  # legacy compat retained
    assert "images_unknown" in recipe["flags"]
    # Defective counts only warning/error issues, never presence metadata.
    assert not is_defective([i for i in recipe["quality_issues"] if i["severity"] == "info"])
    assert is_defective([{"code": "quantity_unknown", "severity": "warning", "field": "x"}])
    assert recipe["available_fields"]["nutrition"] is True
    caps = recipe["capabilities"]
    assert caps["searchable"] and caps["evidence_usable"]
    assert caps["quantities_validated"] is False  # units never known here
    assert caps["complete_eligible"] is False and caps["scalable"] is False


def test_capabilities_distinct_and_conservative():
    caps = capabilities_for(
        has_identity=True,
        has_ingredients=True,
        has_instructions=True,
        quantities_validated=True,
        servings_known=True,
        durations_known=True,
        structural_issue=False,
    )
    assert caps == {
        "searchable": True,
        "evidence_usable": True,
        "quantities_validated": True,
        "complete_eligible": True,
        "scalable": True,
    }
    # Parsed quantities alone never imply completeness.
    partial = capabilities_for(
        has_identity=True,
        has_ingredients=True,
        has_instructions=True,
        quantities_validated=True,
        servings_known=False,
        durations_known=False,
        structural_issue=False,
    )
    assert partial["complete_eligible"] is False and partial["scalable"] is False


def test_foodie_amount_edge_cases():
    assert parse_ingredient_line("1 1/2 cups flour")["amount"] == "3/2"
    assert parse_ingredient_line("0.5 tsp salt")["amount"] == "1/2"
    assert parse_ingredient_line("½ cup sugar")["amount"] == "1/2"
    assert parse_ingredient_line("1⁄4 kg appraisal")["amount"] == "1/4"
    assert parse_ingredient_line("170gplain flour")["unit"] == "g"
    assert parse_ingredient_line("1Eggs")["amount"] == "1"
    assert parse_ingredient_line("2cloves garliccrushed")["canonical"] == "garlic"
    assert parse_ingredient_line("cinnamon(to taste)")["qualitative"] is True
    compound = parse_ingredient_line("1/3 cup plus 2 tablespoons sugar")
    assert compound["compound"] is True and compound["amount"] is None
    assert compound["canonical"] == "sugar"  # never summed, name retained
    equiv = parse_ingredient_line("6g (1 tsp)Salt")
    assert equiv["equivalent"] is True and equiv["amount"] == "6"
    assert equiv["unit"] == "g"  # outer kept, inner in notes, not summed
    assert parse_ingredient_line("40–45 minutes")["is_range"] is True
    assert parse_ingredient_line("40–45 minutes")["amount"] is None
    assert parse_ingredient_line("1 cup sugar (14 oz package)")["notes"] is not None
    assert parse_ingredient_line("2 large eggs")["unit"] == "count"


def test_foodie_sections_and_markers():
    texts = (
        "Chimodho\nIngredients\n250ml buttermilk\n1Eggs\n"
        "4g (1 tsp)Baking PowderIntroduction\n\nDirections\nDo this.\nDo that.\n"
    )
    sections = split_sections(texts)
    assert sections["title"] == "Chimodho"
    assert len(sections["ingredient_lines"]) == 3
    assert sections["instruction_lines"] == ["Do this.", "Do that."]
    damaged = "T\nIngredients\n1 cup flour\n</adirections\nBake.\n"
    assert split_sections(damaged)["instruction_lines"] == ["Bake."]


def test_foodie_headings_repeats_and_no_comma_split():
    texts = (
        "Cake\nIngredients\nFor the sauce:\n1 cup sugar, divided\n"
        "1 cup sugar\nIntroduction\nNote.\nDirections\nMix.\nBake.\n"
    )
    recipe = normalize_foodie_text(texts, 7)
    assert recipe["ingredient_groups"]  # heading detected
    assert len(recipe["ingredients"]) == 2  # comma kept inside one line
    assert recipe["ingredients"][0]["canonical"] == "sugar, divided"


def test_foodie_identity_and_sampling_deterministic():
    assert foodie_source_id(1) == "foodie-000001"
    first = systematic_sample(19566, 150, 42)
    assert len(first) == 150 and first == sorted(first)
    assert first == systematic_sample(19566, 150, 42)
    assert first[0] > 1 and first[-1] < 19566  # covers beyond the file head


def test_foodie_fingerprint_stable():
    a = fingerprint("T", ["sugar"], ["Mix."])
    assert a == fingerprint("T", ["sugar"], ["Mix."])
    assert a != fingerprint("T", ["salt"], ["Mix."])


def test_api_compat_fields_present():
    recipe = normalize(foodcom_row(), set())
    assert isinstance(recipe["nutrition"], dict)  # legacy type unchanged
    assert isinstance(recipe["flags"], list)  # legacy list retained
    assert isinstance(recipe["nutrition_observations"], list)
    assert isinstance(recipe["quality_issues"], list)
    assert isinstance(recipe["available_fields"], dict)
    assert isinstance(recipe["capabilities"], dict)
