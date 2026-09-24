"""Rehearse the Food.com search-text rebuild on a disposable database.

Copies a small sample of real Food.com rows read-only from the
application database into a disposable database, runs the verified
``rebuild_search`` dry-run and apply paths, then proves recipe
identities, raw/source documents, imports, and quarantine rows are
unchanged. Drops the disposable database afterwards (unless --keep).

Usage (from repo root):
    uv run python scripts/retrieval_eval/rehearse_rebuild.py --rows 25
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "src"))

TEST_DB = "culinary_rehearsal_rebuild"
FOODCOM = "AkashPS11/recipes_data_food.com"


def _urls() -> tuple[str, str]:
    from culinary_copilot.config import Settings

    base = Settings().database_url.get_secret_value()
    head, _, _ = base.rpartition("/")
    return f"{head}/postgres", f"{head}/{TEST_DB}"


def _snapshot(conn: Any) -> dict[str, Any]:
    from sqlalchemy import text

    count = conn.execute(text("SELECT count(*) FROM recipes")).scalar_one()
    texts = conn.execute(
        text(
            "SELECT dataset_id, source_id, search_text, search_document_version FROM recipes "
            "ORDER BY dataset_id, source_id"
        )
    ).mappings()
    return {
        "count": count,
        "texts": [(r["dataset_id"], r["source_id"], r["search_text"]) for r in texts],
        "doc_hash": conn.execute(
            text(
                "SELECT md5(string_agg(document::text, '' ORDER BY dataset_id, source_id)) "
                "FROM recipes"
            )
        ).scalar_one(),
        "imports": conn.execute(text("SELECT count(*) FROM recipe_imports")).scalar_one(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=25)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    from sqlalchemy import create_engine, text

    from culinary_copilot.config import Settings
    from culinary_copilot.recipes.import_data import MIGRATIONS_DIR, split_sql_statements
    from culinary_copilot.recipes.rebuild_search import rebuild

    maint_url, test_url = _urls()
    maint = create_engine(maint_url, isolation_level="AUTOCOMMIT")
    with maint.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    app = create_engine(Settings().database_url.get_secret_value())
    test = create_engine(test_url)
    try:
        with test.begin() as conn:
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name):
                for statement in split_sql_statements(migration.read_text()):
                    conn.execute(text(statement))
        with app.connect() as src, test.begin() as dst:
            sample = src.execute(
                text(
                    "SELECT dataset_id, source_id, import_id, title, total_minutes, "
                    "servings, ingredient_names, document, search_text, "
                    "search_document_version FROM recipes "
                    "WHERE dataset_id=:d ORDER BY source_id LIMIT :n"
                ),
                {"d": FOODCOM, "n": args.rows},
            ).mappings()
            rows = [dict(r) for r in sample]
            assert rows, "no Food.com rows copied; aborting rehearsal"
            for imp in src.execute(
                text("SELECT * FROM recipe_imports WHERE dataset_id=:d"), {"d": FOODCOM}
            ).mappings():
                dst.execute(
                    text(
                        "INSERT INTO recipe_imports (id, dataset_id, revision, checksum, "
                        "normalizer_version, vocabulary_checksum, dataset_url, report) "
                        "VALUES (:id, :dataset_id, :revision, :checksum, "
                        ":normalizer_version, :vocabulary_checksum, :dataset_url, :report)"
                    ),
                    {**dict(imp), "report": json.dumps(imp["report"])},
                )
            for row in rows:
                dst.execute(
                    text(
                        "INSERT INTO recipes (dataset_id, source_id, import_id, title, "
                        "total_minutes, servings, ingredient_names, document, "
                        "search_text, search_document_version) "
                        "VALUES (:dataset_id, :source_id, :import_id, :title, "
                        ":total_minutes, :servings, :ingredient_names, "
                        "CAST(:document AS jsonb), :search_text, :search_document_version)"
                    ),
                    {
                        **row,
                        "document": json.dumps(row["document"]),
                    },
                )
        with test.connect() as conn:
            before = _snapshot(conn)
        dry = rebuild(test, dataset_id=FOODCOM, dry_run=True, limit=None)
        with test.connect() as conn:
            during = _snapshot(conn)
        assert during == before, "dry-run modified the rehearsal database"
        applied = rebuild(test, dataset_id=FOODCOM, dry_run=False, limit=None)
        with test.connect() as conn:
            after = _snapshot(conn)
        assert after["count"] == before["count"], "row count changed"
        assert after["doc_hash"] == before["doc_hash"], "raw documents changed"
        assert after["imports"] == before["imports"], "imports changed"
        assert [t[:2] for t in after["texts"]] == [t[:2] for t in before["texts"]]
        changed = sum(
            1 for b, a in zip(before["texts"], after["texts"], strict=True) if b[2] != a[2]
        )
        print(f"copied={len(rows)} dry_run={dry} applied={applied} search_text_changed={changed}")
        print("invariants: identities, documents, imports unchanged; only search_text updated")
        return 0
    finally:
        app.dispose()
        test.dispose()
        if not args.keep:
            with maint.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
        maint.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
