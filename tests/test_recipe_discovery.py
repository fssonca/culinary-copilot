"""Dataset-aware recipe search/lookup: offline API routing + repository validation.

No database, network, model, or ingestion use. Repository validation tests use
a dummy engine; validation raises before any connection is opened.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from culinary_copilot.api.app import create_app
from culinary_copilot.config import Settings
from culinary_copilot.recipes.repository import (
    SUPPORTED_DATASETS,
    get_recipe,
    search_all,
    search_recipes,
)

FOODCOM = "AkashPS11/recipes_data_food.com"
FOODIE = "odunola/foodie"


def settings() -> Settings:
    return Settings(_env_file=None)


def client() -> TestClient:
    return TestClient(create_app(settings()))


def test_supported_datasets_match_imported_sources() -> None:
    assert SUPPORTED_DATASETS == frozenset({FOODCOM, FOODIE})


def _dummy_engine() -> Any:
    engine = MagicMock()
    engine.connect.side_effect = AssertionError("validation must raise before connecting")
    return engine


@pytest.mark.parametrize("fn", [search_recipes, search_all])
@pytest.mark.parametrize("query", ["", "   "])
def test_blank_query_rejected(fn: Any, query: str) -> None:
    with pytest.raises(ValueError):
        fn(_dummy_engine(), query)


@pytest.mark.parametrize("fn", [search_recipes, search_all])
@pytest.mark.parametrize("limit", [0, -1, 51, 100])
def test_invalid_limit_rejected(fn: Any, limit: int) -> None:
    with pytest.raises(ValueError):
        fn(_dummy_engine(), "garlic", limit=limit)


@pytest.mark.parametrize("fn", [search_recipes, search_all])
@pytest.mark.parametrize("minutes", [0, -5, float("inf"), float("-inf"), float("nan")])
def test_nonpositive_nonfinite_time_rejected(fn: Any, minutes: float) -> None:
    with pytest.raises(ValueError):
        fn(_dummy_engine(), "garlic", max_minutes=minutes)


@pytest.mark.parametrize("bad", ["unknown/dataset", "", "   ", "FOODIE", "38"])
def test_search_recipes_rejects_unsupported_dataset(bad: str) -> None:
    with pytest.raises(ValueError):
        search_recipes(_dummy_engine(), "garlic", dataset_id=bad)


def test_get_recipe_rejects_unsupported_dataset() -> None:
    with pytest.raises(ValueError):
        get_recipe(_dummy_engine(), "000038", dataset_id="unknown/dataset")
    with pytest.raises(ValueError):
        get_recipe(_dummy_engine(), "000038", dataset_id="")


def test_default_search_routes_to_combined_and_returns_identity() -> None:
    payload = [
        {"dataset_id": FOODCOM, "source_id": "000038", "title": "A", "score": 1.0},
        {"dataset_id": FOODIE, "source_id": "foodie-000001", "title": "B", "score": 0.9},
    ]
    with client() as test_client:
        with (
            patch("culinary_copilot.api.app.search_all", return_value=payload) as combined,
            patch("culinary_copilot.api.app.search_recipes") as single,
        ):
            response = test_client.get("/api/v1/recipes?q=garlic")
            assert response.status_code == 200
            assert response.json() == payload
            combined.assert_called_once()
            single.assert_not_called()
            for row in response.json():
                assert row["dataset_id"] and row["source_id"]
            datasets = {row["dataset_id"] for row in response.json()}
            assert datasets == {FOODCOM, FOODIE}


def test_dataset_filter_routes_to_single_dataset_search() -> None:
    payload = [{"dataset_id": FOODIE, "source_id": "foodie-000001", "title": "B"}]
    with client() as test_client:
        with (
            patch("culinary_copilot.api.app.search_recipes", return_value=payload) as single,
            patch("culinary_copilot.api.app.search_all") as combined,
        ):
            response = test_client.get("/api/v1/recipes?q=garlic&dataset_id=odunola%2Ffoodie")
            assert response.status_code == 200
            assert response.json() == payload
            combined.assert_not_called()
            _, kwargs = single.call_args
            assert kwargs["dataset_id"] == FOODIE
            assert all(row["dataset_id"] == FOODIE for row in response.json())


def test_search_filters_are_forwarded() -> None:
    with client() as test_client:
        with patch("culinary_copilot.api.app.search_all", return_value=[]) as combined:
            response = test_client.get(
                "/api/v1/recipes?q=garlic&ingredient=garlic&max_minutes=25&limit=7"
            )
            assert response.status_code == 200
            assert response.json() == []
            _, kwargs = combined.call_args
            assert kwargs["ingredients"] == ["garlic"]
            assert kwargs["max_minutes"] == 25
            assert kwargs["limit"] == 7


@pytest.mark.parametrize("url", ["foodie", "unknown%2Fdataset", ""])
def test_search_rejects_bad_dataset_id(url: str) -> None:
    with client() as test_client:
        with (
            patch("culinary_copilot.api.app.search_all") as combined,
            patch("culinary_copilot.api.app.search_recipes") as single,
        ):
            response = test_client.get(f"/api/v1/recipes?q=garlic&dataset_id={url}")
            assert response.status_code == 422
            combined.assert_not_called()
            single.assert_not_called()


def test_search_blank_query_maps_to_422() -> None:
    with client() as test_client:
        with patch("culinary_copilot.api.app.search_all", side_effect=ValueError("bad query")):
            assert test_client.get("/api/v1/recipes?q=%20%20%20").status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/recipes?q=garlic&limit=0",
        "/api/v1/recipes?q=garlic&limit=51",
        "/api/v1/recipes?q=garlic&max_minutes=0",
    ],
)
def test_search_invalid_filters_are_422(url: str) -> None:
    with client() as test_client:
        assert test_client.get(url).status_code == 422


def test_search_nonfinite_time_maps_to_422() -> None:
    with client() as test_client:
        with patch("culinary_copilot.api.app.search_all", side_effect=ValueError("bad minutes")):
            assert test_client.get("/api/v1/recipes?q=garlic&max_minutes=inf").status_code == 422


def test_search_database_failure_is_503_without_leak() -> None:
    with client() as test_client:
        with patch(
            "culinary_copilot.api.app.search_all",
            side_effect=OperationalError("secret", {}, Exception()),
        ):
            response = test_client.get("/api/v1/recipes?q=garlic")
            assert response.status_code == 503
            assert "secret" not in response.text
        with patch(
            "culinary_copilot.api.app.search_recipes",
            side_effect=OperationalError("secret", {}, Exception()),
        ):
            response = test_client.get(f"/api/v1/recipes?q=garlic&dataset_id={FOODCOM}")
            assert response.status_code == 503
            assert "secret" not in response.text


def test_lookup_passes_exact_pair_and_miss_is_404() -> None:
    doc = {"title": "Foodie Shared", "provenance": {"dataset_id": FOODIE}}
    with client() as test_client:
        with patch("culinary_copilot.api.app.get_recipe", return_value=doc) as lookup:
            response = test_client.get(f"/api/v1/recipes/shared-001?dataset_id={FOODIE}")
            assert response.status_code == 200
            assert response.json() == doc
            lookup.assert_called_once_with(lookup.call_args[0][0], "shared-001", dataset_id=FOODIE)
        with patch("culinary_copilot.api.app.get_recipe", return_value=None) as lookup:
            response = test_client.get(f"/api/v1/recipes/shared-001?dataset_id={FOODCOM}")
            assert response.status_code == 404
            lookup.assert_called_once_with(lookup.call_args[0][0], "shared-001", dataset_id=FOODCOM)


def test_lookup_rejects_bad_dataset_id() -> None:
    with client() as test_client:
        with patch("culinary_copilot.api.app.get_recipe") as lookup:
            assert test_client.get("/api/v1/recipes/000038?dataset_id=nope").status_code == 422
            assert test_client.get("/api/v1/recipes/000038?dataset_id=").status_code == 422
            lookup.assert_not_called()


def test_legacy_lookup_without_dataset_preserved() -> None:
    doc = {"title": "Legacy", "source_id": "000038"}
    with client() as test_client:
        with patch("culinary_copilot.api.app.get_recipe", return_value=doc) as lookup:
            response = test_client.get("/api/v1/recipes/000038")
            assert response.status_code == 200
            assert response.json() == doc
            lookup.assert_called_once_with(lookup.call_args[0][0], "000038", dataset_id=None)


def test_lookup_missing_and_db_failure() -> None:
    with client() as test_client:
        with patch("culinary_copilot.api.app.get_recipe", return_value=None):
            assert test_client.get("/api/v1/recipes/does-not-exist").status_code == 404
        with patch(
            "culinary_copilot.api.app.get_recipe",
            side_effect=OperationalError("secret", {}, Exception()),
        ):
            response = test_client.get("/api/v1/recipes/000038")
            assert response.status_code == 503
            assert "secret" not in response.text
