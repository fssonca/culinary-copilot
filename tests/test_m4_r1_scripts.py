"""Milestone 4 R1 scripts: classification rules (pure, no database)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "m4"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _load("direction_audit")
sweep = _load("faithful_copy_sweep")
profile = _load("data_profile")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Stir well.", "step"),
        ("Serve cold.", "step"),
        ("Gather all ingredients.", "step"),
        ("To use:", "heading"),
        ("Cook's note", "heading"),
        ("Nutrition:", "heading"),
        ("Enjoy!", "sign_off"),
        ("Unknown", "placeholder"),
        ("Allrecipes/Jane Doe", "site_name"),
        ("Photo by Jane Doe.", "credit_phrase"),
        ("Makes about 24 cookies.", "yield_note"),
        ("Chill before serving.", "step"),
        ("Jane", "name_like"),
        ("cook4ever12", "name_like"),
    ],
)
def test_direction_classes(text: str, expected: str) -> None:
    assert audit.classify(text)[0] == expected


def test_direction_candidates_include_long_credits() -> None:
    assert audit.is_candidate("Stir.", 3)
    assert not audit.is_candidate("Stir the batter until smooth and pour it in.", 3)
    assert audit.is_candidate("Recipe by a local chef, published with permission today.", 3)


@pytest.mark.parametrize(
    ("error", "bucket"),
    [
        ("plan quantity {'ingredient': 'x'} not in source (invented)", "structured_quantity"),
        (
            "plan mise_en_place says 'a' ('1 cup'), but the source states 'b' as 2 cup",
            "prose_wrong_ingredient",
        ),
        (
            "plan steps says 'a' ('1 cup'), but the source states no amount for 'b'; omit",
            "prose_no_amount",
        ),
        (
            "plan mise_en_place says 'a' ('1 cup'), but the amount is ambiguous between",
            "prose_ambiguous",
        ),
        (
            "plan mise_en_place says 'a' ('1 cup'), but no source ingredient matches that amount",
            "prose_unattributed",
        ),
        ("raw chicken needs a food-safety chunk in technique_refs", "food_safety"),
        ("something new", "other"),
    ],
)
def test_sweep_buckets(error: str, bucket: str) -> None:
    assert sweep.bucket_for(error) == bucket


def test_only_food_safety_is_eligibility() -> None:
    assert sweep.ELIGIBILITY_BUCKETS == frozenset({"food_safety"})


def test_sweep_builds_three_faithful_variants() -> None:
    doc = {
        "title": "T",
        "ingredients": [
            {
                "canonical": "flour",
                "amount": "2",
                "amount_text": "2",
                "unit": "cup",
                "original": "2 cups flour",
            },
            {
                "canonical": "sugar",
                "amount": "1",
                "amount_text": "1",
                "unit": None,
                "original": "1 sugar cube",
            },
        ],
        "instructions": ["Mix.", "Bake."],
    }
    pair = ("odunola/foodie", "x")
    original = sweep.build_plan(doc, pair, "original")
    lines = sweep.build_plan(doc, pair, "lines")
    listed = sweep.build_plan(doc, pair, "list")
    assert original["mise_en_place"] == ["2 cups flour", "1 sugar cube"]
    assert lines["mise_en_place"] == ["2 cup flour", "1 sugar"]
    assert listed["mise_en_place"] == ["Gather 2 cup flour, 1 sugar."]
    assert listed["steps"] == ["Mix.", "Bake."] and listed["step_sources"] == [0, 1]
    assert sweep.check_plan(doc, pair, lines) == []


@pytest.mark.parametrize(
    ("title", "ingredients", "category", "subtype"),
    [
        ("Classic Cheesecake", ["cream cheese"], "cheesecake", ""),
        ("Chocolate Cake", ["flour", "butter"], "cake", "butter"),
        ("Carrot Cake", ["flour", "vegetable oil"], "cake", "oil"),
        ("Angel Food Cake", ["egg white"], "cake", "foam"),
        ("Lemon Bars", ["lemon"], "bar_cookie", ""),
        ("Dinner Rolls", ["flour", "active dry yeast"], "bread", "yeast"),
        ("Banana Bread", ["flour", "banana"], "bread", "quick"),
        ("Grandma's Special", ["flour"], "unknown", ""),
    ],
)
def test_categories(title: str, ingredients: list[str], category: str, subtype: str) -> None:
    doc = {"title": title, "ingredients": [{"canonical": i} for i in ingredients]}
    got = profile.categorize(doc)
    assert got[0] == category and got[1] == subtype


def test_line_profile_classes() -> None:
    doc = {
        "ingredients": [
            {"canonical": "flour", "amount": "2", "unit": "cup"},
            {"canonical": "butter", "amount": "100", "unit": "g"},
            {"canonical": "sugar", "amount": "2", "unit": None},
            {"canonical": "salt", "amount": None, "unit": None},
            {"canonical": "garlic", "amount": "2", "unit": "clove"},
        ]
    }
    counts = profile.profile_lines(doc)
    assert counts["amount_volume_unit"] == 1
    assert counts["amount_mass_unit"] == 1
    assert counts["amount_none_unit"] == 1
    assert counts["no_amount"] == 1
    assert counts["amount_other_unit"] == 1
