"""H5 summary-layout cleanup (one-off; approved at Checkpoint D, 2026-10-08).

Removes the odunola/foodie recipes loaded from the "summary" source
layout (adapter v5 refuses it, routing v3 quarantines it) and records the
in-scope source rows in ``recipe_quarantine`` under a new import id, in
ONE transaction. Recipe embeddings go with their recipes (cascading FK).
No model calls, no new embeddings.

Commands (run from the repo root):

    manifest  Read-only. Freezes the identities and fingerprints the apply
              step checks: the condemned recipes, the quarantine rows from
              the prepared run directory, the expected counts before and
              after, and fingerprints of everything that must not change.
    apply     One transaction. First run: checks the database matches the
              manifest, inserts the import row and the quarantine rows,
              deletes the condemned recipes by identity, checks the end
              state, commits. Rerun: finds the end state already in place,
              verifies it and writes nothing. Any other state aborts.
    verify    Read-only end-state check.

Every database is named explicitly with --database-url; nothing is read
from .env for the target. ``--target rehearsal`` refuses the application
database (same server and database as ``Settings().database_url``) and
requires a database name starting with ``culinary_rehearsal``.
``--target application`` requires the application identity, the database
name repeated in --confirm-application, and a backup file whose sha256
matches --backup-sha256. Every check raises, which rolls the transaction
back; comments never stand in for a check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from culinary_copilot.config import Settings
from culinary_copilot.recipes.llm_batch import upsert_quarantine_rows

MANIFEST_VERSION = "h5-summary-cleanup-manifest-v1"
DATASET = "odunola/foodie"
REASON = "summary_layout_missing_title"
REHEARSAL_PREFIX = "culinary_rehearsal"
LOCK_KEY = 0x48355F53  # pg_advisory_xact_lock key for this cleanup ("H5_S")


class CleanupError(RuntimeError):
    """A check failed; the transaction is rolled back."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().split("\n") if line.strip()]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CleanupError(message)


# ------------------------------------------------------------- identity ---


def db_identity(conn: Any) -> dict[str, Any]:
    """Server address, port and database name of a connection."""
    row = conn.execute(
        text("SELECT host(inet_server_addr()), inet_server_port(), current_database()")
    ).one()
    return {"host": row[0], "port": row[1], "database": row[2]}


def application_identity() -> dict[str, Any]:
    """Identity of the configured application database, read-only."""
    engine = create_engine(
        Settings().database_url.get_secret_value(),
        connect_args={"options": "-c default_transaction_read_only=on"},
    )
    try:
        with engine.connect() as conn:
            return db_identity(conn)
    finally:
        engine.dispose()


def check_target(
    target: str,
    identity: dict[str, Any],
    app_identity: dict[str, Any],
    *,
    confirm_application: str | None = None,
) -> None:
    """Refuse a target that does not match the declared intent."""
    same_as_app = identity == app_identity
    if target == "rehearsal":
        _require(not same_as_app, "rehearsal target is the application database; refusing")
        _require(
            str(identity["database"]).startswith(REHEARSAL_PREFIX),
            f"rehearsal database name must start with {REHEARSAL_PREFIX!r}",
        )
    elif target == "application":
        _require(same_as_app, "application target does not match Settings().database_url")
        _require(
            confirm_application == identity["database"],
            "--confirm-application must repeat the application database name",
        )
    else:
        raise CleanupError(f"unknown target {target!r}")


# --------------------------------------------------------- fingerprints ---


def _keys(condemned: list[dict[str, Any]]) -> list[str]:
    return [f"{c['dataset_id']}|{c['source_id']}" for c in condemned]


def counts(conn: Any) -> dict[str, int]:
    def one(sql: str) -> int:
        return int(conn.execute(text(sql)).scalar() or 0)

    has_embeddings = bool(
        conn.execute(text("SELECT to_regclass('recipe_embeddings') IS NOT NULL")).scalar()
    )
    return {
        "recipes": one("SELECT count(*) FROM recipes"),
        "quarantine": one("SELECT count(*) FROM recipe_quarantine"),
        "imports": one("SELECT count(*) FROM recipe_imports"),
        "summary_titles": one(
            f"SELECT count(*) FROM recipes WHERE dataset_id = '{DATASET}' AND title = 'summary'"
        ),
        "foodie_embeddings": (
            one(f"SELECT count(*) FROM recipe_embeddings WHERE dataset_id = '{DATASET}'")
            if has_embeddings
            else 0
        ),
        "orphan_embeddings": (
            one(
                "SELECT count(*) FROM recipe_embeddings e WHERE NOT EXISTS (SELECT 1 "
                "FROM recipes r WHERE r.dataset_id = e.dataset_id AND r.source_id = e.source_id)"
            )
            if has_embeddings
            else 0
        ),
    }


