"""Disposable-Postgres checks for dataset-aware search/lookup.

Creates/drops only ``culinary_test_discovery`` with synthetic fixtures.
Never touches the application database. Skipped when PostgreSQL is
unreachable so offline runs stay credential-free.
"""

import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.config import Settings
from culinary_copilot.recipes import import_data
from culinary_copilot.recipes.repository import get_recipe, search_all, search_recipes

TEST_DB = "culinary_test_discovery"
FOODCOM = "AkashPS11/recipes_data_food.com"
FOODIE = "odunola/foodie"


def _urls() -> tuple[str, str]:
    settings = Settings()
    base = settings.database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/{TEST_DB}"


def _import_row(dataset: str) -> dict:
    return {
        "id": f"test-discovery-{dataset.replace('/', '-')}",
        "dataset_id": dataset,
        "revision": "test-rev",
        "checksum": "test-checksum",
        "normalizer_version": "3",
        "vocabulary_checksum": "test-vocab",
        "dataset_url": "https://example.invalid/test",
        "report": json.dumps({"counts": {}}),
    }


def _recipe(
    dataset: str,
    source_id: str,
    title: str,
    minutes: float | None,
    names: list[str],
    text_body: str,
) -> dict:
    return {
        "dataset_id": dataset,
        "source_id": source_id,
        "import_id": f"test-discovery-{dataset.replace('/', '-')}",
        "title": title,
        "minutes": minutes,
        "servings": 2,
        "names": names,
        "document": json.dumps(
            {
                "title": title,
                "source_id": source_id,
                "provenance": {"dataset_id": dataset, "source_id": source_id},
                "flags": [],
            }
        ),
        "search_text": text_body,
    }


@pytest.fixture(scope="module")
def engine():
    maint_url, test_url = _urls()
    try:
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
        maint.dispose()
        eng = create_engine(test_url)
        with eng.begin() as conn:
            for migration in sorted(import_data.MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name):
                for statement in import_data.split_sql_statements(migration.read_text()):
                    conn.execute(text(statement))
            for dataset in (FOODCOM, FOODIE):
                conn.execute(
                    text("""
                    INSERT INTO recipe_imports
                        (id, dataset_id, revision, checksum, normalizer_version,
                         vocabulary_checksum, dataset_url, report)
                    VALUES (:id, :dataset_id, :revision, :checksum, :normalizer_version,
                            :vocabulary_checksum, :dataset_url, CAST(:report AS jsonb))
                    """),
                    _import_row(dataset),
                )
            rows = [
                _recipe(
                    FOODCOM,
                    "000038",
                    "Legacy Garlic Chicken",
                    30,
                    ["garlic", "chicken"],
                    "garlic chicken legacy roasted dinner",
                ),
                _recipe(
                    FOODIE,
                    "foodie-000001",
                    "Foodie Garlic Stew",
                    20,
                    ["garlic"],
                    "garlic stew foodie simmered pot",
                ),
                _recipe(
                    FOODCOM,
                    "shared-001",
                    "Foodcom Shared Pie",
                    40,
                    ["apple"],
                    "shared apple pie baked crust",
                ),
                _recipe(
                    FOODIE,
                    "shared-001",
                    "Foodie Shared Pie",
                    35,
                    ["apple"],
                    "shared apple pie baked crust",
                ),
                _recipe(
                    FOODCOM,
                    "nodur-001",
                    "Mystery Garlic Dish",
                    None,
                    ["garlic"],
                    "garlic mystery dish unknown time",
                ),
            ]
            for row in rows:
                conn.execute(
                    text("""
                    INSERT INTO recipes (dataset_id, source_id, import_id, title,
                        total_minutes, servings, ingredient_names, document, search_text)
                    VALUES (:dataset_id, :source_id, :import_id, :title, :minutes,
                        :servings, :names, CAST(:document AS jsonb), :search_text)
                    """),
                    row,
                )
        yield eng
        eng.dispose()
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
        maint.dispose()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL unavailable for discovery tests: {exc!r}")


