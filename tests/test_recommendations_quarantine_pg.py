"""Quarantine exclusion at the recommendation boundary (disposable Postgres).

Uses a disposable ``culinary_test_rec_quarantine`` database created and
dropped here, with synthetic fixtures only; never the application database.
Skipped when no PostgreSQL server is reachable.

Rows reach the database only through the real loaders
(``import_data.audit``/``persist`` for Food.com, ``llm_batch.cmd_load`` for
the hybrid path). Retrieval (``search_all``/``search_recipes``) and exact
lookup (``get_recipe``) read only the ``recipes`` table; ``recipe_quarantine``
is audit history that no serving path reads. Pinned semantics:

- A quarantined-only identity can never become a candidate.
- A Food.com ``duplicate_id`` quarantine row coexists with the first
  accepted row of the same ``RecipeId``; the accepted content is served.
- On the hybrid (upsert) path, a historical quarantine entry does not
  invalidate a later accepted version, and a later quarantine entry does
  not retract an earlier accepted row: ``recipes`` alone decides what is
  served, and quarantine history is retained.
"""

import csv
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from test_recipes import row
from test_recommendations import _run, _settings, _store_with_group

from culinary_copilot.config import Settings
from culinary_copilot.llm.client import FakeApplicationProvider
from culinary_copilot.recipes import import_data, llm_batch
from culinary_copilot.recipes.import_data import REQUIRED
from culinary_copilot.recipes.repository import get_recipe, search_all
from culinary_copilot.recommendations.epicure import FakeEpicureAdapter
from culinary_copilot.recommendations.service import RecommendationFailure, recommend_for_group
from culinary_copilot.services.answers import init_state

TEST_DB = "culinary_test_rec_quarantine"
FOODCOM = "AkashPS11/recipes_data_food.com"
FOODIE = "odunola/foodie"


def _urls() -> tuple[str, str]:
    settings = Settings(_env_file=None)
    head, _, _ = settings.database_url.get_secret_value().rpartition("/")
    return f"{head}/postgres", f"{head}/{TEST_DB}"


@pytest.fixture(scope="module")
def engine():
    maint_url, test_url = _urls()
    try:
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
        maint.dispose()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL unavailable for disposable tests: {exc!r}")
    eng = create_engine(test_url)
    try:
        with eng.begin() as conn:
            for migration in sorted(import_data.MIGRATIONS_DIR.glob("*.sql")):
                for statement in import_data.split_sql_statements(migration.read_text()):
                    conn.execute(text(statement))
        yield eng
    finally:
        eng.dispose()
        maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
        maint.dispose()


def _recommend(engine: Any, fake: FakeApplicationProvider, *, dataset_id: str) -> dict[str, Any]:
    state = init_state(
        request_id="req-quarantine",
        request={"ingredients": ["chicken"]},
        dish="chicken curry",
    )
    store, state, group = _store_with_group(state)
    return _run(
        recommend_for_group(
            store=store,
            engine=engine,
            settings=_settings(),
            provider=fake,
            epicure=FakeEpicureAdapter(),
            group_id=group.group_id,
            expected_request_revision=state.revision,
            expected_group_revision=group.revision,
            dataset_id=dataset_id,
        )
    )


def _selection(ds: str, sid: str, n_ing: int, n_steps: int) -> dict[str, Any]:
    return {
        "dataset_id": ds,
        "source_id": sid,
        "ingredient_refs": [f"ing-{i}" for i in range(n_ing)],
        "step_refs": [f"step-{i}" for i in range(n_steps)],
        "reasons": [],
        "questions": [],
    }


def _foodcom_row(**changes: str) -> dict[str, str]:
    fields = {
        "Name": "Chicken Curry Probe",
        "RecipeIngredientParts": 'c("chicken", "curry powder")',
        "RecipeIngredientQuantities": 'c("1", "2")',
        "RecipeInstructions": 'c("Brown the chicken.", "Add curry powder and simmer.")',
        "TotalTime": "PT25M",
    }
    return row(**{**fields, **changes})


