"""H5 summary-layout cleanup script (disposable Postgres, synthetic rows)."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from culinary_copilot.config import Settings
from culinary_copilot.recipes import import_data

TEST_DB = "culinary_test_h5_cleanup"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "datasets" / "h5_summary_cleanup.py"
_spec = importlib.util.spec_from_file_location("h5_summary_cleanup", SCRIPT)
assert _spec is not None and _spec.loader is not None
cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cleanup)

IMPORT_ID = "foodie-repair-v5-test"


def _urls() -> tuple[str, str]:
    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/{TEST_DB}"


@pytest.fixture()
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
            _seed(conn)
        yield eng
        eng.dispose()
    except SQLAlchemyError as exc:
        pytest.skip(f"PostgreSQL unavailable for H5 cleanup tests: {exc!r}")
    finally:
        try:
            maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
            with maint.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            maint.dispose()
        except Exception:
            pass


def _seed(conn: Any) -> None:
    conn.execute(
        text(
            "INSERT INTO recipe_imports (id, dataset_id, revision, checksum, normalizer_version, "
            "vocabulary_checksum, dataset_url, report) VALUES ('imp-old', 'odunola/foodie', 'r', "
            "'c', '3', 'v', 'u', '{}')"
        )
    )
    for source_id, title in (
        ("foodie-019350", "summary"),
        ("foodie-019351", "summary"),
        ("foodie-000001", "Real Stew"),
    ):
        conn.execute(
            text(
                "INSERT INTO recipes (dataset_id, source_id, import_id, title, ingredient_names, "
                "document, search_text) VALUES ('odunola/foodie', :s, 'imp-old', :t, "
                "ARRAY['x'], CAST(:d AS jsonb), :t)"
            ),
            {"s": source_id, "t": title, "d": json.dumps({"title": title, "id": source_id})},
        )
    conn.execute(
        text(
            "INSERT INTO recipe_quarantine (import_id, row_number, source_id, reason, raw) "
            "VALUES ('imp-old', 19352, 'foodie-019352', 'old reason', '{}')"
        )
    )


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "manifest.json").write_text(
        json.dumps({"adapter_version": "5", "routing_version": "3", "revision": "r"})
    )
    rows = [
        {"source_id": f"foodie-0{n}", "row_number": n, "reason": cleanup.REASON, "raw": {"t": n}}
        for n in (19350, 19351, 19352)
    ]
    (run / "final-quarantine.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (run / "ready_to_load.jsonl").write_text("")
    return run


def _manifest(engine: Any, run: Path) -> dict[str, Any]:
    with engine.connect() as conn:
        return cleanup.build_manifest(conn, run, IMPORT_ID)


def _state(engine: Any) -> tuple[int, int, int]:
    with engine.connect() as conn:
        return (
            int(conn.execute(text("SELECT count(*) FROM recipes")).scalar() or 0),
            int(conn.execute(text("SELECT count(*) FROM recipe_quarantine")).scalar() or 0),
            int(conn.execute(text("SELECT count(*) FROM recipe_imports")).scalar() or 0),
        )


def test_target_guard_refuses_wrong_database() -> None:
    app = {"host": "127.0.0.1", "port": 5432, "database": "culinary_copilot"}
    rehearsal = {"host": "127.0.0.1", "port": 5432, "database": "culinary_rehearsal_h5_a"}
    with pytest.raises(cleanup.CleanupError, match="application database"):
        cleanup.check_target("rehearsal", dict(app), app)
    with pytest.raises(cleanup.CleanupError, match="must start with"):
        cleanup.check_target("rehearsal", {**rehearsal, "database": "scratch"}, app)
    cleanup.check_target("rehearsal", rehearsal, app)
    with pytest.raises(cleanup.CleanupError, match="does not match"):
        cleanup.check_target("application", rehearsal, app, confirm_application="x")
    with pytest.raises(cleanup.CleanupError, match="repeat"):
        cleanup.check_target("application", dict(app), app, confirm_application="other")
    cleanup.check_target("application", dict(app), app, confirm_application="culinary_copilot")


def test_first_run_then_rerun_writes_nothing(engine, tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    manifest = _manifest(engine, run)
    assert len(manifest["condemned"]) == 2
    assert manifest["expected_after"]["recipes"] == 1
    with engine.begin() as conn:
        first = cleanup.apply(conn, manifest, run)
    assert first["state"] == "first_run"
    assert _state(engine) == (1, 4, 2)
    with engine.begin() as conn:
        again = cleanup.apply(conn, manifest, run)
    assert again["state"] == "already_applied" and again["writes"] == 0
    assert again["older_quarantine_events_kept_for_same_rows"] == 1
    assert _state(engine) == (1, 4, 2)


def test_partial_state_is_refused(engine, tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    manifest = _manifest(engine, run)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM recipes WHERE source_id = 'foodie-019350'"))
    with pytest.raises(cleanup.CleanupError, match="unexpected state"):
        with engine.begin() as conn:
            cleanup.apply(conn, manifest, run)
    assert _state(engine) == (2, 1, 1)


def test_failed_end_check_rolls_everything_back(engine, tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    manifest = copy.deepcopy(_manifest(engine, run))
    manifest["expected_after"]["recipes"] += 1
    with pytest.raises(cleanup.CleanupError, match="counts"):
        with engine.begin() as conn:
            cleanup.apply(conn, manifest, run)
    assert _state(engine) == (3, 1, 1)


def test_changed_survivor_is_refused(engine, tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    manifest = _manifest(engine, run)
    with engine.begin() as conn:
        conn.execute(text("UPDATE recipes SET title = 'Changed' WHERE source_id = 'foodie-000001'"))
    with pytest.raises(cleanup.CleanupError, match="surviving recipes differ"):
        with engine.begin() as conn:
            cleanup.apply(conn, manifest, run)
    assert _state(engine) == (3, 1, 1)