def test_default_search_returns_both_datasets(engine) -> None:
    hits = search_all(engine, "garlic")
    datasets = {h["dataset_id"] for h in hits}
    assert {FOODCOM, FOODIE} <= datasets
    for hit in hits:
        assert hit["dataset_id"] and hit["source_id"]


def test_dataset_filter_restricts_results(engine) -> None:
    hits = search_recipes(engine, "garlic", dataset_id=FOODIE)
    assert hits
    assert all(h["dataset_id"] == FOODIE for h in hits)
    foodcom_hits = search_recipes(engine, "garlic", dataset_id=FOODCOM)
    assert foodcom_hits
    assert all(h["dataset_id"] == FOODCOM for h in foodcom_hits)


def test_single_and_combined_share_shape_and_filters(engine) -> None:
    combined = search_all(engine, "garlic", ingredients=["garlic"], max_minutes=30, limit=10)
    single = search_recipes(
        engine,
        "garlic",
        ingredients=["garlic"],
        max_minutes=30,
        limit=10,
        dataset_id=FOODIE,
    )
    assert combined and single
    assert set(combined[0]) == set(single[0])
    assert {
        "dataset_id",
        "source_id",
        "title",
        "total_minutes",
        "servings",
        "flags",
        "score",
    } <= set(combined[0])
    for hit in (*combined, *single):
        assert hit["total_minutes"] is not None and hit["total_minutes"] <= 30


def test_unknown_durations_excluded_by_time_ceiling(engine) -> None:
    hits = search_all(engine, "garlic", max_minutes=25)
    ids = {(h["dataset_id"], h["source_id"]) for h in hits}
    assert (FOODCOM, "nodur-001") not in ids
    assert (FOODCOM, "000038") not in ids  # 30 minutes > 25 ceiling
    assert (FOODIE, "foodie-000001") in ids
    assert all(h["total_minutes"] is not None for h in hits)


def test_stable_ordering_for_score_ties(engine) -> None:
    hits = search_all(engine, "shared apple pie baked crust")
    pair = [(h["dataset_id"], h["source_id"]) for h in hits if h["source_id"] == "shared-001"]
    assert pair == [(FOODCOM, "shared-001"), (FOODIE, "shared-001")]
    assert search_all(engine, "shared apple pie baked crust") == hits  # repeatable


def test_exact_lookup_with_shared_source_id(engine) -> None:
    foodcom = get_recipe(engine, "shared-001", dataset_id=FOODCOM)
    foodie = get_recipe(engine, "shared-001", dataset_id=FOODIE)
    assert foodcom is not None and foodie is not None
    assert foodcom["title"] == "Foodcom Shared Pie"
    assert foodie["title"] == "Foodie Shared Pie"
    assert foodcom["provenance"]["dataset_id"] == FOODCOM
    assert foodie["provenance"]["dataset_id"] == FOODIE


def test_explicit_dataset_miss_does_not_fall_back(engine) -> None:
    assert get_recipe(engine, "foodie-000001", dataset_id=FOODCOM) is None
    assert get_recipe(engine, "000038", dataset_id=FOODIE) is None


def test_legacy_lookup_still_finds_foodcom(engine) -> None:
    doc = get_recipe(engine, "000038")
    assert doc is not None
    assert doc["title"] == "Legacy Garlic Chicken"
    assert doc["provenance"]["dataset_id"] == FOODCOM


def test_no_match_and_missing_recipe(engine) -> None:
    assert search_all(engine, "zzzznomatch") == []
    assert search_recipes(engine, "zzzznomatch", dataset_id=FOODIE) == []
    assert get_recipe(engine, "does-not-exist") is None
    assert get_recipe(engine, "does-not-exist", dataset_id=FOODIE) is None


def test_ids_keep_zero_padding_and_prefixes(engine) -> None:
    assert get_recipe(engine, "38") is None  # no integer coercion
    assert get_recipe(engine, "38", dataset_id=FOODCOM) is None
    assert get_recipe(engine, "000001") is None  # no prefix stripping onto foodie-000001