def survivors_fingerprint(conn: Any, condemned_keys: list[str]) -> str:
    """md5 over every recipe row outside the condemned set, in key order."""
    return str(
        conn.execute(
            text("""
            SELECT coalesce(md5(string_agg(md5(row(dataset_id, source_id, import_id, title,
                total_minutes, servings, ingredient_names, document, search_text)::text), ''
                ORDER BY dataset_id, source_id)), '')
            FROM recipes
            WHERE (dataset_id || '|' || source_id) <> ALL(CAST(:keys AS text[]))
            """),
            {"keys": condemned_keys},
        ).scalar()
    )


def quarantine_fingerprint(conn: Any, exclude_import_id: str) -> str:
    """md5 over quarantine rows of every other import (must never change)."""
    return str(
        conn.execute(
            text("""
            SELECT coalesce(md5(string_agg(md5(q::text), '' ORDER BY import_id, row_number)), '')
            FROM recipe_quarantine q WHERE import_id <> :import_id
            """),
            {"import_id": exclude_import_id},
        ).scalar()
    )


def imports_fingerprint(conn: Any, exclude_import_id: str) -> str:
    return str(
        conn.execute(
            text("""
            SELECT coalesce(md5(string_agg(md5(row(id, dataset_id, revision, checksum,
                normalizer_version, vocabulary_checksum, dataset_url, report)::text), ''
                ORDER BY id)), '')
            FROM recipe_imports WHERE id <> :import_id
            """),
            {"import_id": exclude_import_id},
        ).scalar()
    )


# ------------------------------------------------------------- manifest ---


def quarantine_entries(run_dir: Path) -> list[dict[str, Any]]:
    """Quarantine rows from a finalized run, in cmd_load's entry shape."""
    rows = _read_jsonl(run_dir / "final-quarantine.jsonl")
    return [
        {
            "source_id": row["source_id"],
            "row_number": int(row["row_number"]),
            "status": "quarantined",
            "reason": row.get("reason", "quarantine"),
            "verdict": "quarantined",
            "problems": [],
            "raw": row.get("raw", {}),
        }
        for row in rows
    ]


def build_manifest(conn: Any, run_dir: Path, import_id: str) -> dict[str, Any]:
    run_manifest = json.loads((run_dir / "manifest.json").read_text())
    _require(
        str(run_manifest.get("adapter_version")) == "5"
        and str(run_manifest.get("routing_version")) == "3",
        "run directory was not prepared with adapter v5 / routing v3",
    )
    entries = quarantine_entries(run_dir)
    _require(bool(entries), "run directory has no quarantine rows")
    _require(
        all(e["reason"] == REASON for e in entries),
        f"every quarantine row must carry {REASON!r}",
    )
    _require(
        not _read_jsonl(run_dir / "ready_to_load.jsonl"),
        "run directory has loadable records; this cleanup loads none",
    )
    row_numbers = {e["row_number"] for e in entries}
    condemned = [
        {
            "dataset_id": r[0],
            "source_id": r[1],
            "row_number": int(r[1].split("-")[1]),
            "document_md5": r[2],
        }
        for r in conn.execute(
            text(
                "SELECT dataset_id, source_id, md5(document::text) FROM recipes "
                f"WHERE dataset_id = '{DATASET}' AND title = 'summary' ORDER BY source_id"
            )
        ).all()
    ]
    missing = [c["source_id"] for c in condemned if c["row_number"] not in row_numbers]
    _require(not missing, f"condemned recipes without a quarantine row: {missing[:5]}")
    keys = _keys(condemned)
    before = counts(conn)
    embedded = (
        int(
            conn.execute(
                text(
                    "SELECT count(*) FROM recipe_embeddings "
                    "WHERE (dataset_id || '|' || source_id) = ANY(CAST(:keys AS text[]))"
                ),
                {"keys": keys},
            ).scalar()
            or 0
        )
        if before["foodie_embeddings"] or before["orphan_embeddings"]
        else 0
    )
    after = dict(before)
    after.update(
        recipes=before["recipes"] - len(condemned),
        quarantine=before["quarantine"] + len(entries),
        imports=before["imports"] + 1,
        summary_titles=0,
        foodie_embeddings=before["foodie_embeddings"] - embedded,
        orphan_embeddings=0,
    )
    return {
        "version": MANIFEST_VERSION,
        "import_id": import_id,
        "run_dir": str(run_dir),
        "run_manifest_sha256": _sha256_file(run_dir / "manifest.json"),
        "final_quarantine_sha256": _sha256_file(run_dir / "final-quarantine.jsonl"),
        "source_database": db_identity(conn),
        "condemned": condemned,
        "quarantine_rows": [
            {"row_number": e["row_number"], "source_id": e["source_id"], "reason": e["reason"]}
            for e in entries
        ],
        "expected_before": before,
        "expected_after": after,
        "survivors_fingerprint": survivors_fingerprint(conn, keys),
        "other_quarantine_fingerprint": quarantine_fingerprint(conn, import_id),
        "other_imports_fingerprint": imports_fingerprint(conn, import_id),
    }


