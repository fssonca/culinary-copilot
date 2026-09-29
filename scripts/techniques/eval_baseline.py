#!/usr/bin/env python3
"""Technique retrieval baseline runner (Milestone 3, Phase 4).

Runs the frozen ``evals/technique_retrieval/cases.json`` queries in
full-text mode (``--mode vector`` is prepared for the paid run only:
it needs query + corpus embeddings and is NOT authorized here) against
a target database, and reports document-level HitRate@5 and MRR,
reusing ``scripts/retrieval_eval/metrics.py`` (``recall_at_k`` /
``reciprocal_rank`` with ``(doc_id, doc_id)`` pairs).

Chunk hits are deduplicated to documents preserving first-hit order
before scoring. Writes a results JSON with case ids, retrieved doc
ids, and scores only (no document text).

``--mode vector`` is prepared for the paid run: it embeds the 16
queries (a separate small paid call — 16 short queries, ~640
retry-inclusive tokens, inside the $0.01 bound) and reports HitRate@5
and MRR both with the 0.66 cutoff (recipe-calibrated, UNCALIBRATED for
technique chunks) and with no cutoff, plus the cosine distance of each
relevant document. Without ``--fake`` it refuses (paid run not
authorized); ``--fake`` uses the deterministic fake provider on a
disposable database for rehearsal only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "retrieval_eval"))

from metrics import reciprocal_rank  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from culinary_copilot.recipes.technique_repository import (  # noqa: E402
    search_techniques_fulltext,
)

DEFAULT_CASES = REPO_ROOT / "evals" / "technique_retrieval" / "cases.json"


def _verify_freeze(payload: dict[str, Any]) -> None:
    body = {k: v for k, v in payload.items() if k != "freeze_sha256"}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if digest != payload.get("freeze_sha256"):
        raise ValueError("cases freeze hash mismatch; labels changed after freezing")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--mode", choices=("fulltext", "vector"), default="fulltext")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--fake",
        action="store_true",
        help="vector mode only: fake query embeddings (rehearsal, no paid call)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="vector mode only: real query embeddings (paid; needs key, budget, --yes)",
    )
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--ceiling-usd", type=float, default=None)
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    _verify_freeze(payload)
    from culinary_copilot.config import Settings

    db_url = args.database_url or Settings().database_url.get_secret_value()
    if args.mode == "vector":
        return _run_vector(args, payload, db_url)

    engine = create_engine(db_url)
    rows: list[dict[str, Any]] = []
    for case in payload["cases"]:
        hits, match = search_techniques_fulltext(engine, case["query"], limit=args.limit)
        seen: list[str] = []
        for hit in hits:
            if hit["doc_id"] not in seen:
                seen.append(str(hit["doc_id"]))
        relevant = [(d, d) for d in case["relevant_docs"]]
        retrieved = [(d, d) for d in seen]
        # Document-level HitRate@5: any relevant doc in the top-5.
        is_hit = any(pair in retrieved for pair in relevant)
        rows.append(
            {
                "case_id": case["case_id"],
                "paraphrase": bool(case.get("paraphrase")),
                "match": match,
                "hit_at_5": is_hit,
                "reciprocal_rank": reciprocal_rank(relevant, retrieved, k=5),
                "retrieved_docs": seen,
                "relevant_docs": list(case["relevant_docs"]),
            }
        )
    hits_n = sum(1 for r in rows if r["hit_at_5"])
    rrs = [r["reciprocal_rank"] for r in rows if r["reciprocal_rank"] is not None]
    summary = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "fulltext",
        "cases": len(rows),
        "hit_rate_at_5": hits_n / len(rows) if rows else 0.0,
        "mrr": sum(rrs) / len(rrs) if rrs else 0.0,
        "cases_sha256": payload["freeze_sha256"],
        "rows": rows,
    }
    print(
        f"cases: {len(rows)}; HitRate@5: {summary['hit_rate_at_5']:.3f}; MRR: {summary['mrr']:.3f}"
    )
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"results: {args.out}")
    return 0


def _dedup_docs(hits: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for hit in hits:
        doc_id = str(hit["doc_id"])
        if doc_id not in seen:
            seen.append(doc_id)
    return seen


def _reserve_query_tokens(queries: list[str], *, retries: int) -> int:
    """Retry-inclusive token reservation for the eval query set (pure)."""
    from culinary_copilot.embeddings.rendering import estimate_tokens_bytes

    return sum(estimate_tokens_bytes(q) for q in queries) * (max(0, retries) + 1)


def _run_vector(args: Any, payload: dict[str, Any], db_url: str) -> int:
    import asyncio as _asyncio

    from culinary_copilot.config import Settings
    from culinary_copilot.embeddings.provider import (
        FakeEmbeddingProvider,
        OpenAIEmbeddingProvider,
    )
    from culinary_copilot.embeddings.registry import estimate_cost_usd
    from culinary_copilot.recipes.technique_repository import (
        TECHNIQUE_VECTOR_CUTOFF,
        TechniqueNotConfiguredError,
        apply_technique_vector_cutoff,
        technique_vector_candidates,
    )

    settings = Settings()
    model = settings.embedding_model
    dimension = settings.embedding_dimension
    queries = [str(case["query"]) for case in payload["cases"]]
    if args.fake:
        provider: Any = FakeEmbeddingProvider(model=model, dimension=dimension)
        query_tokens = 0
        query_cost = 0.0
    elif args.live and args.yes and args.ceiling_usd is not None:
        if not settings.embeddings_enabled or not settings.openai_api_key.get_secret_value():
            print(
                "error: live vector eval needs EMBEDDINGS_ENABLED=true and OPENAI_API_KEY.",
                file=sys.stderr,
            )
            return 2
        retries = max(0, settings.embed_max_retries)
        query_tokens = _reserve_query_tokens(queries, retries=retries)
        query_cost = estimate_cost_usd(query_tokens, model) or 0.0
        print(f"query reservation: {query_tokens} tokens (~${query_cost:.5f})")
        if query_cost > float(args.ceiling_usd):
            print(
                f"error: query reservation ${query_cost:.5f} exceeds ceiling "
                f"${float(args.ceiling_usd):.5f}",
                file=sys.stderr,
            )
            return 2
        provider = OpenAIEmbeddingProvider(
            model=model,
            dimension=dimension,
            api_key=settings.openai_api_key.get_secret_value(),
            timeout_s=settings.embed_timeout_s,
            max_retries=settings.embed_max_retries,
        )
    else:
        print(
            "error: vector mode needs the paid embedding run (query + corpus "
            "embeddings). Use --live --yes --ceiling-usd <usd> with "
            "EMBEDDINGS_ENABLED and a key, or --fake for a "
            "disposable-DB rehearsal only.",
            file=sys.stderr,
        )
        return 2
    engine = create_engine(db_url)
    rows: list[dict[str, Any]] = []
    query_used_reported = 0
    try:
        for case in payload["cases"]:
            result = _asyncio.run(provider.embed_texts([str(case["query"])]))
            vectors = result.vectors
            query_used_reported += int(getattr(result.usage, "prompt_tokens", 0) or 0)
            candidates = technique_vector_candidates(
                engine, vectors[0], limit=args.limit, model=model, dimension=dimension
            )
            kept = apply_technique_vector_cutoff(candidates, TECHNIQUE_VECTOR_CUTOFF)
            docs_cut = _dedup_docs(kept)
            docs_all = _dedup_docs(candidates)
            distances = {
                str(case_doc): min(
                    float(c["distance"]) for c in candidates if str(c["doc_id"]) == str(case_doc)
                )
                for case_doc in case["relevant_docs"]
                if any(str(c["doc_id"]) == str(case_doc) for c in candidates)
            }
            relevant = [(d, d) for d in case["relevant_docs"]]
            cut_pairs = [(d, d) for d in docs_cut]
            all_pairs = [(d, d) for d in docs_all]
            rows.append(
                {
                    "case_id": case["case_id"],
                    "paraphrase": bool(case.get("paraphrase")),
                    "cutoff_0_66": {
                        "hit_at_5": any(pair in cut_pairs for pair in relevant),
                        "reciprocal_rank": reciprocal_rank(relevant, cut_pairs, k=5),
                        "retrieved_docs": docs_cut,
                    },
                    "no_cutoff": {
                        "hit_at_5": any(pair in all_pairs for pair in relevant),
                        "reciprocal_rank": reciprocal_rank(relevant, all_pairs, k=5),
                        "retrieved_docs": docs_all,
                    },
                    "relevant_distances": distances,
                    "relevant_docs": list(case["relevant_docs"]),
                }
            )
    except TechniqueNotConfiguredError as exc:
        print(f"error: vector eval not configured: {exc}", file=sys.stderr)
        return 2

    def _aggregate(key: str) -> dict[str, float]:
        hits_n = sum(1 for r in rows if r[key]["hit_at_5"])
        rrs = [r[key]["reciprocal_rank"] for r in rows if r[key]["reciprocal_rank"] is not None]
        return {
            "hit_rate_at_5": hits_n / len(rows) if rows else 0.0,
            "mrr": sum(rrs) / len(rrs) if rrs else 0.0,
        }

    # Query cost is computed from provider-REPORTED usage; the
    # reservation (estimate) is kept as its own field. Runs before this
    # fix recorded the estimate as usage; those files are not rewritten.
    query_used_cost = estimate_cost_usd(query_used_reported, model) or 0.0
    summary = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "vector",
        "fake": bool(args.fake),
        "query_tokens_reserved": query_tokens,
        "query_cost_usd": query_cost,
        "query_tokens_used": query_used_reported,
        "query_used_cost_usd": query_used_cost,
        "cutoff_note": "0.66 is recipe-calibrated and UNCALIBRATED for technique "
        "chunks; both figures are reported so the cutoff can be set from data.",
        "cases": len(rows),
        "cutoff_0_66": _aggregate("cutoff_0_66"),
        "no_cutoff": _aggregate("no_cutoff"),
        "cases_sha256": payload["freeze_sha256"],
        "rows": rows,
    }
    print(
        f"cases: {len(rows)}; cutoff HitRate@5: "
        f"{summary['cutoff_0_66']['hit_rate_at_5']:.3f} "
        f"MRR: {summary['cutoff_0_66']['mrr']:.3f}; no-cutoff HitRate@5: "
        f"{summary['no_cutoff']['hit_rate_at_5']:.3f} "
        f"MRR: {summary['no_cutoff']['mrr']:.3f}; query tokens used "
        f"(provider-reported): {query_used_reported} "
        f"(~${query_used_cost:.5f}), reserved: {query_tokens}"
    )
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"results: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
