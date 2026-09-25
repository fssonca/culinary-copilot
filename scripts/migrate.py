#!/usr/bin/env python3
"""Migration-only CLI: applies pending schema migrations and nothing else.

Unlike ``import_data --write --apply-schema`` (which re-imports and replaces
the Food.com snapshot) and ``llm_batch load --apply-schema``, this command
never touches recipe data. It exists so the application migration (004,
pgvector activation) runs as an isolated, reviewable step.

- ``--dry-run`` lists pending versions and extension availability with zero
  writes (read-only SELECTs only; not even the migrations table is created).
- Without ``--dry-run``, the target must match ``--expect-db-name`` and
  ``--expect-db-host`` exactly, otherwise the command refuses (exit 2).
- Skipped vector migrations are reported with their reason, never applied
  partially.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from typing import Any
from urllib.parse import urlparse


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--expect-db-name", default="")
    parser.add_argument("--expect-db-host", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def _target_identity(url: str) -> tuple[str, str]:
    """Return ``(host, dbname)`` for guard comparison ("" when unparseable)."""
    try:
        parsed = urlparse(url if "://" in url else f"postgresql://{url}")
        name = (parsed.path or "").lstrip("/").split("?")[0]
        return (parsed.hostname or "", name)
    except ValueError:
        return ("", "")


def _dry_run_report(engine: Any) -> list[str]:
    """Pending versions + extension availability; zero writes."""
    from sqlalchemy import text

    from culinary_copilot.recipes.import_data import (
        MIGRATIONS_DIR,
        _extension_available,
        _migration_required_extension,
    )

    lines: list[str] = []
    with engine.connect() as conn:
        try:
            applied = {
                row[0] for row in conn.execute(text("SELECT version FROM recipe_schema_migrations"))
            }
        except Exception:
            applied = set()
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = migration.name.split("_", 1)[0]
            digest = hashlib.sha256(migration.read_bytes()).hexdigest()[:12]
            required = _migration_required_extension(migration)
            if version in applied:
                lines.append(f"{version} ({migration.name}) applied [{digest}]")
            elif required is not None and not _extension_available(conn, required):
                lines.append(
                    f"{version} ({migration.name}) PENDING — skipped: requires extension "
                    f"'{required}', unavailable on this database"
                )
            else:
                lines.append(f"{version} ({migration.name}) PENDING — will apply [{digest}]")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    from sqlalchemy import create_engine

    from culinary_copilot.config import Settings
    from culinary_copilot.recipes.import_data import apply_migrations

    url = args.database_url or Settings().database_url.get_secret_value()
    if args.dry_run:
        engine = create_engine(url)
        try:
            for line in _dry_run_report(engine):
                print(line)
            print("dry-run: zero writes.")
        finally:
            engine.dispose()
        return 0
    host, name = _target_identity(url)
    if not args.expect_db_name or not args.expect_db_host:
        print("error: --expect-db-name and --expect-db-host are required", file=sys.stderr)
        return 2
    if name != args.expect_db_name or host != args.expect_db_host:
        print(
            f"error: target ({host}/{name}) does not match expected "
            f"({args.expect_db_host}/{args.expect_db_name}); refusing",
            file=sys.stderr,
        )
        return 2
    engine = create_engine(url)
    reasons: list[str] = []
    try:
        with engine.begin() as conn:
            applied = apply_migrations(conn, listener=reasons.append)
    finally:
        engine.dispose()
    for version in applied:
        print(f"applied {version}")
    for reason in reasons:
        print(reason)
    print(f"done: applied={applied} skipped={len(reasons)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