def test_foodcom_quarantine_never_becomes_a_candidate(engine: Any, tmp_path: Path) -> None:
    path = tmp_path / "fixture.csv"
    fields = sorted(REQUIRED | {"Barcode"})
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(
            [
                _foodcom_row(RecipeId="000501"),
                # Quarantined only: no instructions.
                _foodcom_row(
                    RecipeId="000502",
                    Name="Chicken Curry Probe Quarantined",
                    RecipeInstructions="NA",
                ),
                # Duplicate id: quarantined beside the accepted 000501.
                _foodcom_row(RecipeId="000501", Name="Chicken Curry Probe Duplicate"),
            ]
        )
    recipes, rejected, report = import_data.audit(path, set())
    assert {r["reason"] for r in rejected} == {"incomplete_ingredients_or_steps", "duplicate_id"}
    provenance = {
        "id": "imp-foodcom-q",
        "dataset_id": FOODCOM,
        "revision": "test-rev",
        "checksum": "test-checksum",
        "normalizer_version": "2",
        "vocabulary_checksum": "test-vocab",
        "dataset_url": "https://example.invalid/test",
    }
    import_data.persist(engine, recipes, rejected, provenance, report, False)

    with engine.connect() as conn:
        quarantined = conn.execute(
            text("SELECT raw->>'RecipeId', reason FROM recipe_quarantine ORDER BY row_number")
        ).all()
        served = conn.execute(
            text("SELECT source_id, title FROM recipes WHERE dataset_id=:d"), {"d": FOODCOM}
        ).all()
    assert [tuple(r) for r in quarantined] == [
        ("000502", "incomplete_ingredients_or_steps"),
        ("000501", "duplicate_id"),
    ]
    assert [tuple(r) for r in served] == [("000501", "Chicken Curry Probe")]

    # Retrieval and exact lookup read only the recipes table.
    hits = search_all(engine, "chicken curry probe", limit=10)
    assert [(h["dataset_id"], h["source_id"]) for h in hits] == [(FOODCOM, "000501")]
    assert get_recipe(engine, "000502", dataset_id=FOODCOM) is None
    assert get_recipe(engine, "000501", dataset_id=FOODCOM)["title"] == "Chicken Curry Probe"

    # Full workflow: only the accepted row is offered; exact identity kept.
    fake = FakeApplicationProvider(script=[_selection(FOODCOM, "000501", 2, 2)])
    result = _recommend(engine, fake, dataset_id=FOODCOM)
    assert result["outcome"] == "recommendation"
    assert result["selection"] == {
        "dataset_id": FOODCOM,
        "source_id": "000501",
        "title": "Chicken Curry Probe",
    }
    offered = result["evidence"]["offered_candidates"]
    assert [(c["dataset_id"], c["source_id"]) for c in offered] == [(FOODCOM, "000501")]

    # A proposal naming the quarantined identity is an unknown identity.
    blocked = FakeApplicationProvider(script=[_selection(FOODCOM, "000502", 2, 2)])
    with pytest.raises(RecommendationFailure) as exc_info:
        _recommend(engine, blocked, dataset_id=FOODCOM)
    assert exc_info.value.detail["validation_reason"] == "unknown_identity"


def _load_args(run_dir: Path, import_id: str) -> SimpleNamespace:
    return SimpleNamespace(run_dir=str(run_dir), import_id=import_id, partial=True)