def load_manifest(path: Path, run_dir: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(path.read_text())
    _require(manifest.get("version") == MANIFEST_VERSION, "unknown manifest version")
    _require(
        _sha256_file(run_dir / "manifest.json") == manifest["run_manifest_sha256"]
        and _sha256_file(run_dir / "final-quarantine.jsonl") == manifest["final_quarantine_sha256"],
        "run directory changed since the manifest was built",
    )
    return manifest


# ----------------------------------------------------------- end state ---


def check_end_state(conn: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    """Raise unless the database is exactly in the manifest's end state."""
    import_id = manifest["import_id"]
    keys = _keys(manifest["condemned"])
    now = counts(conn)
    _require(
        now == manifest["expected_after"],
        f"counts {now} != expected {manifest['expected_after']}",
    )
    present = int(
        conn.execute(
            text(
                "SELECT count(*) FROM recipes "
                "WHERE (dataset_id || '|' || source_id) = ANY(CAST(:keys AS text[]))"
            ),
            {"keys": keys},
        ).scalar()
        or 0
    )
    _require(present == 0, f"{present} condemned recipes still present")
    rows = {
        (int(r[0]), r[1], r[2])
        for r in conn.execute(
            text(
                "SELECT row_number, source_id, reason FROM recipe_quarantine "
                "WHERE import_id = :import_id"
            ),
            {"import_id": import_id},
        ).all()
    }
    wanted = {(q["row_number"], q["source_id"], q["reason"]) for q in manifest["quarantine_rows"]}
    _require(rows == wanted, "quarantine rows under the new import id differ from the manifest")
    _require(
        survivors_fingerprint(conn, keys) == manifest["survivors_fingerprint"],
        "a surviving recipe changed",
    )
    _require(
        quarantine_fingerprint(conn, import_id) == manifest["other_quarantine_fingerprint"],
        "quarantine rows of other imports changed",
    )
    _require(
        imports_fingerprint(conn, import_id) == manifest["other_imports_fingerprint"],
        "other import records changed",
    )
    old_rows = {q["row_number"] for q in manifest["quarantine_rows"]}
    kept_history = int(
        conn.execute(
            text(
                "SELECT count(*) FROM recipe_quarantine WHERE import_id <> :import_id "
                "AND row_number = ANY(CAST(:rows AS int[]))"
            ),
            {"import_id": import_id, "rows": sorted(old_rows)},
        ).scalar()
        or 0
    )
    return {
        "counts": now,
        "unique_source_rows_quarantined": len(wanted),
        "condemned_removed": len(keys),
        "older_quarantine_events_kept_for_same_rows": kept_history,
    }


# ---------------------------------------------------------------- apply ---


def apply(conn: Any, manifest: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """First run or verified rerun, inside the caller's transaction."""
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
    import_id = manifest["import_id"]
    keys = _keys(manifest["condemned"])
    present = int(
        conn.execute(
            text(
                "SELECT count(*) FROM recipes "
                "WHERE (dataset_id || '|' || source_id) = ANY(CAST(:keys AS text[]))"
            ),
            {"keys": keys},
        ).scalar()
        or 0
    )
    new_rows = int(
        conn.execute(
            text("SELECT count(*) FROM recipe_quarantine WHERE import_id = :import_id"),
            {"import_id": import_id},
        ).scalar()
        or 0
    )
    has_import = bool(
        conn.execute(
            text("SELECT count(*) FROM recipe_imports WHERE id = :import_id"),
            {"import_id": import_id},
        ).scalar()
    )
    if present == 0 and new_rows == len(manifest["quarantine_rows"]) and has_import:
        result = check_end_state(conn, manifest)
        return {"state": "already_applied", "writes": 0, **result}
    _require(
        present == len(keys) and new_rows == 0 and not has_import,
        f"unexpected state: {present} condemned present, {new_rows} new quarantine rows, "
        f"import row {'present' if has_import else 'absent'}; refusing",
    )
    before = counts(conn)
    _require(
        before == manifest["expected_before"],
        f"counts {before} != manifest {manifest['expected_before']}",
    )
    _require(
        survivors_fingerprint(conn, keys) == manifest["survivors_fingerprint"],
        "surviving recipes differ from the manifest",
    )
    document_md5 = {
        f"{r[0]}|{r[1]}": r[2]
        for r in conn.execute(
            text(
                "SELECT dataset_id, source_id, md5(document::text) FROM recipes "
                "WHERE (dataset_id || '|' || source_id) = ANY(CAST(:keys AS text[]))"
            ),
            {"keys": keys},
        ).all()
    }
    _require(
        all(
            document_md5.get(f"{c['dataset_id']}|{c['source_id']}") == c["document_md5"]
            for c in manifest["condemned"]
        ),
        "a condemned recipe's document differs from the manifest",
    )
    run_manifest = json.loads((run_dir / "manifest.json").read_text())
    conn.execute(
        text("""
        INSERT INTO recipe_imports (id, dataset_id, revision, checksum, normalizer_version,
            vocabulary_checksum, dataset_url, report)
        VALUES (:id, :dataset_id, :revision, :checksum, '3', 'foodie-hybrid', :url,
            CAST(:report AS jsonb))
        """),
        {
            "id": import_id,
            "dataset_id": DATASET,
            "revision": run_manifest.get("revision", ""),
            "checksum": run_manifest.get("file_sha256", ""),
            "url": f"https://huggingface.co/datasets/{DATASET}",
            "report": json.dumps(
                {
                    "h5_summary_cleanup": True,
                    "run_manifest_sha256": manifest["run_manifest_sha256"],
                    "removed_recipes": len(keys),
                    "quarantined_rows": len(manifest["quarantine_rows"]),
                }
            ),
        },
    )
    entries = quarantine_entries(run_dir)
    _require(
        {(e["row_number"], e["source_id"], e["reason"]) for e in entries}
        == {(q["row_number"], q["source_id"], q["reason"]) for q in manifest["quarantine_rows"]},
        "run quarantine rows differ from the manifest",
    )
    upsert_quarantine_rows(conn, import_id, entries)
    deleted = conn.execute(
        text(
            "DELETE FROM recipes "
            "WHERE (dataset_id || '|' || source_id) = ANY(CAST(:keys AS text[]))"
        ),
        {"keys": keys},
    ).rowcount
    _require(deleted == len(keys), f"deleted {deleted} recipes, expected {len(keys)}")
    result = check_end_state(conn, manifest)
    return {"state": "first_run", "writes": deleted + len(entries) + 1, **result}


# ------------------------------------------------------------------ CLI ---


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("manifest", "apply", "verify"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--database-url", required=True)
        cmd.add_argument("--run-dir", required=True, type=Path)
        cmd.add_argument("--manifest", required=True, type=Path)
    sub.choices["manifest"].add_argument("--import-id", required=True)
    sub.choices["apply"].add_argument(
        "--target", required=True, choices=("rehearsal", "application")
    )
    sub.choices["apply"].add_argument("--confirm-application", default=None)
    sub.choices["apply"].add_argument("--backup", type=Path, default=None)
    sub.choices["apply"].add_argument("--backup-sha256", default=None)
    sub.choices["apply"].add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    read_only = args.command in ("manifest", "verify")
    engine = create_engine(
        args.database_url,
        connect_args={"options": "-c default_transaction_read_only=on"} if read_only else {},
    )
    try:
        if args.command == "manifest":
            with engine.connect() as conn:
                manifest = build_manifest(conn, args.run_dir, args.import_id)
            args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
            print(
                f"manifest: {len(manifest['condemned'])} condemned, "
                f"{len(manifest['quarantine_rows'])} quarantine rows, "
                f"before {manifest['expected_before']}, after {manifest['expected_after']}"
            )
            return 0
        manifest = load_manifest(args.manifest, args.run_dir)
        if args.command == "verify":
            with engine.connect() as conn:
                print(json.dumps(check_end_state(conn, manifest), indent=2))
            return 0
        with engine.connect() as conn:
            identity = db_identity(conn)
        check_target(
            args.target,
            identity,
            application_identity(),
            confirm_application=args.confirm_application,
        )
        if args.target == "application":
            _require(
                args.backup is not None and args.backup.exists(),
                "application run needs --backup pointing at the verified dump",
            )
            _require(
                _sha256_file(args.backup) == args.backup_sha256,
                "backup sha256 does not match --backup-sha256",
            )
        with engine.begin() as conn:
            result = apply(conn, manifest, args.run_dir)
        report = {"target": args.target, "database": identity, **result}
        print(json.dumps(report, indent=2))
        if args.report is not None:
            args.report.write_text(json.dumps(report, indent=2) + "\n")
        return 0
    except CleanupError as exc:
        print(f"ABORTED (rolled back): {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
