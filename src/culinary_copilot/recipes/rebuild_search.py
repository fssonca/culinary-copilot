"""Idempotent search_text rebuild (Workstream 1A, reviewed — NOT run here).

Rebuilds ``recipes.search_text`` from the stored ``document`` JSON using the
versioned renderer in :mod:`culinary_copilot.recipes.search`, without
downloading the dataset and without replacing recipe records.

Reviewed command (run later, after review, against a chosen database):

.. code-block:: bash

    uv run python -m culinary_copilot.recipes.rebuild_search --dataset \\
        AkashPS11/recipes_data_food.com --dry-run
    uv run python -m culinary_copilot.recipes.rebuild_search --dataset \\
        AkashPS11/recipes_data_food.com --apply

With migration 002 applied, ``--apply`` also stamps
``search_document_version``. Without 002 it updates ``search_text`` only.
The command is idempotent: rows already matching the renderer are skipped.
"""

import argparse
from typing import Any

from sqlalchemy import text

from culinary_copilot.config import Settings
from culinary_copilot.db import create_db_engine
from culinary_copilot.recipes.search import SEARCH_DOCUMENT_VERSION, render_from_recipe


def rebuild(
    engine: Any, *, dataset_id: str | None, dry_run: bool, limit: int | None
) -> dict[str, Any]:
    """Recompute search_text from document; return counts. Commits unless dry_run."""
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(73190422)"))
        has_version = (
            conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='recipes' AND column_name='search_document_version'"
                )
            ).scalar_one_or_none()
            is not None
        )
        query = "SELECT dataset_id, source_id, document FROM recipes"
        params: dict[str, Any] = {}
        if dataset_id:
            query += " WHERE dataset_id=:dataset"
            params["dataset"] = dataset_id
        query += " ORDER BY dataset_id, source_id"
        if limit:
            query += " LIMIT :limit"
            params["limit"] = limit
        rows = conn.execute(text(query), params).mappings().all()
        checked = updated = 0
        for row in rows:
            expected = render_from_recipe(row["document"])
            current = conn.execute(
                text("SELECT search_text FROM recipes WHERE dataset_id=:d AND source_id=:s"),
                {"d": row["dataset_id"], "s": row["source_id"]},
            ).scalar_one()
            checked += 1
            if current != expected and not dry_run:
                if has_version:
                    conn.execute(
                        text(
                            "UPDATE recipes SET search_text=:t, "
                            "search_document_version=:v "
                            "WHERE dataset_id=:d AND source_id=:s"
                        ),
                        {
                            "t": expected,
                            "v": SEARCH_DOCUMENT_VERSION,
                            "d": row["dataset_id"],
                            "s": row["source_id"],
                        },
                    )
                else:
                    conn.execute(
                        text(
                            "UPDATE recipes SET search_text=:t WHERE dataset_id=:d AND source_id=:s"
                        ),
                        {"t": expected, "d": row["dataset_id"], "s": row["source_id"]},
                    )
                updated += 1
            elif current != expected:
                updated += 1
        if dry_run:
            return {
                "checked": checked,
                "would_update": updated,
                "search_document_version": SEARCH_DOCUMENT_VERSION,
                "applied": False,
            }
        return {
            "checked": checked,
            "updated": updated,
            "search_document_version": SEARCH_DOCUMENT_VERSION,
            "applied": True,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("Use either --dry-run or --apply")
    if not args.apply and not args.dry_run:
        parser.error("One of --dry-run or --apply is required (no default write)")
    engine = create_db_engine(Settings())
    try:
        print(
            rebuild(
                engine,
                dataset_id=args.dataset,
                dry_run=args.dry_run or not args.apply,
                limit=args.limit,
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