def _hybrid_run(
    tmp_path: Path, name: str, *, ready: list[dict[str, Any]], quarantined: list[str]
) -> Path:
    run_dir = tmp_path / name
    run_dir.mkdir()
    (run_dir / "ready_to_load.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in ready), encoding="utf-8"
    )
    records = [{"source_id": r["source_id"], "state": "ready_to_load"} for r in ready] + [
        {"source_id": sid, "state": "quarantined"} for sid in quarantined
    ]
    (run_dir / "records.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    (run_dir / "final-quarantine.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "source_id": sid,
                    "row_number": int(sid.rsplit("-", 1)[1]),
                    "reason": "single_line_requires_spans",
                    "raw": {"texts": "Chicken curry probe blob"},
                }
            )
            + "\n"
            for sid in quarantined
        ),
        encoding="utf-8",
    )
    (run_dir / "manifest.json").write_text(
        json.dumps({"revision": "test-rev", "file_sha256": "test-sha"}) + "\n"
    )
    return run_dir


def _foodie_record(source_id: str, title: str) -> dict[str, Any]:
    return {
        "dataset_id": FOODIE,
        "source_id": source_id,
        "title": title,
        "ingredients": [
            {
                "original": "chicken",
                "canonical": "chicken",
                "name": "chicken",
                "amount": None,
                "quantity_text": None,
                "unit": None,
            },
            {
                "original": "curry powder",
                "canonical": "curry powder",
                "name": "curry powder",
                "amount": None,
                "quantity_text": None,
                "unit": None,
            },
        ],
        "instructions": ["Brown the chicken.", "Add curry powder and simmer."],
        "servings": None,
        "durations_minutes": {"TotalTime": None},
        "quality_issues": [],
        "capabilities": {},
    }


def test_hybrid_quarantine_history_and_accepted_versions(engine: Any, tmp_path: Path) -> None:
    _, test_url = _urls()
    settings = Settings(_env_file=None, database_url=test_url)

    # 1. Quarantined only: never a candidate.
    llm_batch.cmd_load(
        _load_args(
            _hybrid_run(tmp_path, "hist", ready=[], quarantined=["foodie-000077"]),
            "imp-hist",
        ),
        settings,
    )
    assert get_recipe(engine, "foodie-000077", dataset_id=FOODIE) is None
    empty = _recommend(engine, FakeApplicationProvider(script=[]), dataset_id=FOODIE)
    assert empty["outcome"] == "insufficient_evidence"
    assert empty["insufficient_reason"] == "no_candidates"

    # 2. A later accepted version is served despite the historical entry.
    accepted = _foodie_record("foodie-000077", "Chicken Curry Probe Accepted")
    llm_batch.cmd_load(
        _load_args(_hybrid_run(tmp_path, "new", ready=[accepted], quarantined=[]), "imp-new"),
        settings,
    )
    fake = FakeApplicationProvider(script=[_selection(FOODIE, "foodie-000077", 2, 2)])
    result = _recommend(engine, fake, dataset_id=FOODIE)
    assert result["outcome"] == "recommendation"
    assert result["selection"]["source_id"] == "foodie-000077"
    assert result["recipe"]["title"] == "Chicken Curry Probe Accepted"

    # 3. A later quarantine entry does not retract the accepted row: the
    #    upsert loader never deletes recipes, so it stays served.
    llm_batch.cmd_load(
        _load_args(
            _hybrid_run(tmp_path, "later", ready=[], quarantined=["foodie-000077"]),
            "imp-later",
        ),
        settings,
    )
    with engine.connect() as conn:
        history = conn.execute(
            text(
                "SELECT import_id, status FROM recipe_quarantine "
                "WHERE source_id='foodie-000077' ORDER BY import_id"
            )
        ).all()
        served_import = conn.execute(
            text("SELECT import_id FROM recipes WHERE dataset_id=:d AND source_id='foodie-000077'"),
            {"d": FOODIE},
        ).scalar_one()
    assert [tuple(r) for r in history] == [
        ("imp-hist", "quarantined"),
        ("imp-later", "quarantined"),
    ]
    assert served_import == "imp-new"
    assert get_recipe(engine, "foodie-000077", dataset_id=FOODIE)["title"] == (
        "Chicken Curry Probe Accepted"
    )
