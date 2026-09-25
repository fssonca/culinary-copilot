"""Disposable-Postgres checks for Phase 1 retrieval.

Creates/drops only ``culinary_test_retrieval`` with synthetic fixtures.
Never touches the application database. Skipped when PostgreSQL is
unreachable so offline runs stay credential-free.
"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.config import Settings
from culinary_copilot.recipes import import_data
from culinary_copilot.retrieval.service import retrieve_for_group
from culinary_copilot.services.answers import init_state
from culinary_copilot.services.clarification_service import make_group
from culinary_copilot.services.store import InMemoryClarificationStore

TEST_DB = "culinary_test_retrieval"
FOODCOM = "AkashPS11/recipes_data_food.com"
FOODIE = "odunola/foodie"


def _urls() -> tuple[str, str]:
    settings = Settings()
    base = settings.database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/{TEST_DB}"


def _import_row(dataset: str) -> dict:
    return {
        "id": f"test-retrieval-{dataset.replace('/', '-')}",
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
    servings: float | None,
    names: list[str],
    text_body: str,
    document: dict,
) -> dict:
    return {
        "dataset_id": dataset,
        "source_id": source_id,
        "import_id": f"test-retrieval-{dataset.replace('/', '-')}",
        "title": title,
        "minutes": minutes,
        "servings": servings,
        "names": names,
        "document": json.dumps(document),
        "search_text": text_body,
    }


def _doc(dataset: str, source_id: str, title: str, **overrides) -> dict:
    base = {
        "title": title,
        "source_id": source_id,
        "provenance": {"dataset_id": dataset, "source_id": source_id},
        "servings": 2.0,
        "durations_minutes": {"TotalTime": 20.0},
        "ingredients": [{"canonical": "garlic"}],
        "instructions": ["Cook it.", "Serve."],
        "flags": [],
        "capabilities": {"searchable": True, "scalable": False},
        "available_fields": {"servings": True},
        "quality_issues": [],
        "nutrition": {"calories": None},
        "description": "Test doc.",
        "source_url": None,
    }
    base.update(overrides)
    return base


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
            import_data.apply_migrations(conn)
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
                    "000159",
                    "Chicken Curry",
                    107.0,
                    4.0,
                    ["chicken", "curry powder"],
                    "chicken curry toasted spices dinner",
                    _doc(FOODCOM, "000159", "Chicken Curry", servings=4.0),
                ),
                _recipe(
                    FOODIE,
                    "foodie-000001",
                    "Chimodho",
                    None,
                    None,
                    ["buttermilk", "sunflower oil"],
                    "chimodho baked test dish",
                    _doc(
                        FOODIE,
                        "foodie-000001",
                        "Chimodho",
                        servings=None,
                        durations_minutes={
                            "TotalTime": None,
                            "PrepTime": None,
                            "CookTime": None,
                        },
                    ),
                ),
                _recipe(
                    FOODCOM,
                    "zero-stew",
                    "Zero Stew",
                    0.0,
                    2.0,
                    ["cabbage"],
                    "zero stew cabbage pot simmered",
                    _doc(
                        FOODCOM,
                        "zero-stew",
                        "Zero Stew",
                        durations_minutes={"TotalTime": 0.0},
                    ),
                ),
                _recipe(
                    FOODCOM,
                    "neg-noodles",
                    "Negative Noodles",
                    -5.0,
                    2.0,
                    ["noodle"],
                    "negative noodles quick bowl",
                    _doc(
                        FOODCOM,
                        "neg-noodles",
                        "Negative Noodles",
                        durations_minutes={"TotalTime": -5.0},
                    ),
                ),
                _recipe(
                    FOODCOM,
                    "nan-soup",
                    "Nan Soup",
                    float("nan"),
                    2.0,
                    ["potato"],
                    "nan soup potato bowl",
                    _doc(
                        FOODCOM,
                        "nan-soup",
                        "Nan Soup",
                        durations_minutes={"TotalTime": 20.0},
                    ),
                ),
                _recipe(
                    FOODCOM,
                    "oo-pasta",
                    "Olive Oil Pasta",
                    20.0,
                    2.0,
                    ["olive oil", "pasta", "garlic"],
                    "olive oil pasta garlic dinner",
                    _doc(
                        FOODCOM,
                        "oo-pasta",
                        "Olive Oil Pasta",
                        ingredients=[{"canonical": "olive oil"}, {"canonical": "garlic"}],
                    ),
                ),
                _recipe(
                    FOODCOM,
                    "garlic-bread",
                    "Garlic Bread",
                    15.0,
                    2.0,
                    ["garlic", "bread"],
                    "garlic bread baked side",
                    _doc(
                        FOODCOM,
                        "garlic-bread",
                        "Garlic Bread",
                        ingredients=[{"canonical": "garlic"}],
                    ),
                ),
                _recipe(
                    FOODCOM,
                    "chicken-garlic-roast",
                    "Chicken Garlic Roast",
                    40.0,
                    4.0,
                    ["chicken", "garlic"],
                    "chicken garlic roast dinner",
                    _doc(
                        FOODCOM,
                        "chicken-garlic-roast",
                        "Chicken Garlic Roast",
                        servings=4.0,
                        durations_minutes={"TotalTime": 40.0},
                        ingredients=[{"canonical": "chicken"}, {"canonical": "garlic"}],
                    ),
                ),
                _recipe(
                    FOODCOM,
                    "old-001",
                    "Old Soup",
                    25.0,
                    None,
                    ["cabbage", "onion"],
                    "old soup cabbage simmered pot",
                    # Old import generation: no capabilities/available_fields.
                    {
                        "title": "Old Soup",
                        "source_id": "old-001",
                        "provenance": {
                            "dataset_id": FOODCOM,
                            "source_id": "old-001",
                        },
                        "servings": None,
                        "durations_minutes": {"TotalTime": 25.0},
                        "ingredients": [{"canonical": "cabbage"}],
                        "instructions": ["Simmer."],
                        "flags": ["units_unknown"],
                        "nutrition": {"calories": None},
                        "description": None,
                        "source_url": None,
                    },
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
        pytest.skip(f"PostgreSQL unavailable for retrieval tests: {exc!r}")


def _ready_store(dish=None, ingredients=None, time_minutes=None):
    state = init_state(
        request_id="req-pg",
        request={
            "ingredients": ingredients or [],
            **({"time_minutes": time_minutes} if time_minutes else {}),
        },
        dish=dish,
    )
    store = InMemoryClarificationStore()
    group = make_group(state=state, questions=[], created_by="rule_only")
    store.create(state, group)
    return store, state, group


def test_ready_flow_returns_full_evidence(engine) -> None:
    store, state, group = _ready_store(dish="Chicken Curry")
    result = asyncio.run(
        retrieve_for_group(
            store=store,
            engine=engine,
            group_id=group.group_id,
            expected_request_revision=state.revision,
            expected_group_revision=group.revision,
        )
    )
    assert result["outcome"] == "ready"
    assert result["request_revision"] == state.revision
    assert result["result_count"] >= 1
    first = result["results"][0]
    assert (first["dataset_id"], first["source_id"]) == (FOODCOM, "000159")
    assert first["document_available"] is True
    assert first["provenance"]["dataset_id"] == FOODCOM
    assert first["excerpt"]
    assert "dietary_compatibility" in first["unknowns"]


def test_time_ceiling_excludes_unknown_durations(engine) -> None:
    store, state, group = _ready_store(dish="Chimodho", time_minutes=30)
    result = asyncio.run(
        retrieve_for_group(
            store=store,
            engine=engine,
            group_id=group.group_id,
            expected_request_revision=state.revision,
            expected_group_revision=group.revision,
            dataset_id=FOODIE,
        )
    )
    assert result["outcome"] == "ready"
    # foodie-000001 has unknown total_minutes: excluded by the ceiling.
    assert result["result_count"] == 0
    assert "No recipes matched" in result["explanation"]
    assert "max_minutes" in result["query"]["applied_filters"]


def test_old_document_unknowns_stay_unknown(engine) -> None:
    store, state, group = _ready_store(dish="Old Soup")
    result = asyncio.run(
        retrieve_for_group(
            store=store,
            engine=engine,
            group_id=group.group_id,
            expected_request_revision=state.revision,
            expected_group_revision=group.revision,
        )
    )
    assert result["result_count"] >= 1
    evidence = next(r for r in result["results"] if r["source_id"] == "old-001")
    assert evidence["capabilities_unknown"] is True
    assert evidence["servings_known"] is False
    assert "capabilities" in evidence["unknowns"]


def test_dataset_slice_isolation(engine) -> None:
    store, state, group = _ready_store(dish="Chicken Curry")
    foodie_only = asyncio.run(
        retrieve_for_group(
            store=store,
            engine=engine,
            group_id=group.group_id,
            expected_request_revision=state.revision,
            expected_group_revision=group.revision,
            dataset_id=FOODIE,
        )
    )
    assert all(r["dataset_id"] == FOODIE for r in foodie_only["results"])
    assert all(
        (r["dataset_id"], r["source_id"]) != (FOODCOM, "000159") for r in foodie_only["results"]
    )


def test_dish_match_with_zero_pantry_overlap_stays_eligible(engine) -> None:
    from culinary_copilot.recipes.repository import search_all

    plain = search_all(engine, "Chicken Curry", limit=10)
    boosted = search_all(engine, "Chicken Curry", limit=10, rank_pantry_terms=["tofu", "miso"])
    plain_ids = {(r["dataset_id"], r["source_id"]) for r in plain}
    boosted_ids = {(r["dataset_id"], r["source_id"]) for r in boosted}
    # Eligibility is identical with and without pantry terms: pantry only ranks.
    assert plain_ids == boosted_ids
    assert (FOODCOM, "000159") in boosted_ids

    store, state, group = _ready_store(dish="Chicken Curry", ingredients=["tofu", "miso"])
    result = asyncio.run(
        retrieve_for_group(
            store=store,
            engine=engine,
            group_id=group.group_id,
            expected_request_revision=state.revision,
            expected_group_revision=group.revision,
        )
    )
    assert (FOODCOM, "000159") in {(r["dataset_id"], r["source_id"]) for r in result["results"]}


def test_pantry_only_matches_any_with_overlap_ranking(engine) -> None:
    from culinary_copilot.recipes.repository import search_all

    rows = search_all(
        engine, "chicken garlic", limit=10, match_any_ingredients=["chicken", "garlic"]
    )
    ids = [(r["dataset_id"], r["source_id"]) for r in rows]
    assert (FOODCOM, "chicken-garlic-roast") in ids  # two overlaps
    assert (FOODCOM, "garlic-bread") in ids  # one overlap
    # Higher overlap ranks first; ties break by score then identity.
    assert ids.index((FOODCOM, "chicken-garlic-roast")) < ids.index((FOODCOM, "garlic-bread"))
    assert all(r["pantry_overlap"] >= 1 for r in rows)
    assert rows[0]["pantry_overlap"] == 2


def test_pantry_only_preserves_multiword_meaning(engine) -> None:
    from culinary_copilot.recipes.repository import search_all

    rows = search_all(engine, "olive oil", limit=10, match_any_ingredients=["olive oil"])
    assert [(r["dataset_id"], r["source_id"]) for r in rows] == [(FOODCOM, "oo-pasta")]


def test_required_ingredient_filter_stays_mandatory(engine) -> None:
    from culinary_copilot.recipes.repository import search_all

    rows = search_all(engine, "garlic", limit=10, ingredients=["chicken"])
    assert [(r["dataset_id"], r["source_id"]) for r in rows] == [(FOODCOM, "chicken-garlic-roast")]


def test_duration_policy_null_zero_negative_nan(engine) -> None:
    from culinary_copilot.recipes.repository import search_all

    stew = search_all(engine, "zero stew", limit=10)
    assert (FOODCOM, "zero-stew") in {(r["dataset_id"], r["source_id"]) for r in stew}
    ceiling = search_all(engine, "zero stew", limit=10, max_minutes=30)
    assert (FOODCOM, "zero-stew") not in {(r["dataset_id"], r["source_id"]) for r in ceiling}
    assert (FOODCOM, "old-001") in {
        (r["dataset_id"], r["source_id"])
        for r in search_all(engine, "old soup", limit=10, max_minutes=30)
    }
    noodles = search_all(engine, "negative noodles", limit=10, max_minutes=30)
    assert (FOODCOM, "neg-noodles") not in {(r["dataset_id"], r["source_id"]) for r in noodles}
    nan_soup = search_all(engine, "nan soup", limit=10, max_minutes=30)
    assert (FOODCOM, "nan-soup") not in {(r["dataset_id"], r["source_id"]) for r in nan_soup}


def test_zero_total_evidence_reports_raw_as_unverified(engine) -> None:
    store, state, group = _ready_store(dish="Zero Stew")
    result = asyncio.run(
        retrieve_for_group(
            store=store,
            engine=engine,
            group_id=group.group_id,
            expected_request_revision=state.revision,
            expected_group_revision=group.revision,
        )
    )
    evidence = next(r for r in result["results"] if r["source_id"] == "zero-stew")
    assert evidence["duration_status"] == "reported_zero_unverified"
    assert evidence["total_minutes"] is None
    assert evidence["total_minutes_reported"] == 0.0
    assert "total_minutes" in evidence["unknowns"]


def test_no_match_and_not_ready_and_stale(engine) -> None:
    store, state, group = _ready_store(dish="zzzznomatch xyzzy")
    empty = asyncio.run(
        retrieve_for_group(
            store=store,
            engine=engine,
            group_id=group.group_id,
            expected_request_revision=state.revision,
            expected_group_revision=group.revision,
        )
    )
    assert empty["result_count"] == 0
    assert "No constraints were dropped" in empty["explanation"]

    vague_store, vague_state, vague_group = _ready_store()
    # No dish and no ingredients: not ready, no DB search claimed.
    not_ready = asyncio.run(
        retrieve_for_group(
            store=vague_store,
            engine=engine,
            group_id=vague_group.group_id,
            expected_request_revision=vague_state.revision,
            expected_group_revision=vague_group.revision,
        )
    )
    assert not_ready["outcome"] == "not_ready"

    with pytest.raises(Exception, match="stale revision"):
        asyncio.run(
            retrieve_for_group(
                store=store,
                engine=engine,
                group_id=group.group_id,
                expected_request_revision=999,
                expected_group_revision=group.revision,
            )
        )
