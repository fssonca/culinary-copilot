"""Isolated PostgreSQL integration tests (Workstreams 1A verify + 2E).

Uses a disposable ``culinary_test_isolated`` database on the configured
Postgres server. Never touches the application database records: the test
database is created/dropped by these tests and contains synthetic fixtures
only. Skipped gracefully when no PostgreSQL server is reachable, so default
offline runs never require credentials.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.config import Settings
from culinary_copilot.recipes import import_data
from culinary_copilot.recipes.adapters.foodie import normalize_foodie_text
from culinary_copilot.recipes.normalize import normalize
from culinary_copilot.recipes.rebuild_search import rebuild
from culinary_copilot.recipes.repository import get_recipe, search_all, search_recipes

TEST_DB = "culinary_test_isolated"


def _urls():
    settings = Settings(_env_file=None)
    base = settings.database_url.get_secret_value()
    # postgresql+psycopg://user:pass@host:port/dbname
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/{TEST_DB}"


def _foodcom_recipe():
    raw = {
        "RecipeId": "000038",
        "Name": "Low-Fat Berry Blue Frozen Dessert",
        "RecipeIngredientParts": 'c("blueberries", "sugar")',
        "RecipeIngredientQuantities": 'c("4", "1/4")',
        "RecipeInstructions": 'c("Toss berries.", "Freeze.")',
        "TotalTime": "PT30M",
        "RecipeServings": "4",
        "Description": "Berry dessert.",
        "RecipeCategory": "Frozen Desserts",
        "Keywords": 'c("Dessert", "Low Cholesterol")',
        "Calories": "170.9",
        "FatContent": "2.5",
    }
    recipe = normalize(raw, set())
    recipe["row_number"] = 1
    return recipe


def _foodie_recipe():
    texts = (
        "Test Pilot Stew\nA hearty test stew.\nIngredients\n"
        "500g beef\n1 large onionchopped \n"
        "salt to taste\nIntroduction\nHearty.\nDirections\nBrown beef.\nSimmer.\n"
    )
    return normalize_foodie_text(texts, 43)


def _prov(dataset: str) -> dict:
    return {
        "id": f"test-{dataset.replace('/', '-')}",
        "dataset_id": dataset,
        "revision": "test-rev",
        "checksum": "test-checksum",
        "normalizer_version": "3",
        "vocabulary_checksum": "test-vocab",
        "dataset_url": "https://example.invalid/test",
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
        migrations = sorted(Path(import_data.MIGRATIONS_DIR).glob("*.sql"), key=lambda p: p.name)
        assert [p.name for p in migrations] == [
            "001_recipes.sql",
            "002_search_version.sql",
            "003_quarantine_status.sql",
            "004_recipe_embeddings.sql",
        ]
        with eng.begin() as conn:
            import_data.apply_migrations(conn)
        yield eng
        eng.dispose()
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
        maint.dispose()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL unavailable for isolated tests: {exc!r}")


def test_persist_idempotent_and_isolated(engine):
    foodcom = _foodcom_recipe()
    foodie = _foodie_recipe()
    report = {"counts": {}, "database_written": True}
    import_data.persist(
        engine, [foodcom], [], _prov("AkashPS11/recipes_data_food.com"), report, False
    )
    import_data.persist(engine, [foodie], [], _prov("odunola/foodie"), {**report}, False)
    # Re-persisting the same Food.com snapshot is idempotent.
    import_data.persist(
        engine, [foodcom], [], _prov("AkashPS11/recipes_data_food.com"), report, False
    )
    with engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM recipes")).scalar_one()
        assert n == 2
        # Replacing one dataset snapshot preserves the other dataset.
        assert (
            conn.execute(
                text("SELECT count(*) FROM recipes WHERE dataset_id='odunola/foodie'")
            ).scalar_one()
            == 1
        )


def test_complete_retrieval_preserves_raw_and_provenance(engine):
    doc = get_recipe(engine, "000038")
    assert doc is not None
    assert doc["title"].startswith("Low-Fat Berry")
    assert doc["raw"]["Name"].startswith("Low-Fat Berry")  # raw evidence kept
    assert doc["provenance"]["dataset_id"] == "AkashPS11/recipes_data_food.com"
    # Compatibility: zero-padded Food.com ID still resolves without dataset.
    assert get_recipe(engine, "000038") is not None
    # Dataset-aware lookup isolates identical source_ids across datasets.
    assert (
        get_recipe(engine, "foodie-000043", dataset_id="odunola/foodie")["title"]
        == "Test Pilot Stew"
    )
    assert get_recipe(engine, "foodie-000043", dataset_id="AkashPS11/recipes_data_food.com") is None


def test_whole_keyword_fts_and_rebuild(engine):
    # Whole phrase "Low Cholesterol" is indexed (not char-exploded).
    hits = search_recipes(engine, "Low Cholesterol")
    assert any(h["source_id"] == "000038" for h in hits)
    # Cross-dataset discovery finds the foodie fixture too.
    assert any(h["source_id"] == "foodie-000043" for h in search_all(engine, "stew"))
    # Corrupt one search_text, then verify dry-run + apply rebuild.
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE recipes SET search_text='garbage' "
                "WHERE dataset_id='AkashPS11/recipes_data_food.com'"
            )
        )
    assert (
        rebuild(engine, dataset_id="AkashPS11/recipes_data_food.com", dry_run=True, limit=None)[
            "would_update"
        ]
        == 1
    )
    result = rebuild(
        engine, dataset_id="AkashPS11/recipes_data_food.com", dry_run=False, limit=None
    )
    assert result["updated"] == 1
    assert any(h["source_id"] == "000038" for h in search_recipes(engine, "Low Cholesterol"))
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT search_text, search_document_version FROM recipes "
                    "WHERE dataset_id='AkashPS11/recipes_data_food.com'"
                )
            )
            .mappings()
            .one()
        )
        assert "Low Cholesterol" in row["search_text"]
        assert row["search_document_version"] == "1"


def test_transaction_rollback_preserves_state(engine):
    with engine.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM recipes")).scalar_one()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO recipes (dataset_id, source_id, import_id, title,"
                    " total_minutes, servings, ingredient_names, document, search_text)"
                    " VALUES ('AkashPS11/recipes_data_food.com','rollback-probe',"
                    " 'test-AkashPS11-recipes_data_food.com','t',1,1,'{}','{}','t')"
                )
            )
            raise RuntimeError("intentional failure")
    except RuntimeError:
        pass
    with engine.connect() as conn:
        after = conn.execute(text("SELECT count(*) FROM recipes")).scalar_one()
        assert after == before
        payload = conn.execute(
            text("SELECT document FROM recipes WHERE source_id='foodie-000043'")
        ).scalar_one()
        assert json.loads(json.dumps(dict(payload)))["title"] == "Test Pilot Stew"
