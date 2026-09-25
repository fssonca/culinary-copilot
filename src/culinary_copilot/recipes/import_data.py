"""Reviewable CLI: preview by default; PostgreSQL writes require --write."""

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from huggingface_hub import hf_hub_download
from sqlalchemy import Engine, text

from culinary_copilot.config import Settings
from culinary_copilot.db import create_db_engine
from culinary_copilot.recipes.normalize import VERSION, normalize
from culinary_copilot.recipes.quality import is_defective
from culinary_copilot.recipes.search import SEARCH_DOCUMENT_VERSION, render_from_recipe

DATASET = "AkashPS11/recipes_data_food.com"
REVISION = "aa68f5bf9c9f33a9fe4624e180d9e80a2030c675"
CHECKSUM = "3f93a145e5449fcd1ddd9c90896b669f8dc17bb2b37f8781c05179dd3765efe7"
REQUIRED = {
    "RecipeId",
    "Name",
    "RecipeIngredientParts",
    "RecipeIngredientQuantities",
    "RecipeInstructions",
    "TotalTime",
    "RecipeServings",
}

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def _availability_counts(recipe: dict[str, Any]) -> dict[str, bool]:
    available = recipe.get("available_fields", {})
    flat: dict[str, bool] = {}
    for key in ("description", "images", "keywords", "nutrition", "ratings", "servings"):
        flat[key] = bool(available.get(key))
    return flat


