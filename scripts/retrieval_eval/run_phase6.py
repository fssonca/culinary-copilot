"""Frozen three-mode retrieval comparison harness (Phase 6, read-only).

Runs full-text top-20 plus vector top-20 per query from one raw run, then
derives every configuration's top-5 from that record (filters are
post-retrieval, so the grid needs no re-querying). Always uses
``allow_fallback=False``: an embedding failure is a recorded error, never a
silent full-text result.

Recipe-filled raw output goes to an ignored path (default
``data/phase6/``); commit-ready files hold aggregates, hashes and code only.

Usage (from repo root):
    uv run python scripts/retrieval_eval/run_phase6.py --fake
    uv run python scripts/retrieval_eval/run_phase6.py --live --price-verified --ceiling-usd 0.05
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "retrieval_eval"))
sys.path.insert(0, str(REPO / "src"))

from phase6_cases import (  # noqa: E402
    Phase6CaseFile,
    check_phase6_file,
    eval_set_hash,
    load_phase6_file,
)
from phase6_eval import DERIVED_LIMIT, RAW_N, derive_configs, mechanical_violations  # noqa: E402

MODEL = "text-embedding-3-small"
DIMENSION = 1536
RRF_K = 60
VECTOR_CANDIDATES_N = 20
WARMUP = 2
REPS = 3
EMBED_REPS = 3
DEFAULT_CEILING_USD = 0.05
MAX_CEILING_USD = 0.05
EXPECTED_PRICE_PER_1M = 0.02


class CeilingBreach(Exception):
    """Next reservation would exceed the aggregate spend ceiling."""


@dataclass
class SpendTracker:
    price_per_1m: float
    ceiling_usd: float
    max_retries: int
    reserved_usd: float = 0.0
    actual_usd: float = 0.0
    calls_reserved: int = 0
    calls_sent: int = 0
    actual_tokens: int = 0
    reserved_tokens: int = 0

    def reserve(self, text: str) -> float:
        from culinary_copilot.embeddings.rendering import estimate_tokens_bytes

        tokens = estimate_tokens_bytes(text) * (self.max_retries + 1)
        cost = tokens / 1e6 * self.price_per_1m
        if self.reserved_usd + cost > self.ceiling_usd:
            raise CeilingBreach(
                f"reservation ${self.reserved_usd:.6f} + ${cost:.6f} would exceed "
                f"ceiling ${self.ceiling_usd:.2f}; stopping"
            )
        self.reserved_usd += cost
        self.reserved_tokens += tokens
        self.calls_reserved += 1
        return cost

    def record_actual(self, tokens: int) -> None:
        self.actual_usd += tokens / 1e6 * self.price_per_1m
        self.actual_tokens += tokens
        self.calls_sent += 1


def _pct(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct / 100
    low, high = int(pos), min(int(pos) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _git(args: list[str]) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, cwd=REPO)
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_query(case: dict[str, Any]) -> dict[str, Any]:
    """Request-to-query mapping identical to the frozen baseline path."""
    from culinary_copilot.retrieval.query import MATCH_DISH, map_request_to_query
    from culinary_copilot.services.answers import init_state

    if case.get("kind") == "integration":
        request: dict[str, Any] = {"ingredients": list(case.get("ingredients") or [])}
        for fname in ("time_minutes", "dietary_constraints", "equipment"):
            if case.get(fname):
                request[fname] = case[fname]
        state = init_state(
            request_id=f"p6-{case['case_id']}", request=request, dish=case.get("dish")
        )
        query = map_request_to_query(state, dataset_id=case.get("dataset_id"))
        if query.match_mode == MATCH_DISH:
            return {
                "text": query.query_text,
                "match_mode": query.match_mode,
                "max_minutes": query.max_minutes,
                "dataset_id": query.dataset_id,
                "match_any": None,
                "rank_pantry": list(query.rank_pantry_terms or []),
            }
        return {
            "text": query.query_text or "recipe",
            "match_mode": query.match_mode,
            "max_minutes": query.max_minutes,
            "dataset_id": query.dataset_id,
            "match_any": list(query.match_any_ingredients or []),
            "rank_pantry": [],
        }
    return {
        "text": case.get("query_text") or "",
        "match_mode": "retrieval_only_text",
        "max_minutes": case.get("time_minutes"),
        "dataset_id": case.get("dataset_id"),
        "match_any": None,
        "rank_pantry": [],
    }


def _fulltext_search(engine: Any, built: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    from culinary_copilot.recipes.repository import search_all, search_recipes

    if built["dataset_id"]:
        return [
            dict(r)
            for r in search_recipes(
                engine,
                built["text"],
                max_minutes=built["max_minutes"],
                limit=limit,
                dataset_id=built["dataset_id"],
                match_any_ingredients=built["match_any"],
                rank_pantry_terms=built["rank_pantry"],
            )
        ]
    return [
        dict(r)
        for r in search_all(
            engine,
            built["text"],
            max_minutes=built["max_minutes"],
            limit=limit,
            match_any_ingredients=built["match_any"],
            rank_pantry_terms=built["rank_pantry"],
        )
    ]


def _vector_search(engine: Any, vector: list[float], built: dict[str, Any]) -> list[dict[str, Any]]:
    from culinary_copilot.recipes.vector_search import vector_candidates

    return [
        dict(r)
        for r in vector_candidates(
            engine,
            vector,
            ingredients=[],
            max_minutes=built["max_minutes"],
            limit=VECTOR_CANDIDATES_N,
            dataset_id=built["dataset_id"],
            match_any_ingredients=built["match_any"],
            rank_pantry_terms=built["rank_pantry"],
            model=MODEL,
            dimension=DIMENSION,
        )
    ]


def _corpus_fingerprint(engine: Any) -> dict[str, Any]:
    from sqlalchemy import text

    from culinary_copilot.embeddings.rendering import CHUNKING_VERSION, EMBED_DOCUMENT_VERSION
    from culinary_copilot.recipes.search import SEARCH_DOCUMENT_VERSION

    with engine.connect() as conn:
        pg_version = conn.execute(text("select version()")).scalar_one().split("(")[0].strip()
        total = conn.execute(text("select count(*) from recipes")).scalar_one()
        by_dataset = [
            [r[0], r[1]]
            for r in conn.execute(
                text("select dataset_id, count(*) from recipes group by 1 order by 1")
            ).all()
        ]
        imports = [
            [r[0], r[1], r[2]]
            for r in conn.execute(
                text(
                    "select dataset_id, revision, normalizer_version from recipe_imports order by 1"
                )
            ).all()
        ]
        search_versions = [
            [r[0], r[1]]
            for r in conn.execute(
                text("select search_document_version, count(*) from recipes group by 1 order by 1")
            ).all()
        ]
        vectors = conn.execute(text("select count(*) from recipe_embeddings")).scalar_one()
        quarantine = conn.execute(text("select count(*) from recipe_quarantine")).scalar_one()
    return {
        "pg": pg_version,
        "recipes_total": total,
        "by_dataset": by_dataset,
        "imports": imports,
        "quarantine_total": quarantine,
        "search_renderer": SEARCH_DOCUMENT_VERSION,
        "search_document_versions": search_versions,
        "vectors_total": vectors,
        "embedding_model": MODEL,
        "embedding_dimension": DIMENSION,
        "embed_renderer": EMBED_DOCUMENT_VERSION,
        "chunking_version": CHUNKING_VERSION,
    }


async def _embed_texts(
    provider: Any, texts: list[str], tracker: SpendTracker, live: bool
) -> list[tuple[list[float], int]]:
    """Embed texts with pre-send reservation; returns (vector, actual_tokens)."""
    out: list[tuple[list[float], int]] = []
    for text in texts:
        cleaned = " ".join(text.split())
        tracker.reserve(cleaned)
        result = await provider.embed_texts([cleaned])
        actual = int(result.usage.total_tokens or result.usage.prompt_tokens or 0)
        tracker.record_actual(actual)
        out.append((list(result.vectors[0]), actual))
    _ = live
    return out


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        action="append",
        default=[],
        help="Case file (repeatable). Default: phase1 frozen + phase6 dev additions if present.",
    )
    parser.add_argument("--out", default="data/phase6/phase6_raw.json")
    parser.add_argument(
        "--live", action="store_true", help="Paid query embeddings (needs --price-verified)."
    )
    parser.add_argument(
        "--price-verified",
        action="store_true",
        help="Operator confirms the $0.02/1M price was re-verified on the official pricing page.",
    )
    parser.add_argument("--ceiling-usd", type=float, default=DEFAULT_CEILING_USD)
    parser.add_argument("--cutoff", type=float, default=None)
    parser.add_argument(
        "--split",
        action="append",
        default=[],
        choices=["development", "held_out", "blind_confirmation"],
        help="Restrict to splits (repeatable). Default: all loaded cases.",
    )
    return parser.parse_args()


def main() -> int:
    from case_schema import load_case_file as load_phase1_file

    from culinary_copilot.embeddings.provider import FakeEmbeddingProvider
    from culinary_copilot.embeddings.registry import embedding_spec
    from culinary_copilot.retrieval.service import _excerpt_from_doc

    args = _parse_args()
    if args.ceiling_usd > MAX_CEILING_USD:
        print(f"refusing ceiling ${args.ceiling_usd:.2f} above ${MAX_CEILING_USD:.2f} cap")
        return 2
    spec = embedding_spec(MODEL)
    if spec is None or abs(spec.prices.input_per_1m - EXPECTED_PRICE_PER_1M) > 1e-9:
        print("registry pricing drifted; re-verify before any live run")
        return 2
    live = bool(args.live)
    if live and not args.price_verified:
        print("live runs require --price-verified (re-check the official pricing page first)")
        return 2

    case_files: list[Path] = [Path(p) for p in args.cases] if args.cases else []
    if not case_files:
        case_files.append(REPO / "evals" / "cases" / "phase1_retrieval.json")
        dev_add = REPO / "evals" / "cases" / "phase6_dev_additions.json"
        if dev_add.exists():
            case_files.append(dev_add)
    cases: list[dict[str, Any]] = []
    for path in case_files:
        if path.name == "phase1_retrieval.json":
            data = load_phase1_file(str(path))
            cases.extend(c.model_dump() for c in data.cases)
        else:
            data6: Phase6CaseFile = load_phase6_file(path)
            problems = check_phase6_file(data6)
            if problems:
                print(json.dumps({"file": str(path), "problems": problems}, indent=2))
                return 1
            cases.extend(c.model_dump() for c in data6.cases)
    seen: set[str] = set()
    wanted = set(args.split or [])
    for case in cases:
        if case["case_id"] in seen:
            print(f"duplicate case_id {case['case_id']} across case files")
            return 1
        seen.add(str(case["case_id"]))
    if wanted:
        cases = [c for c in cases if str(c.get("split")) in wanted]
        if not cases:
            print(f"no cases match --split {sorted(wanted)}")
            return 1
    rubric = REPO / "evals" / "rubric_v1.md"
    eval_hash = eval_set_hash([*case_files, rubric])

    from sqlalchemy import create_engine

    from culinary_copilot.config import Settings

    engine = create_engine(Settings().database_url.get_secret_value())
    try:
        fingerprint = _corpus_fingerprint(engine)
        built_by_case: dict[str, dict[str, Any]] = {}
        for case in cases:
            built_by_case[str(case["case_id"])] = _build_query(case)

        # Distinct query texts share one cached embedding keyed by
        # (model, dimension, text hash); latency measured separately.
        distinct: dict[str, str] = {}
        for case_id, built in built_by_case.items():
            distinct.setdefault(_text_hash(built["text"]), built["text"])

        if live:
            import os as _os

            from culinary_copilot.embeddings.provider import OpenAIEmbeddingProvider

            api_key = _os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                print("live runs need OPENAI_API_KEY in the environment")
                return 2
            provider: Any = OpenAIEmbeddingProvider(
                model=MODEL, dimension=DIMENSION, api_key=api_key, max_retries=1
            )
            max_retries = 1
        else:
            provider = FakeEmbeddingProvider(model=MODEL, dimension=DIMENSION)
            max_retries = 0
        tracker = SpendTracker(
            price_per_1m=spec.prices.input_per_1m,
            ceiling_usd=args.ceiling_usd,
            max_retries=max_retries,
        )

        cache: dict[tuple[str, int, str], list[float]] = {}
        embed_ms_by_hash: dict[str, float] = {}
        errors: dict[str, str] = {}

        async def _embed_all() -> None:
            for digest, text in distinct.items():
                key = (MODEL, DIMENSION, digest)
                try:
                    # One embedding for results, cached by (model, dimension, text hash).
                    got = await _embed_texts(provider, [text], tracker, live)
                    cache[key] = got[0][0]
                    # Latency measured separately: 3 repetitions per distinct query.
                    reps: list[float] = []
                    for _ in range(EMBED_REPS):
                        start = time.perf_counter()
                        await _embed_texts(provider, [text], tracker, live)
                        reps.append((time.perf_counter() - start) * 1000)
                    embed_ms_by_hash[digest] = statistics.median(reps)
                except CeilingBreach as exc:
                    errors[digest] = f"ceiling: {exc}"
                except Exception as exc:  # recorded error, never silent fallback
                    errors[digest] = f"{type(exc).__name__}: {exc}"

        asyncio.run(_embed_all())

        from culinary_copilot.recipes.repository import get_recipe

        case_results: list[dict[str, Any]] = []
        fulltext_lat: list[float] = []
        vector_lat: list[float] = []
        hybrid_lat: list[float] = []
        vector_with_embed: list[float] = []
        hybrid_with_embed: list[float] = []
        fetch_lat: list[float] = []
        for case in cases:
            case_id = str(case["case_id"])
            built = built_by_case[case_id]
            digest = _text_hash(built["text"])
            error = errors.get(digest)
            fulltext20: list[dict[str, Any]] = []
            vector20: list[dict[str, Any]] = []
            fulltext_ms = 0.0
            vector_ms = 0.0
            if error is None:
                for _ in range(WARMUP):
                    _fulltext_search(engine, built, RAW_N)
                times: list[float] = []
                for _ in range(REPS):
                    start = time.perf_counter()
                    fulltext20 = _fulltext_search(engine, built, RAW_N)
                    times.append((time.perf_counter() - start) * 1000)
                fulltext_ms = statistics.median(times)
                vector = cache.get((MODEL, DIMENSION, digest))
                if vector is None:
                    error = "embedding cache miss"
                else:
                    for _ in range(WARMUP):
                        _vector_search(engine, vector, built)
                    vtimes: list[float] = []
                    for _ in range(REPS):
                        start = time.perf_counter()
                        vector20 = _vector_search(engine, vector, built)
                        vtimes.append((time.perf_counter() - start) * 1000)
                    vector_ms = statistics.median(vtimes)
            fulltext_ranked = [
                {
                    "dataset_id": r.get("dataset_id"),
                    "source_id": r.get("source_id"),
                    "rank": rank,
                    "score": r.get("score"),
                    "total_minutes": r.get("total_minutes"),
                }
                for rank, r in enumerate(fulltext20, 1)
            ]
            vector_ranked: list[dict[str, Any]] = []
            for rank, row in enumerate(vector20, 1):
                raw_distance: Any = row.get("distance")
                vector_ranked.append(
                    {
                        "dataset_id": row.get("dataset_id"),
                        "source_id": row.get("source_id"),
                        "rank": rank,
                        "distance": float(raw_distance),
                    }
                )
            derived = derive_configs(fulltext20, vector20, cutoff=args.cutoff, rrf_k=RRF_K)
            # Full-document fetch for the pooled derived top-5 union.
            pool_ids: dict[tuple[str, str], None] = {}
            for key in ("fulltext", "vector_a", "vector_c", "hybrid_a", "hybrid_b", "hybrid_c"):
                for row in derived[key]:
                    pool_ids.setdefault(
                        (str(row.get("dataset_id")), str(row.get("source_id"))), None
                    )
            fetch_ms = 0.0
            docs: dict[str, dict[str, Any]] = {}
            if pool_ids:
                fbatch: list[dict[str, Any] | None] = []
                ftimes: list[float] = []
                for _ in range(REPS):
                    start = time.perf_counter()
                    batch = []
                    for dataset_id, source_id in pool_ids:
                        try:
                            batch.append(get_recipe(engine, source_id, dataset_id=dataset_id))
                        except ValueError:
                            batch.append(None)
                    ftimes.append((time.perf_counter() - start) * 1000)
                    fbatch = batch
                fetch_ms = statistics.median(ftimes)
                for (dataset_id, source_id), doc in zip(pool_ids, fbatch, strict=False):
                    if doc is None:
                        continue
                    durations = doc.get("durations_minutes")
                    total = durations.get("TotalTime") if isinstance(durations, dict) else None
                    ingredients = doc.get("ingredients") or []
                    names = [
                        i.get("canonical") or i.get("name")
                        for i in ingredients
                        if isinstance(i, dict)
                    ]
                    docs[f"{dataset_id}\x00{source_id}"] = {
                        "title": doc.get("title"),
                        "ingredients": names[:30],
                        "total_minutes_reported": total,
                        "excerpt": _excerpt_from_doc(doc),
                    }
            violations: dict[str, list[str]] = {}
            ceiling_raw: Any = case.get("time_minutes")
            ceiling: float | None = float(ceiling_raw) if ceiling_raw is not None else None
            scope = case.get("dataset_id")
            for key in ("fulltext", "vector_a", "vector_c", "hybrid_a", "hybrid_b", "hybrid_c"):
                for row in derived[key]:
                    identity = f"{row.get('dataset_id')}\x00{row.get('source_id')}"
                    doc = docs.get(identity)
                    total = doc["total_minutes_reported"] if doc else row.get("total_minutes")
                    violations[f"{key}:{identity}"] = mechanical_violations(
                        total_minutes=total,
                        ceiling=float(ceiling) if ceiling is not None else None,
                        dataset_id=str(row.get("dataset_id")),
                        scope=str(scope) if scope is not None else None,
                    )
            embed_ms = embed_ms_by_hash.get(digest, 0.0)
            fulltext_lat.append(fulltext_ms)
            vector_lat.append(vector_ms)
            hybrid_lat.append(fulltext_ms + vector_ms)
            vector_with_embed.append(vector_ms + embed_ms)
            hybrid_with_embed.append(fulltext_ms + vector_ms + embed_ms)
            fetch_lat.append(fetch_ms)
            case_results.append(
                {
                    "case_id": case_id,
                    "split": case.get("split"),
                    "kind": case.get("kind"),
                    "category": case.get("category"),
                    "query_text": built["text"],
                    "match_mode": built["match_mode"],
                    "dataset_scope": scope or "combined",
                    "error": error,
                    "fulltext_top20": fulltext_ranked,
                    "vector_top20": vector_ranked,
                    "derived_top5": {
                        key: [
                            {
                                "dataset_id": r.get("dataset_id"),
                                "source_id": r.get("source_id"),
                            }
                            for r in derived[key]
                        ]
                        for key in (
                            "fulltext",
                            "vector_a",
                            "vector_c",
                            "hybrid_a",
                            "hybrid_b",
                            "hybrid_c",
                        )
                    },
                    "vector_abstention_c": derived["vector_abstention_c"],
                    "violations": violations,
                    "latency_ms": {
                        "embed_median": embed_ms,
                        "fulltext_db_median": fulltext_ms,
                        "vector_db_median": vector_ms,
                        "fetch_median": fetch_ms,
                    },
                    "documents": docs,
                }
            )
        latency = {
            "fulltext_db_p50": _pct(fulltext_lat, 50),
            "fulltext_db_p95": _pct(fulltext_lat, 95),
            "vector_db_p50": _pct(vector_lat, 50),
            "vector_db_p95": _pct(vector_lat, 95),
            "vector_with_embed_p50": _pct(vector_with_embed, 50),
            "vector_with_embed_p95": _pct(vector_with_embed, 95),
            "hybrid_db_p50": _pct(hybrid_lat, 50),
            "hybrid_db_p95": _pct(hybrid_lat, 95),
            "hybrid_with_embed_p50": _pct(hybrid_with_embed, 50),
            "hybrid_with_embed_p95": _pct(hybrid_with_embed, 95),
            "fetch_p50": _pct(fetch_lat, 50),
            "fetch_p95": _pct(fetch_lat, 95),
            "note": (
                "db medians over 3 reps after 2 warmups; "
                "embed medians over 3 reps per distinct query"
            ),
        }
        report: dict[str, Any] = {
            "harness": "phase6-three-mode-raw",
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "status": "raw retrieval only; scoring happens in analysis with recorded labels",
            "model": MODEL,
            "dimension": DIMENSION,
            "rrf_k": RRF_K,
            "vector_candidates_n": VECTOR_CANDIDATES_N,
            "raw_n": RAW_N,
            "derived_limit": DERIVED_LIMIT,
            "cutoff": args.cutoff,
            "allow_fallback": False,
            "live_embeddings": live,
            "pricing": {
                "input_per_1m_usd": spec.prices.input_per_1m,
                "source": spec.prices.source_url,
                "date_verified": spec.prices.date_verified,
                "operator_reverified": bool(args.price_verified) if live else False,
            },
            "spend": {
                "ceiling_usd": args.ceiling_usd,
                "reserved_usd": tracker.reserved_usd,
                "actual_usd": tracker.actual_usd,
                "reserved_tokens": tracker.reserved_tokens,
                "actual_tokens": tracker.actual_tokens,
                "calls_reserved": tracker.calls_reserved,
                "calls_sent": tracker.calls_sent,
            },
            "corpus_fingerprint": fingerprint,
            "code_revision": _git(["git", "rev-parse", "--short", "HEAD"]),
            "dirty_tree": _git(["git", "status", "--short"])[:2000],
            "eval_files": [str(p) for p in case_files],
            "eval_set_hash": eval_hash,
            "latency_ms": latency,
            "cases": case_results,
        }
    finally:
        engine.dispose()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"cases={len(case_results)} errors={sum(1 for c in case_results if c['error'])} "
        f"reserved=${tracker.reserved_usd:.6f} actual=${tracker.actual_usd:.6f} "
        f"ceiling=${args.ceiling_usd:.2f} out={out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
