#!/usr/bin/env python3
"""Load normalized technique docs + chunks into a target database.

Reads ``data/technique-corpus/<doc_id>.txt`` + ``<doc_id>.meta.json``
(only ids with ``status: ingested`` in
``evals/technique_corpus/manifest.json``), chunks with
``embeddings/technique_rendering.py``, and upserts
``technique_documents`` / ``technique_chunks`` (migration 006), filling
``search_vector`` via ``to_tsvector('english', ...)``.

Identity guard like ``scripts/migrate.py``: ``--expect-db-name`` and
``--expect-db-host`` are required (unless ``--dry-run``). Idempotent on
``sha256_normalized``: a doc whose stored hash matches is skipped
entirely; otherwise its chunks are atomically replaced (delete +
insert in one transaction). Never touches 001-005 data. ``--dry-run``
performs zero writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from culinary_copilot.embeddings.technique_rendering import (  # noqa: E402
    chunk_technique_document,
)

DEFAULT_CORPUS_DIR = REPO_ROOT / "data" / "technique-corpus"
DEFAULT_MANIFEST = REPO_ROOT / "evals" / "technique_corpus" / "manifest.json"


def _check_expectations(url: str, expect_name: str, expect_host: str) -> str:
    name = urlparse(url).path.rsplit("/", 1)[-1]
    host = urlparse(url).hostname or ""
    problems: list[str] = []
    if name != expect_name:
        problems.append(f"database name {name!r} != expected {expect_name!r}")
    if host != expect_host:
        problems.append(f"database host {host!r} != expected {expect_host!r}")
    return "; ".join(problems)


def _manifest_docs(manifest_path: Path) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [
        doc
        for doc in manifest["docs"].values()
        if isinstance(doc, dict) and doc.get("status") == "ingested"
    ]


def plan_load(corpus_dir: Path, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build load jobs (doc row + chunks); pure function for tests."""
    jobs: list[dict[str, object]] = []
    for doc in sorted(docs, key=lambda d: str(d["doc_id"])):
        doc_id = str(doc["doc_id"])
        text_path = corpus_dir / f"{doc_id}.txt"
        normalized = text_path.read_text(encoding="utf-8")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if digest != str(doc.get("sha256_normalized")):
            raise ValueError(
                f"{doc_id}: normalized text hash mismatch "
                "(corpus changed after the manifest freeze?)"
            )
        jobs.append(
            {
                "doc": doc,
                "normalized": normalized,
                "chunks": chunk_technique_document(normalized),
            }
        )
    return jobs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--expect-db-name", default="")
    parser.add_argument("--expect-db-host", default="")
    parser.add_argument("--corpus-dir", default=str(DEFAULT_CORPUS_DIR))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    docs = _manifest_docs(Path(args.manifest))
    jobs = plan_load(Path(args.corpus_dir), docs)
    total_chunks = sum(len(job["chunks"]) for job in jobs)
    print(f"docs: {len(jobs)}; chunks: {total_chunks}")
    if args.dry_run:
        print("dry-run: zero writes.")
        return 0
    from culinary_copilot.config import Settings

    db_url = args.database_url or Settings().database_url.get_secret_value()
    if not args.expect_db_name or not args.expect_db_host:
        print(
            "error: --expect-db-name and --expect-db-host are required (identity pre-check).",
            file=sys.stderr,
        )
        return 2
    problem = _check_expectations(db_url, args.expect_db_name, args.expect_db_host)
    if problem:
        print(f"error: refusing: {problem}", file=sys.stderr)
        return 2

    engine = create_engine(db_url)
    loaded = 0
    skipped = 0
    with engine.begin() as conn:
        for job in jobs:
            doc = job["doc"]
            assert isinstance(doc, dict)
            current = conn.execute(
                text("SELECT sha256_normalized FROM technique_documents WHERE doc_id=:doc_id"),
                {"doc_id": str(doc["doc_id"])},
            ).scalar_one_or_none()
            if current == str(doc["sha256_normalized"]):
                skipped += 1
                continue
            conn.execute(
                text(
                    "INSERT INTO technique_documents (doc_id, title, final_title, "
                    "topic, url, publisher, licence, licence_url, "
                    "attribution_text, retrieval_date_utc, revision_id, "
                    "sha256_raw, sha256_normalized, words, bytes) "
                    "VALUES (:doc_id, :title, :final_title, :topic, :url, "
                    ":publisher, :licence, :licence_url, :attribution_text, "
                    ":retrieval_date_utc, :revision_id, :sha256_raw, "
                    ":sha256_normalized, :words, :bytes) "
                    "ON CONFLICT (doc_id) DO UPDATE SET title=excluded.title, "
                    "final_title=excluded.final_title, topic=excluded.topic, "
                    "url=excluded.url, publisher=excluded.publisher, "
                    "licence=excluded.licence, licence_url=excluded.licence_url, "
                    "attribution_text=excluded.attribution_text, "
                    "retrieval_date_utc=excluded.retrieval_date_utc, "
                    "revision_id=excluded.revision_id, sha256_raw=excluded.sha256_raw, "
                    "sha256_normalized=excluded.sha256_normalized, "
                    "words=excluded.words, bytes=excluded.bytes"
                ),
                {
                    "doc_id": str(doc["doc_id"]),
                    "title": str(doc["title"]),
                    "final_title": str(doc.get("final_title") or ""),
                    "topic": str(doc.get("topic") or ""),
                    "url": str(doc["url"]),
                    "publisher": str(doc["publisher"]),
                    "licence": str(doc["licence"]),
                    "licence_url": str(doc["licence_url"]),
                    "attribution_text": str(doc["attribution_text"]),
                    "retrieval_date_utc": str(doc["retrieval_date_utc"]),
                    "revision_id": str(doc["revision_id"]),
                    "sha256_raw": str(doc["sha256_raw"]),
                    "sha256_normalized": str(doc["sha256_normalized"]),
                    "words": int(doc["words"]),
                    "bytes": int(doc["bytes"]),
                },
            )
            conn.execute(
                text("DELETE FROM technique_chunks WHERE doc_id=:doc_id"),
                {"doc_id": str(doc["doc_id"])},
            )
            chunks = job["chunks"]
            assert isinstance(chunks, list)
            for chunk in chunks:
                assert isinstance(chunk, dict)
                conn.execute(
                    text(
                        "INSERT INTO technique_chunks (doc_id, chunk_id, section, "
                        "chunk_text, search_vector) VALUES (:doc_id, :chunk_id, "
                        ":section, :chunk_text, "
                        "to_tsvector('english', :section || ' ' || :chunk_text))"
                    ),
                    {
                        "doc_id": str(doc["doc_id"]),
                        "chunk_id": int(chunk["chunk_id"]),
                        "section": str(chunk["section"]),
                        "chunk_text": str(chunk["text"]),
                    },
                )
            loaded += 1
    print(f"loaded {loaded} doc(s), skipped {skipped} unchanged doc(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