def sha256_file(path: Path) -> str:
    """Chunked SHA-256; avoids loading multi-GB CSVs and needs no 3.11-only API."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 64), b""):
            digest.update(chunk)
    return digest.hexdigest()


def split_sql_statements(sql: str) -> list[str]:
    """Split on top-level semicolons; respects quotes, comments, dollar-quoting."""
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_single = in_double = in_line_comment = in_block_comment = False
    dollar_tag = ""
    while i < n:
        if in_line_comment:
            buf.append(sql[i])
            if sql[i] == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if sql.startswith("*/", i):
                buf.append("*/")
                i += 2
                in_block_comment = False
            else:
                buf.append(sql[i])
                i += 1
            continue
        if dollar_tag:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = ""
            else:
                buf.append(sql[i])
                i += 1
            continue
        if in_single:
            buf.append(sql[i])
            if sql[i] == "'":
                if sql.startswith("''", i):
                    buf.append("'")
                    i += 2
                else:
                    in_single = False
                    i += 1
            else:
                i += 1
            continue
        if in_double:
            buf.append(sql[i])
            if sql[i] == '"':
                if sql.startswith('""', i):
                    buf.append('"')
                    i += 2
                else:
                    in_double = False
                    i += 1
            else:
                i += 1
            continue
        if sql.startswith("--", i):
            in_line_comment = True
            buf.append("--")
            i += 2
            continue
        if sql.startswith("/*", i):
            in_block_comment = True
            buf.append("/*")
            i += 2
            continue
        if sql[i] == "'":
            in_single = True
            buf.append(sql[i])
            i += 1
            continue
        if sql[i] == '"':
            in_double = True
            buf.append(sql[i])
            i += 1
            continue
        if sql[i] == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[i:])
            if match:
                dollar_tag = match.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue
            buf.append(sql[i])
            i += 1
            continue
        if sql[i] == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue
        buf.append(sql[i])
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def audit(
    path: Path, vocabulary: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    counts: Counter[str] = Counter()
    flags: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    recipes: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    ids: set[str] = set()
    hashes: set[str] = set()
    mapped = total_ingredients = 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if not REQUIRED.issubset(reader.fieldnames or []):
            raise ValueError("CSV is missing required columns")
        for number, raw in enumerate(reader, start=1):
            counts["total_rows"] += 1
            if None in raw or any(v is None for v in raw.values()):
                raise ValueError(f"CSV column count mismatch at record {number}")
            populated = {k: v for k, v in raw.items() if v.strip()}
            if not populated:
                counts["empty_rows"] += 1
                continue
            if set(populated) == {"Barcode"} and populated["Barcode"].strip() == "()":
                counts["placeholder_rows"] += 1
                continue
            counts["candidate_rows"] += 1
            missing.update(k for k in REQUIRED if raw[k].strip() in {"", "NA", "NULL"})
            try:
                recipe = normalize(raw, vocabulary)
                if recipe["source_id"] in ids:
                    raise ValueError("duplicate_id")
            except ValueError as exc:
                reason = str(exc)
                counts["rejected_rows"] += 1
                counts[f"rejected:{reason}"] += 1
                quarantine.append({"row_number": number, "reason": reason, "raw": raw})
                continue
            ids.add(recipe["source_id"])
            if recipe["content_hash"] in hashes:
                recipe["flags"].append("duplicate_content")
                counts["duplicate_content_rows"] += 1
            hashes.add(recipe["content_hash"])
            recipe["row_number"] = number
            recipes.append(recipe)
            counts["accepted_rows"] += 1
            counts["flagged_rows"] += bool(recipe["flags"])
            # Defective = real quality issues only; presence metadata
            # (images_present, nutrition_present, ...) never counts.
            counts["defective_rows"] += is_defective(recipe.get("quality_issues", []))
            flags.update(recipe["flags"])
            for available_key, present in _availability_counts(recipe).items():
                if present:
                    counts[f"available:{available_key}"] += 1
            total_ingredients += len(recipe["ingredients"])
            mapped += sum(i["epicure_id"] is not None for i in recipe["ingredients"])
    return (
        recipes,
        quarantine,
        {
            "counts": dict(counts),
            "flags": dict(flags),
            "missing_fields": dict(missing),
            "ingredient_count": total_ingredients,
            "mapped_ingredients": mapped,
            "mapping_coverage": mapped / total_ingredients if total_ingredients else 0,
            "duplicate_content_rate": counts["duplicate_content_rows"] / max(len(recipes), 1),
            "missing_field_rates": {
                k: v / max(counts["candidate_rows"], 1) for k, v in missing.items()
            },
        },
    )


def _migration_required_extension(migration: Path) -> str | None:
    """Return the extension named by a ``requires-extension:`` header, if any."""
    for line in migration.read_text(encoding="utf-8").splitlines()[:10]:
        stripped = line.strip().lstrip("-# ").strip()
        if stripped.lower().startswith("requires-extension:"):
            return stripped.split(":", 1)[1].strip()
    return None


def _extension_available(conn: Any, extension: str) -> bool:
    """True when ``extension`` is installed or installable on this database."""
    try:
        row = conn.execute(
            text(
                "SELECT 1 FROM pg_available_extensions WHERE name=:name "
                "UNION SELECT 1 FROM pg_extension WHERE extname=:name LIMIT 1"
            ),
            {"name": extension},
        ).scalar_one_or_none()
    except Exception:
        return False
    return row is not None


def apply_migrations(conn: Any, listener: Callable[[str], None] | None = None) -> list[str]:
    """Apply pending versioned migrations with checksum pinning. Returns applied versions.

    Vector-tagged migrations (``requires-extension: vector`` header, e.g.
    ``004``) are skipped — never partially applied — when the extension is
    unavailable on stock ``postgres:17``. Full-text-only operation is
    preserved; activation requires the pgvector image override. Skipped
    migrations stay pending and are applied on a pgvector-enabled database.
    Each skip is reported through ``listener`` (called with one human-
    readable reason string per skipped migration) when provided, so callers
    such as the migration-only CLI can surface the reason.
    """
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS recipe_schema_migrations "
            "(version text PRIMARY KEY, checksum text NOT NULL)"
        )
    )
    applied: list[str] = []
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = migration.name.split("_", 1)[0]
        digest = hashlib.sha256(migration.read_bytes()).hexdigest()
        previous = conn.execute(
            text("SELECT checksum FROM recipe_schema_migrations WHERE version=:v"),
            {"v": version},
        ).scalar_one_or_none()
        if previous is None:
            required = _migration_required_extension(migration)
            if required is not None and not _extension_available(conn, required):
                if listener is not None:
                    listener(
                        f"skipped {version} ({migration.name}): requires extension "
                        f"'{required}', unavailable on this database; "
                        "full-text-only operation preserved"
                    )
                continue
            for statement in split_sql_statements(migration.read_text()):
                conn.execute(text(statement))
            conn.execute(
                text("INSERT INTO recipe_schema_migrations VALUES (:version, :checksum)"),
                {"version": version, "checksum": digest},
            )
            applied.append(version)
        elif previous != digest:
            raise ValueError(f"Applied migration {version} changed; create a new migration")
    return applied


def pending_vector_migrations(conn: Any) -> list[str]:
    """Versions skipped for a missing extension (informational, no writes)."""
    pending: list[str] = []
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = migration.name.split("_", 1)[0]
        try:
            exists = conn.execute(
                text("SELECT 1 FROM recipe_schema_migrations WHERE version=:v"),
                {"v": version},
            ).scalar_one_or_none()
        except Exception:
            return []
        if exists is not None:
            continue
        required = _migration_required_extension(migration)
        if required is not None and not _extension_available(conn, required):
            pending.append(version)
    return pending


def persist(
    engine: Engine,
    recipes: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    provenance: dict[str, Any],
    report: dict[str, Any],
    apply_schema: bool,
) -> None:
    """DDL, report, quarantine and snapshot replacement commit atomically.

    CSV audit streams row-by-row; only accepted + quarantined records are held.
    Empty/barcode placeholders are counted and skipped, so a 1M-row source with
    ~1k candidates stays bounded in memory. DB inserts run inside one
    transaction in 500-row batches.
    """
    with engine.begin() as conn:
        # Serialize importer/migration invocations for this corpus.
        conn.execute(text("SELECT pg_advisory_xact_lock(73190421)"))
        if apply_schema:
            apply_migrations(conn)
        conn.execute(
            text("""
            INSERT INTO recipe_imports
                (id, dataset_id, revision, checksum, normalizer_version,
                 vocabulary_checksum, dataset_url, report)
            VALUES (:id, :dataset_id, :revision, :checksum, :normalizer_version,
                    :vocabulary_checksum, :dataset_url, CAST(:report AS jsonb))
            ON CONFLICT (id) DO UPDATE SET report=excluded.report,
                vocabulary_checksum=excluded.vocabulary_checksum,
                dataset_url=excluded.dataset_url
        """),
            {**provenance, "report": json.dumps(report)},
        )
        # Current dataset snapshot only: remove stale recipes from older revisions.
        # Other datasets and historical import reports remain untouched.
        conn.execute(text("DELETE FROM recipes WHERE dataset_id=:dataset_id"), provenance)
        conn.execute(text("DELETE FROM recipe_quarantine WHERE import_id=:id"), provenance)
        recipe_params = []
        for recipe in recipes:
            document = {
                **recipe,
                "provenance": {
                    **provenance,
                    "search_document_version": SEARCH_DOCUMENT_VERSION,
                },
            }
            recipe_params.append(
                {
                    "dataset_id": provenance["dataset_id"],
                    "source_id": recipe["source_id"],
                    "id": provenance["id"],
                    "title": recipe["title"],
                    "minutes": recipe["durations_minutes"]["TotalTime"],
                    "servings": recipe["servings"],
                    "names": [i.get("canonical") or i.get("name") for i in recipe["ingredients"]],
                    "document": json.dumps(document),
                    "search_text": render_from_recipe(recipe),
                }
            )
        has_search_version = (
            conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='recipes' AND column_name='search_document_version'"
                )
            ).scalar_one_or_none()
            is not None
        )
        if has_search_version:
            recipe_stmt = text("""
                INSERT INTO recipes (dataset_id, source_id, import_id, title,
                    total_minutes, servings, ingredient_names, document,
                    search_text, search_document_version)
                VALUES (:dataset_id, :source_id, :id, :title, :minutes, :servings,
                    :names, CAST(:document AS jsonb), :search_text, :search_version)
            """)
            for param in recipe_params:
                param["search_version"] = SEARCH_DOCUMENT_VERSION
        else:
            recipe_stmt = text("""
                INSERT INTO recipes (dataset_id, source_id, import_id, title,
                    total_minutes, servings, ingredient_names, document, search_text)
                VALUES (:dataset_id, :source_id, :id, :title, :minutes, :servings,
                    :names, CAST(:document AS jsonb), :search_text)
            """)
        for start in range(0, len(recipe_params), 500):
            conn.execute(recipe_stmt, recipe_params[start : start + 500])
        quarantine_params = [
            {
                "id": provenance["id"],
                "row_number": row["row_number"],
                "reason": row["reason"],
                "raw": json.dumps(row["raw"]),
            }
            for row in rejected
        ]
        if quarantine_params:
            quarantine_stmt = text("""
                INSERT INTO recipe_quarantine (import_id, row_number, reason, raw)
                VALUES (:id, :row_number, :reason, CAST(:raw AS jsonb))
            """)
            for start in range(0, len(quarantine_params), 500):
                conn.execute(quarantine_stmt, quarantine_params[start : start + 500])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, help="Use a local copy of the pinned CSV")
    parser.add_argument(
        "--vocab", type=Path, help="Optional local Epicure vocab.json; exact matches only"
    )
    parser.add_argument("--output", type=Path, default=Path("data/recipe-import"))
    parser.add_argument(
        "--write", action="store_true", help="Commit the reviewed snapshot to Postgres"
    )
    parser.add_argument(
        "--apply-schema", action="store_true", help="Apply versioned schema with --write"
    )
    args = parser.parse_args()
    if args.apply_schema and not args.write:
        parser.error("--apply-schema requires --write")
    settings = Settings()
    path = args.csv or Path(
        hf_hub_download(
            DATASET,
            "recipes.csv",
            repo_type="dataset",
            revision=REVISION,
            token=settings.hf_token.get_secret_value() or False,
            cache_dir=str(Path(settings.hf_home) / "hub"),
        )
    )
    checksum = sha256_file(path)
    if checksum != CHECKSUM:
        raise ValueError("Dataset checksum mismatch; review source and update pin before importing")
    vocab = json.loads(args.vocab.read_text()) if args.vocab else {}
    if not isinstance(vocab, dict) or not all(isinstance(k, str) for k in vocab):
        raise ValueError("Expected Epicure token-to-index vocabulary object")
    vocab_hash = hashlib.sha256(json.dumps(vocab, sort_keys=True).encode()).hexdigest()
    identity = f"{DATASET}:{REVISION}:{checksum}:{VERSION}:{vocab_hash}"
    provenance = {
        "id": hashlib.sha256(identity.encode()).hexdigest(),
        "dataset_id": DATASET,
        "revision": REVISION,
        "checksum": checksum,
        "normalizer_version": VERSION,
        "vocabulary_checksum": vocab_hash,
        "dataset_url": f"https://huggingface.co/datasets/{DATASET}/tree/{REVISION}",
    }
    recipes, rejected, quality = audit(path, set(vocab))
    if not recipes:
        raise ValueError("No usable recipes; refusing empty snapshot")
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"provenance": provenance, **quality, "database_written": False}
    for name, records in (("normalized.jsonl", recipes), ("quarantine.jsonl", rejected)):
        with (args.output / name).open("w") as output:
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
    report_path = args.output / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    if args.write:
        engine = create_db_engine(settings)
        try:
            persist(
                engine,
                recipes,
                rejected,
                provenance,
                {**report, "database_written": True},
                args.apply_schema,
            )
        finally:
            engine.dispose()
        report["database_written"] = True
        report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
