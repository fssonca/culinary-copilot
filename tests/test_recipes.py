"""Synthetic fixtures only: never invoke CLI, download data or open a database."""

import csv
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from culinary_copilot.api.app import create_app
from culinary_copilot.config import Settings
from culinary_copilot.recipes.import_data import REQUIRED, audit
from culinary_copilot.recipes.normalize import minutes, normalize, parse_list, quantity


def row(**changes):
    return {
        "RecipeId": "000038",
        "Name": "Egg bowl",
        "RecipeIngredientParts": 'c("eggs", "olive oil")',
        "RecipeIngredientQuantities": 'c("2", "1/2")',
        "RecipeInstructions": 'c("Heat oil.", "Cook eggs.")',
        "TotalTime": "PT10M",
        "RecipeServings": "2",
        **changes,
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        ('c("salt, fine", NA, "a \\"quote\\"")', ["salt, fine", None, 'a "quote"']),
        ("character(0)", []),
        ("NA", []),
        ('"eggs"', ["eggs"]),
        ("eggs", ["eggs"]),
    ],
)
def test_lists(value, expected):
    assert parse_list(value) == expected


@pytest.mark.parametrize("value", ['c("egg",)', 'c(system("bad"))', 'c("a" "b")'])
def test_invalid_lists(value):
    with pytest.raises(ValueError):
        parse_list(value)


def test_quantities_and_time():
    assert quantity("1 1/2") == "3/2"
    for value in ["to taste", "1-2", "1/0", "-1", None, "2 cups"]:
        assert quantity(value) is None
    assert minutes("PT1H30M30S") == 90.5
    assert minutes("NA") is None
    with pytest.raises(ValueError):
        minutes("PT")


def test_preserves_unknown_units_mapping_and_raw():
    raw = row()
    recipe = normalize(raw, {"olive_oil"})
    assert recipe["raw"] == raw
    assert recipe["source_id"] == "000038"
    assert recipe["ingredients"][1]["epicure_id"] == "olive_oil"
    assert recipe["ingredients"][0]["epicure_id"] is None
    assert recipe["ingredients"][1]["amount"] == "1/2"
    assert recipe["ingredients"][1]["unit"] is None
    assert not recipe["scalable"] and not recipe["final_recipe_eligible"]


def test_mismatch_never_aligns_partial_quantities():
    recipe = normalize(row(RecipeIngredientQuantities='c("2")', TotalTime="bad"), set())
    assert "ingredient_quantity_mismatch" in recipe["flags"]
    assert all(i["amount"] is None for i in recipe["ingredients"])
    assert recipe["durations_minutes"]["TotalTime"] is None


def test_audit_placeholder_duplicates_quarantine_and_accounting(tmp_path):
    path = tmp_path / "fixture.csv"
    fields = sorted(REQUIRED | {"Barcode"})
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(
            [
                {},
                {"Barcode": "()"},
                row(),
                row(),
                row(RecipeId="000039"),
                row(RecipeId="000040", RecipeInstructions="NA"),
            ]
        )
    recipes, rejected, report = audit(path, set())
    assert len(recipes) == 2 and len(rejected) == 2
    assert report["counts"]["total_rows"] == 6
    assert report["counts"]["duplicate_content_rows"] == 1
    assert {r["reason"] for r in rejected} == {"duplicate_id", "incomplete_ingredients_or_steps"}
    assert report["mapping_coverage"] == 0


def test_recipe_api_missing_database_and_unknown_id():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        with patch("culinary_copilot.api.app.get_recipe", return_value=None):
            assert client.get("/api/v1/recipes/unknown").status_code == 404
        # Default search covers the combined corpus via search_all.
        with patch(
            "culinary_copilot.api.app.search_all",
            side_effect=OperationalError("secret", {}, Exception()),
        ):
            response = client.get("/api/v1/recipes?q=eggs")
            assert response.status_code == 503
            assert "secret" not in response.text
        # Scoped search covers one dataset via search_recipes.
        with patch(
            "culinary_copilot.api.app.search_recipes",
            side_effect=OperationalError("secret", {}, Exception()),
        ):
            response = client.get("/api/v1/recipes?q=eggs&dataset_id=odunola%2Ffoodie")
            assert response.status_code == 503
            assert "secret" not in response.text
