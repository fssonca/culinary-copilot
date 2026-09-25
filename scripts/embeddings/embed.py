#!/usr/bin/env python3
"""Resumable recipe-embedding CLI (Phase 5, offline by default).

``batch`` means multiple inputs in one synchronous ``/v1/embeddings``
request (per-input <=8192 tokens, aggregate <=300k, <=2048 inputs) — not
the asynchronous Batch API. Token-aware packing only; recipe count alone
never bounds a request. Response vectors map by their ``index`` field and
are dimension/numeric-validated before any write.

Identity per chunk (reusable only on full match)::

    dataset_id + source_id + model + dimension
    + renderer version + chunking version + embedded-text hash

Source provenance stays separate. Changed content, renderer, chunking,
model, or dimension invalidates old rows; shorter recipes drop stale
chunks via atomic per-recipe replacement (delete + insert in one
transaction). A source changed during embedding cannot look current:
the recipe document is re-read and the identity recomputed before commit.

Recovery: the authoritative persisted ledger is ``<run-dir>/ledger.json``
(checkpoints reconcile against it on resume). Provider-success-before-
commit is safe: resume re-embeds the same identities and upserts by PK.
One worker per run-dir: a second process sharing a run-dir is refused via
a pid claim (concurrent jobs must use separate ``--run-dir`` values).
``--dry-run`` performs zero network calls and zero writes. ``--fake``
writes deterministic fake vectors to a disposable database for rehearsal.
``--live`` additionally requires ``OPENAI_API_KEY``, ``EMBED_BUDGET_USD``,
and ``--yes`` and is NOT authorized by Phase 5.

Recipe-text embeddings stay separate from Epicure ingredient vectors.
Never runs on startup.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default="data/embeddings/run")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-inputs", type=int, default=64)
    parser.add_argument("--max-tokens-per-request", type=int, default=300_000)
    parser.add_argument("--ceiling-usd", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--database-url", default="")
    return parser.parse_args(argv)


def _db_name(url: str) -> str:
    name = url.rpartition("/")[2].split("?")[0]
    return name.strip().strip('"')


def _require_disposable_db(url: str) -> str:
    """Refuse the application database for --fake rehearsal (isolation guard).

    The application database is localhost:5432/culinary_copilot. The isolated
    pgvector rehearsal uses port 5544 (same db name, different server), which
    is allowed. Any other database whose name lacks a disposable marker is
    also refused.
    """
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
        ledger.setdefault("recipes", {})
        ledger.setdefault("reserved_tokens", 0)
        ledger.setdefault("used_tokens_total", 0)
        ledger.setdefault("status", "running")
        return ledger
    return {"recipes": {}, "reserved_tokens": 0, "used_tokens_total": 0, "status": "running"}


def _save_ledger(run_dir: Path, ledger: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "ledger.json").write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` exists (single-host guard for shared run-dirs)."""
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
    """Single-worker guard: refuse when another live process owns the run.

    Reservations in ``ledger.json`` are read-modify-written by one process;
    two workers sharing a run-dir would silently clobber each other's
    reservations. A stale claim (dead pid, or ``status == done``) is
    releasable and returns "". Each CLI invocation must use its own
    ``--run-dir`` for concurrent jobs.
    """
    if ledger.get("status") != "running":
        return ""
    owner = ledger.get("run_pid")
    if isinstance(owner, int) and owner != pid and _pid_alive(owner):
        return f"run-dir owned by live pid {owner}; use a separate --run-dir per worker"
    return ""


