"""Official post-rebuild full-text baseline (read-only; no tuning, no label edits).

Runs every frozen case through the deterministic production-equivalent path
(mapping plus repository search at case limit, then exact-pair full-document
fetch), times the query and fetch phases separately, and scores the top-5
against RECORDED labels only. Unjudged hits are reported as unjudged, never
as irrelevant. Writes ``baseline_fulltext_post_rebuild.json``.

Usage (from repo root):
    uv run python scripts/retrieval_eval/run_baseline.py
"""

from __future__ import annotations

import json
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "retrieval_eval"))
sys.path.insert(0, str(REPO / "src"))

from case_schema import load_case_file  # noqa: E402
from metrics import joint_verdict  # noqa: E402

PHASE1 = REPO / "evals" / "results" / "phase1"
OUT = PHASE1 / "baseline_fulltext_post_rebuild.json"

WARMUP = 2
REPS = 3


def _pct(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct / 100
    low, high = int(pos), min(int(pos) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _search_once(
    engine: Any,
    case: Any,
    search_all: Any,
    search_recipes: Any,
    init_state: Any,
    map_request_to_query: Any,
    match_dish: str,
) -> tuple[list[dict[str, Any]], str, str]:
    limit = int(case.limit)
    if case.dataset_id:
        search, extra = search_recipes, {"dataset_id": case.dataset_id}
    else:
        search, extra = search_all, {}
    if case.kind == "integration":
        request: dict[str, Any] = {"ingredients": list(case.ingredients)}
        for field in ("time_minutes", "dietary_constraints", "equipment"):
            if getattr(case, field):
                request[field] = getattr(case, field)
        state = init_state(request_id=f"base-{case.case_id}", request=request, dish=case.dish)
        query = map_request_to_query(state, dataset_id=case.dataset_id)
        if query.match_mode == match_dish:
            rows = search(
                engine,
                query.query_text,
                max_minutes=query.max_minutes,
                limit=limit,
                rank_pantry_terms=query.rank_pantry_terms,
                **extra,
            )
        else:
            rows = search(
                engine,
                query.query_text or "recipe",
                max_minutes=query.max_minutes,
                limit=limit,
                match_any_ingredients=query.match_any_ingredients,
                **extra,
            )
        return [dict(r) for r in rows], query.match_mode, query.query_text
    rows = search(
        engine, case.query_text or "", max_minutes=case.time_minutes, limit=limit, **extra
    )
    return [dict(r) for r in rows], "retrieval_only_text", case.query_text or ""


def main() -> int:
    from sqlalchemy import create_engine, text

    from culinary_copilot.config import Settings
    from culinary_copilot.recipes.repository import get_recipe, search_all, search_recipes
    from culinary_copilot.retrieval.query import MATCH_DISH, map_request_to_query
    from culinary_copilot.services.answers import init_state

    data = load_case_file(str(REPO / "evals/cases/phase1_retrieval.json"))
    corrections = json.loads((PHASE1 / "checkpoint1_corrections.json").read_text())
    recorded = {
        (label["case_id"], label["dataset_id"], label["source_id"]): label
        for label in corrections.get("labels", [])
    }
    engine = create_engine(Settings().database_url.get_secret_value())
    try:
        with engine.connect() as conn:
            pg_version = conn.execute(text("select version()")).scalar_one().split("(")[0].strip()
        case_results: list[dict[str, Any]] = []
        for case in data.cases:
            for _ in range(WARMUP):
                rows, _, _ = _search_once(
                    engine,
                    case,
                    search_all,
                    search_recipes,
                    init_state,
                    map_request_to_query,
                    MATCH_DISH,
                )
            query_times: list[float] = []
            rows = []
            for _ in range(REPS):
                start = time.perf_counter()
                rows, mode, query_text = _search_once(
                    engine,
                    case,
                    search_all,
                    search_recipes,
                    init_state,
                    map_request_to_query,
                    MATCH_DISH,
                )
                query_times.append((time.perf_counter() - start) * 1000)
            fetch_times: list[float] = []
            docs: list[dict[str, Any] | None] = []
            for _ in range(REPS):
                start = time.perf_counter()
                batch = []
                for row in rows:
                    try:
                        batch.append(
                            get_recipe(
                                engine, str(row["source_id"]), dataset_id=str(row["dataset_id"])
                            )
                        )
                    except ValueError:
                        batch.append(None)
                fetch_times.append((time.perf_counter() - start) * 1000)
                docs = batch
            top5 = rows[:5]
            ceiling = case.time_minutes
            scope = case.dataset_id
            violations: list[str] = []
            hits: list[dict[str, Any]] = []
            for rank, row in enumerate(top5, 1):
                total = row.get("total_minutes")
                usable = isinstance(total, (int, float)) and total == total and total > 0
                if ceiling is not None:
                    ceiling_value = float(ceiling)
                    if not usable:
                        violations.append(
                            f"{row['dataset_id']},{row['source_id']}: "
                            f"total={total} under ceiling {ceiling_value:g}"
                        )
                    elif (
                        isinstance(total, (int, float))
                        and not isinstance(total, bool)
                        and float(total) > ceiling_value
                    ):
                        violations.append(
                            f"{row['dataset_id']},{row['source_id']}: "
                            f"total={total} under ceiling {ceiling_value:g}"
                        )
                if scope is not None and row.get("dataset_id") != scope:
                    violations.append(
                        f"{row['dataset_id']},{row['source_id']}: outside scope {scope}"
                    )
                label = recorded.get((case.case_id, str(row["dataset_id"]), str(row["source_id"])))
                grade = label.get("topical_grade") if label else None
                hits.append(
                    {
                        "dataset_id": row["dataset_id"],
                        "source_id": row["source_id"],
                        "rank": rank,
                        "recorded_grade": grade,
                        "joint": joint_verdict(label)
                        if label and grade is not None
                        else "unjudged",
                        "document_available": docs[rank - 1] is not None
                        if rank - 1 < len(docs)
                        else False,
                    }
                )
            judged = [h for h in hits if h["recorded_grade"] is not None]
            relevant = [h for h in judged if h["recorded_grade"] >= 1]
            first = min([h["rank"] for h in relevant], default=None)
            verdicts: list[str] = [h["joint"] for h in judged]
            if any(v == "suitable" for v in verdicts):
                case_joint: str = "suitable"
            elif any(v == "unknown" for v in verdicts):
                case_joint = "unknown"
            elif judged:
                case_joint = "unsuitable"
            else:
                case_joint = "unjudged"
            case_results.append(
                {
                    "case_id": case.case_id,
                    "split": case.split,
                    "kind": case.kind,
                    "category": case.category,
                    "query_text": query_text,
                    "match_mode": mode,
                    "dataset_scope": scope or "combined",
                    "result_count": len(rows),
                    "empty_top5": not top5,
                    "judged_hits": len(judged),
                    "unjudged_hits": len(hits) - len(judged),
                    "recall_at_5": 1.0 if relevant else (0.0 if judged else None),
                    "rr_at_5": 1.0 / first if first else (0.0 if judged else None),
                    "joint": case_joint,
                    "violations": violations,
                    "query_ms_median": statistics.median(query_times),
                    "fetch_ms_median": statistics.median(fetch_times),
                    "hits": hits,
                }
            )
        query_lat = [c["query_ms_median"] for c in case_results]
        fetch_lat = [c["fetch_ms_median"] for c in case_results]
        judged_cases = [c for c in case_results if c["recall_at_5"] is not None]
        denominators: dict[str, Any] = {
            "cases_total": len(case_results),
            "cases_scored": len(judged_cases),
            "cases_without_judgments": sorted(
                c["case_id"] for c in case_results if c["recall_at_5"] is None
            ),
            "note": "Unjudged cases excluded from recall/MRR/joint (never zero-graded, "
            "never verified no-match).",
        }
        report: dict[str, Any] = {
            "baseline": "official-fulltext-post-rebuild",
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "status": (
                "AI-assisted review accepted by the project owner; not independently "
                "human-verified or culinarily validated. Measurements against recorded "
                "labels only; not production reliability or culinary safety certification."
            ),
            "environment": {
                "platform": platform.platform(),
                "pg": pg_version,
                "warmup": WARMUP,
                "reps": REPS,
            },
            "denominators": denominators,
            "recall_at_5": sum(c["recall_at_5"] for c in judged_cases) / len(judged_cases)
            if judged_cases
            else 0.0,
            "mrr_at_5": sum(c["rr_at_5"] for c in judged_cases) / len(judged_cases)
            if judged_cases
            else 0.0,
            "joint": {
                v: sum(1 for c in judged_cases if c["joint"] == v)
                for v in ("suitable", "unknown", "unsuitable")
            },
            "joint_unjudged_cases": sum(1 for c in case_results if c["joint"] == "unjudged"),
            "hard_filter_violations": sum(len(c["violations"]) for c in case_results),
            "violation_cases": sorted(c["case_id"] for c in case_results if c["violations"]),
            "empty_top5_cases": sorted(c["case_id"] for c in case_results if c["empty_top5"]),
            "latency_ms": {
                "query_p50": _pct(query_lat, 50),
                "query_p95": _pct(query_lat, 95),
                "fetch_p50": _pct(fetch_lat, 50),
                "fetch_p95": _pct(fetch_lat, 95),
                "note": "per-case medians over 3 reps after 2 warmups",
            },
            "by_split": _split(case_results),
            "by_category": _group(case_results, "category"),
            "by_scope": _group(case_results, "dataset_scope"),
            "cases": case_results,
        }
    finally:
        engine.dispose()
    OUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"scored={denominators['cases_scored']}/{denominators['cases_total']} "
        f"recall5={report['recall_at_5']:.3f} mrr5={report['mrr_at_5']:.3f} "
        f"violations={report['hard_filter_violations']}"
    )
    return 0


def _split(cases: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for split in ("development", "held_out"):
        members = [c for c in cases if c["split"] == split and c["recall_at_5"] is not None]
        out[split] = {
            "n_scored": len(members),
            "n_total": sum(1 for c in cases if c["split"] == split),
            "recall_at_5": sum(c["recall_at_5"] for c in members) / len(members)
            if members
            else None,
            "note": "held-out numbers must not drive iterative changes",
        }
    return out


def _group(cases: list[dict[str, Any]], key: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for case in cases:
        out.setdefault(str(case[key]), []).append(case)
    summary = {}
    for name, members in sorted(out.items()):
        scored = [c for c in members if c["recall_at_5"] is not None]
        summary[name] = {
            "n_scored": len(scored),
            "n_total": len(members),
            "recall_at_5": sum(c["recall_at_5"] for c in scored) / len(scored) if scored else None,
        }
    return summary


if __name__ == "__main__":
    raise SystemExit(main())
