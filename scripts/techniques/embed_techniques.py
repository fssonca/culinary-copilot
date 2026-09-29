#!/usr/bin/env python3
"""Resumable technique-embedding CLI (Milestone 3, Phase 4, offline by default).

Mirrors ``scripts/embeddings/embed.py`` (same ledger, reservation, and
ceiling logic) for technique chunks from migration 006 into migration
007. Token-aware packing only; chunk count alone never bounds a request.
Response vectors map by index and are dimension/numeric-validated
before any write.

Identity per chunk (reusable only on full match)::

    doc_id + chunk_id + model + dimension
    + technique renderer version + technique chunking version
    + embedded-text hash

``--dry-run`` performs zero network calls and zero writes. ``--fake``
writes deterministic fake vectors to a disposable database for
rehearsal (refused on the application database). ``--live`` additionally
requires ``OPENAI_API_KEY``, ``EMBED_BUDGET_USD``, and ``--yes`` and is
NOT authorized by Phase 4 part 2 (paid run needs its own go-ahead).
Default ceiling is the owner-approved $0.01 cap. Tests use ``--fake``
only. Never runs on startup.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

OWNER_CAP_USD = 0.01


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default="data/embeddings-technique/run")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-inputs", type=int, default=64)
    parser.add_argument("--max-tokens-per-request", type=int, default=300_000)
    parser.add_argument("--ceiling-usd", type=float, default=OWNER_CAP_USD)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--expect-db-name", default="")
    parser.add_argument("--expect-db-host", default="")
    return parser.parse_args(argv)


def _db_name(url: str) -> str:
    return urlparse(url).path.rsplit("/", 1)[-1].split("?")[0].strip().strip('"')


def _require_disposable_db(url: str) -> str:
    lowered = url.lower()
    is_app_host = "localhost:5432" in lowered or "127.0.0.1:5432" in lowered
    name = _db_name(url)
    if name == "culinary_copilot" and is_app_host:
        return "refusing --fake on the application database; use a disposable test database"
    if "test" not in name and "disposable" not in name and "check" not in name:
        if ":5544" not in lowered:
            return (
                f"refusing --fake on database '{name}'; use a disposable database "
                "(name must contain 'test', 'disposable', or 'check')"
            )
    return ""


def _load_ledger(run_dir: Path) -> dict[str, Any]:
    ledger_path = run_dir / "ledger.json"
    if ledger_path.is_file():
        ledger = dict(json.loads(ledger_path.read_text(encoding="utf-8")))
        ledger.setdefault("chunks", {})
        ledger.setdefault("reserved_tokens", 0)
        ledger.setdefault("used_tokens_total", 0)
        ledger.setdefault("status", "running")
        return ledger
    return {"chunks": {}, "reserved_tokens": 0, "used_tokens_total": 0, "status": "running"}


def _save_ledger(run_dir: Path, ledger: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "ledger.json").write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except ValueError:
        return False
    return True


def _claim_ledger(ledger: dict[str, Any], *, pid: int) -> str:
    if ledger.get("status") != "running":
        return ""
    owner = ledger.get("run_pid")
    if isinstance(owner, int) and owner != pid and _pid_alive(owner):
        return f"run-dir owned by live pid {owner}; use a separate --run-dir per worker"
    return ""


def _fingerprint(jobs: list[dict[str, Any]], *, model: str, dimension: int) -> str:
    from culinary_copilot.embeddings.technique_rendering import (
        TECHNIQUE_CHUNK_VERSION,
        TECHNIQUE_RENDER_VERSION,
    )

    return hashlib.sha256(
        json.dumps(
            [
                {
                    "doc_id": job["doc_id"],
                    "chunk_id": job["chunk_id"],
                    "text_hash": job["text_hash"],
                    "model": model,
                    "dimension": dimension,
                    "renderer": TECHNIQUE_RENDER_VERSION,
                    "chunking": TECHNIQUE_CHUNK_VERSION,
                }
                for job in jobs
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _pack_jobs(
    jobs: list[dict[str, Any]], *, max_inputs: int, max_tokens: int
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for job in jobs:
        tokens = int(job["est_tokens"])
        if tokens > 8192:
            raise ValueError(f"job exceeds per-input cap: {tokens} tokens")
        if current and (len(current) + 1 > max_inputs or current_tokens + tokens > max_tokens):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(job)
        current_tokens += tokens
    if current:
        batches.append(current)
    return batches


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    from culinary_copilot.config import Settings
    from culinary_copilot.embeddings.provider import (
        FakeEmbeddingProvider,
        OpenAIEmbeddingProvider,
        validate_vectors,
    )
    from culinary_copilot.embeddings.registry import embedding_spec, estimate_cost_usd
    from culinary_copilot.embeddings.rendering import estimate_tokens_bytes
    from culinary_copilot.embeddings.technique_rendering import (
        TECHNIQUE_CHUNK_VERSION,
        TECHNIQUE_RENDER_VERSION,
        embedded_text_hash,
    )

    settings = Settings()
    model = settings.embedding_model
    dimension = settings.embedding_dimension
    spec = embedding_spec(model)
    if spec is None or spec.dimension != dimension:
        print(f"error: unsupported embedding model/dimension {model}/{dimension}", file=sys.stderr)
        return 2
    run_dir = Path(args.run_dir)
    ledger = _load_ledger(run_dir)
    db_url = args.database_url or settings.database_url.get_secret_value()

    if not args.dry_run:
        import os

        guard = _claim_ledger(ledger, pid=os.getpid())
        if guard:
            print(f"error: {guard}", file=sys.stderr)
            return 2
        ledger["run_pid"] = os.getpid()
        _save_ledger(run_dir, ledger)

    if args.fake:
        guard = _require_disposable_db(db_url)
        if guard:
            print(f"error: {guard}", file=sys.stderr)
            return 2
    elif not args.dry_run:
        if not args.expect_db_name or not args.expect_db_host:
            print(
                "error: --expect-db-name and --expect-db-host are required (identity pre-check).",
                file=sys.stderr,
            )
            return 2
        name = _db_name(db_url)
        host = urlparse(db_url).hostname or ""
        if name != args.expect_db_name or host != args.expect_db_host:
            print(
                f"error: refusing: database {host}/{name} != expected "
                f"{args.expect_db_host}/{args.expect_db_name}",
                file=sys.stderr,
            )
            return 2

    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT c.doc_id, c.chunk_id, c.chunk_text, "
                    "e.embedded_text_hash AS have_hash "
                    "FROM technique_chunks c LEFT JOIN technique_embeddings e "
                    "ON e.doc_id=c.doc_id AND e.chunk_id=c.chunk_id "
                    "AND e.model=:model AND e.dimension=:dimension "
                    "AND e.renderer_version=:renderer AND e.chunking_version=:chunking "
                    "ORDER BY c.doc_id, c.chunk_id"
                ),
                {
                    "model": model,
                    "dimension": dimension,
                    "renderer": TECHNIQUE_RENDER_VERSION,
                    "chunking": TECHNIQUE_CHUNK_VERSION,
                },
            )
            .mappings()
            .all()
        )
    chunks = [dict(r) for r in rows]
    if args.limit > 0:
        seen: set[str] = set()
        kept: list[dict[str, Any]] = []
        for row in chunks:
            if row["doc_id"] not in seen:
                if len(seen) >= args.limit:
                    continue
                seen.add(str(row["doc_id"]))
            kept.append(row)
        chunks = kept

    jobs: list[dict[str, Any]] = []
    for row in chunks:
        chunk_text = str(row["chunk_text"])
        text_hash = embedded_text_hash(chunk_text)
        if row.get("have_hash") == text_hash:
            continue
        jobs.append(
            {
                "doc_id": str(row["doc_id"]),
                "chunk_id": int(row["chunk_id"]),
                "text": chunk_text,
                "text_hash": text_hash,
                "est_tokens": estimate_tokens_bytes(chunk_text),
            }
        )

    retries = max(0, settings.embed_max_retries)
    total_tokens = sum(int(job["est_tokens"]) for job in jobs)
    reserved = total_tokens * (retries + 1)
    ledger["reserved_tokens"] = reserved
    cost = estimate_cost_usd(reserved, model)
    cumulative = int(ledger.get("used_tokens_total", 0)) + reserved
    cumulative_cost = estimate_cost_usd(cumulative, model)
    print(f"chunks to embed: {len(jobs)}")
    print(f"reserved tokens (retry x{retries + 1}): {reserved}; estimated cost: {cost} USD")
    print(f"cumulative reserved: {cumulative} tokens ({cumulative_cost} USD)")
    if cost is not None and cost > args.ceiling_usd:
        print(
            f"error: reservation ${cost:.4f} exceeds ceiling ${args.ceiling_usd:.4f}",
            file=sys.stderr,
        )
        _save_ledger(run_dir, ledger)
        return 2
    batches = _pack_jobs(
        jobs,
        max_inputs=min(args.batch_inputs, spec.max_inputs_per_request),
        max_tokens=min(args.max_tokens_per_request, spec.max_total_tokens_per_request),
    )
    print(f"requests planned: {len(batches)} (token-aware packing)")
    _save_ledger(run_dir, ledger)
    if args.dry_run:
        print("dry-run: zero network calls, zero writes.")
        return 0
    if not args.fake and not args.live:
        print("refusing: pass --dry-run (plan only) or --fake (disposable-DB rehearsal).")
        return 2
    if args.live and not (
        args.yes and settings.openai_api_key.get_secret_value() and settings.embed_budget_usd
    ):
        print(
            "error: --live needs a key, budget (EMBED_BUDGET_USD), and --yes; "
            "not authorized by Phase 4 part 2.",
            file=sys.stderr,
        )
        return 2

    if args.live:
        provider: Any = OpenAIEmbeddingProvider(
            model=model,
            dimension=dimension,
            api_key=settings.openai_api_key.get_secret_value(),
            timeout_s=settings.embed_timeout_s,
            max_retries=settings.embed_max_retries,
        )
    else:
        provider = FakeEmbeddingProvider(model=model, dimension=dimension)

    async def _embed_all() -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in batches:
            result = await provider.embed_texts([str(job["text"]) for job in batch])
            if len(result.vectors) != len(batch):
                raise ValueError("provider index mapping mismatch")
            validate_vectors(result.vectors, expected_dimension=dimension, model=model)
            vectors.extend(result.vectors)
        return vectors

    vectors = asyncio.run(_embed_all())
    if len(vectors) != len(jobs):
        print("error: vector/job count mismatch", file=sys.stderr)
        return 1
    run_id = f"tech-{run_dir.name}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO embedding_runs (run_id, model, dimension, renderer_version, "
                "chunking_version, status, reserved_tokens, used_tokens) "
                "VALUES (:run_id, :model, :dimension, :renderer, :chunking, 'running', "
                ":reserved, :used) ON CONFLICT (run_id) DO UPDATE SET "
                "status='running', reserved_tokens=excluded.reserved_tokens, "
                "used_tokens=excluded.used_tokens, updated_at=now()"
            ),
            {
                "run_id": run_id,
                "model": model,
                "dimension": dimension,
                "renderer": TECHNIQUE_RENDER_VERSION,
                "chunking": TECHNIQUE_CHUNK_VERSION,
                "reserved": reserved,
                "used": total_tokens,
            },
        )
        for job, vector in zip(jobs, vectors, strict=True):
            conn.execute(
                text(
                    "INSERT INTO technique_embeddings "
                    "(doc_id, chunk_id, model, dimension, renderer_version, "
                    "chunking_version, embedded_text_hash, embedding) "
                    "VALUES (:d, :c, :model, :dimension, :renderer, :chunking, "
                    ":hash, CAST(:vec AS vector)) "
                    "ON CONFLICT (doc_id, chunk_id, model, dimension, "
                    "renderer_version, chunking_version) "
                    "DO UPDATE SET embedded_text_hash=excluded.embedded_text_hash, "
                    "embedding=excluded.embedding"
                ),
                {
                    "d": str(job["doc_id"]),
                    "c": int(job["chunk_id"]),
                    "model": model,
                    "dimension": dimension,
                    "renderer": TECHNIQUE_RENDER_VERSION,
                    "chunking": TECHNIQUE_CHUNK_VERSION,
                    "hash": str(job["text_hash"]),
                    "vec": "[" + ",".join(repr(float(v)) for v in vector) + "]",
                },
            )
            ledger["chunks"][f"{job['doc_id']}\0{job['chunk_id']}"] = {"committed": True}
        conn.execute(
            text("UPDATE embedding_runs SET status='done', updated_at=now() WHERE run_id=:run_id"),
            {"run_id": run_id},
        )
    ledger["used_tokens_total"] = int(ledger.get("used_tokens_total", 0)) + total_tokens
    ledger["status"] = "done"
    _save_ledger(run_dir, ledger)
    print(f"embedded chunks: {len(jobs)}; used tokens: {total_tokens}; ledger: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