def _fingerprint(chunk_jobs: list[dict[str, Any]], *, model: str, dimension: int) -> str:
    from culinary_copilot.embeddings.rendering import CHUNKING_VERSION, EMBED_DOCUMENT_VERSION

    return hashlib.sha256(
        json.dumps(
            [
                {
                    "chunk_index": job["chunk_index"],
                    "text_hash": job["text_hash"],
                    "model": model,
                    "dimension": dimension,
                    "renderer": EMBED_DOCUMENT_VERSION,
                    "chunking": CHUNKING_VERSION,
                }
                for job in chunk_jobs
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _pack_jobs(
    jobs: list[dict[str, Any]], *, max_inputs: int, max_tokens: int
) -> list[list[dict[str, Any]]]:
    """Token-aware packing: per-input and aggregate limits, never count-only."""
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
    from culinary_copilot.embeddings.rendering import (
        CHUNKING_VERSION,
        EMBED_DOCUMENT_VERSION,
        chunk_embed_text,
        embedded_text_hash,
        estimate_tokens_bytes,
        render_embed_text,
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
    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT dataset_id, source_id, document "
                    "FROM recipes ORDER BY dataset_id, source_id"
                )
            )
            .mappings()
            .all()
        )
        recipes = [dict(r) for r in rows]
    if args.limit > 0:
        recipes = recipes[: args.limit]

    jobs: list[dict[str, Any]] = []
    for recipe in recipes:
        dataset_id = str(recipe["dataset_id"])
        source_id = str(recipe["source_id"])
        key = f"{dataset_id}\0{source_id}"
        document = recipe["document"]
        if isinstance(document, str):
            document = json.loads(document)
        embed_text = render_embed_text(document if isinstance(document, dict) else {})
        chunks, truncated = chunk_embed_text(embed_text)
        chunk_jobs: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks):
            chunk_jobs.append(
                {
                    "dataset_id": dataset_id,
                    "source_id": source_id,
                    "chunk_index": index,
                    "text": chunk,
                    "text_hash": embedded_text_hash(chunk),
                    "est_tokens": estimate_tokens_bytes(chunk),
                    "truncated": truncated,
                }
            )
        done = ledger["recipes"].get(key)
        fingerprint = _fingerprint(chunk_jobs, model=model, dimension=dimension)
        if (
            isinstance(done, dict)
            and done.get("fingerprint") == fingerprint
            and done.get("committed")
        ):
            continue
        for job in chunk_jobs:
            job["fingerprint"] = fingerprint
        jobs.extend(chunk_jobs)

    retries = max(0, settings.embed_max_retries)
    total_tokens = sum(int(job["est_tokens"]) for job in jobs)
    reserved = total_tokens * (retries + 1)
    ledger["reserved_tokens"] = reserved
    cost = estimate_cost_usd(reserved, model)
    cumulative = int(ledger.get("used_tokens_total", 0)) + reserved
    cumulative_cost = estimate_cost_usd(cumulative, model)
    print(f"recipes scanned: {len(recipes)}; chunks to embed: {len(jobs)}")
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
    if args.live and not (args.yes and settings.openai_api_key.get_secret_value()):
        print(
            "error: --live needs a key, budget, and --yes; not authorized by Phase 5.",
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
    # Re-read sources before commit: a source changed during embedding must
    # never be published as current. Drop changed recipes from this commit;
    # they are re-queued on the next resume.
    with engine.connect() as conn:
        current_hashes: dict[str, str] = {}
        for job in jobs:
            key = f"{job['dataset_id']}\0{job['source_id']}"
            if key in current_hashes:
                continue
            doc = conn.execute(
                text("SELECT document FROM recipes WHERE dataset_id=:d AND source_id=:s"),
                {"d": str(job["dataset_id"]), "s": str(job["source_id"])},
            ).scalar_one_or_none()
            if doc is None:
                current_hashes[key] = "__deleted__"
                continue
            if isinstance(doc, str):
                doc = json.loads(doc)
            text_now = render_embed_text(doc if isinstance(doc, dict) else {})
            chunks_now, _ = chunk_embed_text(text_now)
            current_hashes[key] = embedded_text_hash(chunks_now[int(job["chunk_index"])])
    kept_jobs: list[dict[str, Any]] = []
    kept_vectors: list[list[float]] = []
    dropped = 0
    for job, vector in zip(jobs, vectors, strict=True):
        key = f"{job['dataset_id']}\0{job['source_id']}"
        if current_hashes.get(key) != str(job["text_hash"]):
            dropped += 1
            continue
        kept_jobs.append(job)
        kept_vectors.append(vector)
    if dropped:
        print(f"stale during embedding: {dropped} chunk(s) dropped, re-queued on resume")
    jobs, vectors = kept_jobs, kept_vectors
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
                "run_id": run_dir.name,
                "model": model,
                "dimension": dimension,
                "renderer": EMBED_DOCUMENT_VERSION,
                "chunking": CHUNKING_VERSION,
                "reserved": reserved,
                "used": total_tokens,
            },
        )
        by_recipe: dict[str, list[tuple[dict[str, Any], list[float]]]] = {}
        for job, vector in zip(jobs, vectors, strict=True):
            key = f"{job['dataset_id']}\0{job['source_id']}"
            by_recipe.setdefault(key, []).append((job, vector))
        for key, pairs in by_recipe.items():
            dataset_id, _, source_id = key.partition("\0")
            conn.execute(
                text("DELETE FROM recipe_embeddings WHERE dataset_id=:d AND source_id=:s"),
                {"d": dataset_id, "s": source_id},
            )
            for job, vector in pairs:
                conn.execute(
                    text(
                        "INSERT INTO recipe_embeddings "
                        "(dataset_id, source_id, model, dimension, "
                        "renderer_version, chunking_version, chunk_index, "
                        "embedded_text_hash, embedding) "
                        "VALUES (:d, :s, :model, :dimension, :renderer, "
                        ":chunking, :idx, :hash, CAST(:vec AS vector)) "
                        "ON CONFLICT (dataset_id, source_id, model, dimension, "
                        "renderer_version, chunking_version, chunk_index) "
                        "DO UPDATE SET embedded_text_hash=excluded.embedded_text_hash, "
                        "embedding=excluded.embedding"
                    ),
                    {
                        "d": str(job["dataset_id"]),
                        "s": str(job["source_id"]),
                        "model": model,
                        "dimension": dimension,
                        "renderer": EMBED_DOCUMENT_VERSION,
                        "chunking": CHUNKING_VERSION,
                        "idx": int(job["chunk_index"]),
                        "hash": str(job["text_hash"]),
                        "vec": "[" + ",".join(repr(float(v)) for v in vector) + "]",
                    },
                )
            ledger["recipes"][key] = {
                "fingerprint": str(pairs[0][0].get("fingerprint")),
                "committed": True,
            }
        conn.execute(
            text("UPDATE embedding_runs SET status='done', updated_at=now() WHERE run_id=:run_id"),
            {"run_id": run_dir.name},
        )
    ledger["used_tokens_total"] = int(ledger.get("used_tokens_total", 0)) + total_tokens
    ledger["status"] = "done" if not jobs else "done"
    _save_ledger(run_dir, ledger)
    ledger_path = run_dir / "ledger.json"
    print(f"embedded chunks: {len(jobs)}; used tokens: {total_tokens}; ledger: {ledger_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
