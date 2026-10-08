"""H6 ranking comparison: current vs title-first vs bounded title boost (offline, read-only).

Runs every frozen Phase 1 case through three rankings at the case limit and
scores the top-5 against RECORDED labels only (unjudged stays unjudged, as in
run_baseline.py). The production ranking (recipes/repository.py) is never
touched: ``current`` calls the production repository functions, while the two
experiment rankings use a copied SELECT with only the ORDER BY changed.

Rankings (H6, 2026-10-08):
- current: repository.search_all/search_recipes (score DESC, then identity;
  pantry boost where the mapping supplies rank_pantry_terms; overlap count
  first in pantry-overlap mode).
- title_first: same WHERE filters, ORDER BY title_match DESC first, where
  title_match is (to_tsvector('english', title) @@ plainto_tsquery) -- i.e.
  full title-word matches first, the 2026-10-07 experiment reproduced.
- title_boost: same WHERE filters, ORDER BY (score + TITLE_BOOST_WEIGHT *
  title_score) DESC, where title_score is ts_rank_cd on the title vector
  alone (weight 2.0). It is a boost, not precedence: body score still
  decides among equal title scores, so a strong body match can outrank a
  weak title match. Applied to dish mode and to every retrieval_only text
  query (there is no dish-intent signal for free text, so phrase queries
  are boosted too); only pantry-overlap (ingredient discovery) queries run
  unchanged.

Malformed H5 records (odunola/foodie rows titled "summary", 158 in the app
DB) are flagged wherever they surface in a top-5. Metrics are reported
as-is and with those rows quarantined (skipped, next non-malformed rows
fill the top-5), so the comparison does not depend on the re-ingestion.

Read-only: every connection opens with default_transaction_read_only=on
(including the production repository calls), so any write would fail.

Paired metrics: the per-ranking metrics follow run_baseline.py and score a
case only when its top-5 holds a judged hit, so rankings are scored on
different case sets. ``paired_vs_*`` scores every ranking on the cases the
current ranking scores, counting a top-5 with no judged hit as 0 (a lower
bound for the experiments, since unjudged hits may be relevant).

All outputs go to --out (default evals/results/h6/compare.json); the
frozen Phase 1 result file is never written.

Usage (from repo root):
    uv run python scripts/retrieval_eval/h6_ranking_compare.py
    uv run python scripts/retrieval_eval/h6_ranking_compare.py --out /tmp/h6.json
    uv run python scripts/retrieval_eval/h6_ranking_compare.py \\
        --cases evals/cases/h6_dish_vs_ingredient_v1.json --out /tmp/h6reg.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "retrieval_eval"))
sys.path.insert(0, str(REPO / "src"))

from case_schema import load_case_file  # noqa: E402
from metrics import joint_verdict  # noqa: E402

PHASE1 = REPO / "evals" / "results" / "phase1"
DEFAULT_OUT = REPO / "evals" / "results" / "h6" / "compare.json"

#: Bounded title weight for the title_boost experiment (H6, 2026-10-08).
#: 2.0 recovers the adobo dish titles (body gap ~0.2) while the body score
#: still decides among equal title scores, so it is a boost, not precedence.
TITLE_BOOST_WEIGHT = 2.0

#: How many rows to fetch per ranking so the quarantined top-5 can be
#: recomputed without re-querying (top-5 plus headroom for 158 malformed).
FETCH_N = 10

#: Malformed H5 identity test: odunola/foodie rows titled exactly "summary".
MALFORMED_DATASET = "odunola/foodie"
MALFORMED_TITLE = "summary"


def _is_malformed(row: dict[str, Any]) -> bool:
    return (
        str(row.get("dataset_id")) == MALFORMED_DATASET
        and str(row.get("title") or "") == MALFORMED_TITLE
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path | str) -> str:
    """Repo-relative path for the report (no local absolute paths)."""
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(REPO.resolve()))
    except ValueError:
        return str(path)


def _build_query(case: Any) -> dict[str, Any]:
    from culinary_copilot.retrieval.query import MATCH_DISH, map_request_to_query
    from culinary_copilot.services.answers import init_state

    if case.kind == "integration":
        request: dict[str, Any] = {"ingredients": list(case.ingredients)}
        for field in ("time_minutes", "dietary_constraints", "equipment"):
            if getattr(case, field):
                request[field] = getattr(case, field)
        state = init_state(request_id=f"h6-{case.case_id}", request=request, dish=case.dish)
        query = map_request_to_query(state, dataset_id=case.dataset_id)
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
        "text": case.query_text or "",
        "match_mode": "retrieval_only_text",
        "max_minutes": case.time_minutes,
        "dataset_id": case.dataset_id,
        "match_any": None,
        "rank_pantry": [],
    }


def _current_search(engine: Any, built: dict[str, Any], limit: int) -> list[dict[str, Any]]:
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


def _variant_search(
    engine: Any, built: dict[str, Any], limit: int, *, mode: str
) -> list[dict[str, Any]]:
    """Copied SELECT from repository._search with only ORDER BY changed.

    ``mode`` is "title_first" or "title_boost". WHERE filters, params and
    tie-breaks match production; only the ranking expression differs (H6,
    2026-10-08: experiment change lives here, never in repository.py).
    """

    from sqlalchemy import text as sql_text

    from culinary_copilot.recipes.normalize import canonical

    if mode not in ("title_first", "title_boost"):
        raise ValueError(f"unknown experiment mode {mode!r}")
    query_text: str = built["text"]
    max_minutes: float | None = built["max_minutes"]
    dataset_id: str | None = built["dataset_id"]
    match_any = [canonical(i) for i in (built["match_any"] or []) if str(i).strip()]
    pantry = [canonical(i) for i in (built["rank_pantry"] or []) if str(i).strip()]
    dataset_filter = "AND dataset_id=:dataset" if dataset_id is not None else ""
    params: dict[str, Any] = {
        "query": query_text,
        "limit": limit,
        "ingredients": [],
        "minutes": max_minutes,
        "match_any": match_any,
        "pantry": pantry,
    }
    if dataset_id is not None:
        params["dataset"] = dataset_id
    # Pantry-overlap (ingredient discovery) eligibility is unchanged in both
    # experiments; title_boost additionally leaves its ranking unchanged.
    if match_any:
        select_extra = """,
                    (SELECT count(*) FROM unnest(ingredient_names) n
                     WHERE n = ANY(CAST(:match_any AS text[]))) AS pantry_overlap"""
        match_clause = "ingredient_names && CAST(:match_any AS text[])"
        if mode == "title_first":
            order_clause = """ORDER BY pantry_overlap DESC,
                ((to_tsvector('english', title)
                  @@ plainto_tsquery('english', :query))::int) DESC,
                score DESC, dataset_id, source_id"""
        else:
            order_clause = "ORDER BY pantry_overlap DESC, score DESC, dataset_id, source_id"
    else:
        select_extra = ""
        match_clause = "search_vector @@ plainto_tsquery('english', :query)"
        title_match = "((to_tsvector('english', title) @@ plainto_tsquery('english', :query))::int)"
        title_score = (
            "ts_rank_cd(to_tsvector('english', coalesce(title, ''))"
            ", plainto_tsquery('english', :query))"
        )
        base_score = "ts_rank_cd(search_vector, plainto_tsquery('english', :query))"
        if pantry:
            pantry_expr = f"""({base_score}
                      + CAST(:pantry_weight AS double precision)
                      * COALESCE((SELECT max(ts_rank_cd(
                            search_vector, plainto_tsquery('english', term)))
                        FROM unnest(CAST(:pantry AS text[])) AS term), 0))"""
            params["pantry_weight"] = 0.25  # PANTRY_RANK_WEIGHT in repository.py
        else:
            pantry_expr = base_score
        if mode == "title_first":
            order_clause = f"ORDER BY {title_match} DESC, {pantry_expr} DESC, dataset_id, source_id"
        else:
            # Bounded title boost. Pantry-overlap mode is handled above and
            # stays unchanged, so this covers dish mode and all free text
            # (H6, 2026-10-08; free text carries no dish-intent signal).
            order_clause = (
                f"ORDER BY ({pantry_expr} + {TITLE_BOOST_WEIGHT} * {title_score})"
                " DESC, dataset_id, source_id"
            )
    with engine.connect() as conn:
        rows = conn.execute(
            sql_text(f"""
            SELECT dataset_id, source_id, title, total_minutes,
                    ts_rank_cd(search_vector, plainto_tsquery('english', :query)) AS score
                    {select_extra}
            FROM recipes
            WHERE {match_clause}
              {dataset_filter}
              AND ingredient_names @> CAST(:ingredients AS text[])
              AND (CAST(:minutes AS double precision) IS NULL
                    OR (total_minutes > 0 AND total_minutes <= :minutes
                        AND total_minutes <> 'Infinity'::float8
                        AND total_minutes = total_minutes))
            {order_clause} LIMIT :limit
        """),
            params,
        )
        return [dict(row) for row in rows.mappings()]


def _score_top5(
    top5: list[dict[str, Any]],
    case_id: str,
    recorded: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    judged = 0
    relevant1 = 0
    relevant2 = 0
    first1: int | None = None
    first2: int | None = None
    unjudged = 0
    malformed = 0
    hits: list[dict[str, Any]] = []
    for rank, row in enumerate(top5, 1):
        key = (case_id, str(row["dataset_id"]), str(row["source_id"]))
        label = recorded.get(key)
        grade = label.get("topical_grade") if label else None
        if grade is None:
            unjudged += 1
        else:
            judged += 1
            if int(grade) >= 1:
                relevant1 += 1
                if first1 is None:
                    first1 = rank
            if int(grade) == 2:
                relevant2 += 1
                if first2 is None:
                    first2 = rank
        if _is_malformed(row):
            malformed += 1
        hits.append(
            {
                "dataset_id": row["dataset_id"],
                "source_id": row["source_id"],
                "title": row.get("title"),
                "rank": rank,
                "recorded_grade": grade,
                "joint": joint_verdict(label) if label and grade is not None else "unjudged",
                "malformed_h5": _is_malformed(row),
            }
        )
    return {
        "judged": judged,
        "unjudged": unjudged,
        "malformed": malformed,
        "recall1": 1.0 if relevant1 else (0.0 if judged else None),
        "rr1": 1.0 / first1 if first1 else (0.0 if judged else None),
        "recall2": 1.0 if relevant2 else (0.0 if judged else None),
        "rr2": 1.0 / first2 if first2 else (0.0 if judged else None),
        "grade2_hits": relevant2,
        "grade1_hits": relevant1 - relevant2,
        "hits": hits,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        action="append",
        default=[],
        help="Case file (repeatable). Default: frozen Phase 1 retrieval cases.",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--extra-judgments",
        default=None,
        help="Optional additional judgment file (H6 AI-prepared layer) for "
        "expanded-metrics scoring; original labels are never modified.",
    )
    args = parser.parse_args()

    from sqlalchemy import create_engine

    from culinary_copilot.config import Settings

    case_files: list[Path] = [Path(p) for p in args.cases] if args.cases else []
    if not case_files:
        case_files.append(REPO / "evals" / "cases" / "phase1_retrieval.json")
    cases: list[Any] = []
    for path in case_files:
        loaded = load_case_file(str(path))
        cases.extend(loaded.cases)
    corrections = json.loads((PHASE1 / "checkpoint1_corrections.json").read_text())
    recorded = {
        (label["case_id"], label["dataset_id"], label["source_id"]): label
        for label in corrections.get("labels", [])
    }
    expanded: dict[tuple[str, str, str], dict[str, Any]] | None = None
    if args.extra_judgments:
        layer = json.loads(Path(args.extra_judgments).read_text(encoding="utf-8"))
        expanded = dict(recorded)
        for label in layer.get("labels", []):
            expanded[(label["case_id"], label["dataset_id"], label["source_id"])] = label

    engine = create_engine(
        Settings().database_url.get_secret_value(),
        connect_args={"options": "-c default_transaction_read_only=on"},
    )
    try:
        case_results: list[dict[str, Any]] = []
        for case in cases:
            built = _build_query(case)
            limit = FETCH_N
            cur = _current_search(engine, built, limit)
            tf = _variant_search(engine, built, limit, mode="title_first")
            tb = _variant_search(engine, built, limit, mode="title_boost")
            per_rank: dict[str, Any] = {}
            for name, rows in (("current", cur), ("title_first", tf), ("title_boost", tb)):
                top5 = rows[:5]
                # Quarantined top-5: skip known-malformed H5 rows, fill from
                # the fetched headroom (H6, 2026-10-08).
                no_mal = [r for r in rows if not _is_malformed(r)][:5]
                scored = _score_top5(top5, case.case_id, recorded)
                scored_q = _score_top5(no_mal, case.case_id, recorded)
                entry: dict[str, Any] = {
                    "top5": [
                        {
                            "dataset_id": r.get("dataset_id"),
                            "source_id": r.get("source_id"),
                            "title": r.get("title"),
                        }
                        for r in top5
                    ],
                    "top5_quarantined": [
                        {
                            "dataset_id": r.get("dataset_id"),
                            "source_id": r.get("source_id"),
                            "title": r.get("title"),
                        }
                        for r in no_mal
                    ],
                    "vs_recorded": scored,
                    "vs_recorded_quarantined": scored_q,
                }
                if expanded is not None:
                    entry["vs_expanded"] = _score_top5(top5, case.case_id, expanded)
                    entry["vs_expanded_quarantined"] = _score_top5(no_mal, case.case_id, expanded)
                per_rank[name] = entry
            cur_ids = [(h["dataset_id"], h["source_id"]) for h in per_rank["current"]["top5"]]
            tf_ids = [(h["dataset_id"], h["source_id"]) for h in per_rank["title_first"]["top5"]]
            tb_ids = [(h["dataset_id"], h["source_id"]) for h in per_rank["title_boost"]["top5"]]
            case_results.append(
                {
                    "case_id": case.case_id,
                    "split": case.split,
                    "kind": case.kind,
                    "query_text": built["text"],
                    "match_mode": built["match_mode"],
                    "rankings": per_rank,
                    "top5_changed_title_first": cur_ids != tf_ids,
                    "top5_changed_title_boost": cur_ids != tb_ids,
                    "newly_surfaced_title_first": [
                        {"dataset_id": d, "source_id": s}
                        for d, s in tf_ids
                        if (d, s) not in cur_ids
                    ],
                    "newly_surfaced_title_boost": [
                        {"dataset_id": d, "source_id": s}
                        for d, s in tb_ids
                        if (d, s) not in cur_ids
                    ],
                }
            )

        def aggregate(
            key: str, score: str = "vs_recorded", field: str = "recall1"
        ) -> dict[str, Any]:
            vals: list[float] = []
            for c in case_results:
                v = c["rankings"][key][score].get(field)
                if v is not None:
                    vals.append(float(v))
            return {
                "n_scored": len(vals),
                "n_total": len(case_results),
                field: sum(vals) / len(vals) if vals else 0.0,
            }

        def paired(key: str, score: str, field: str) -> dict[str, Any]:
            base = [
                c for c in case_results if c["rankings"]["current"][score].get(field) is not None
            ]
            vals = [float(c["rankings"][key][score].get(field) or 0.0) for c in base]
            no_judged = sum(1 for c in base if c["rankings"][key][score].get(field) is None)
            return {
                "n_cases": len(vals),
                "top5_without_judged_hit": no_judged,
                field: sum(vals) / len(vals) if vals else 0.0,
            }

        def paired_block(score: str) -> dict[str, Any]:
            return {
                name: {
                    "recall_at_5_g1": paired(name, score, "recall1"),
                    "mrr_at_5_g1": paired(name, score, "rr1"),
                    "recall_at_5_g2": paired(name, score, "recall2"),
                    "mrr_at_5_g2": paired(name, score, "rr2"),
                }
                for name in ("current", "title_first", "title_boost")
            }

        summary: dict[str, Any] = {
            "paired_vs_recorded": paired_block("vs_recorded"),
            "metrics_vs_recorded": {
                name: {
                    "recall_at_5_g1": aggregate(name, "vs_recorded", "recall1"),
                    "mrr_at_5_g1": aggregate(name, "vs_recorded", "rr1"),
                    "recall_at_5_g2": aggregate(name, "vs_recorded", "recall2"),
                    "mrr_at_5_g2": aggregate(name, "vs_recorded", "rr2"),
                    "recall_at_5_g1_quarantined": aggregate(
                        name, "vs_recorded_quarantined", "recall1"
                    ),
                    "mrr_at_5_g1_quarantined": aggregate(name, "vs_recorded_quarantined", "rr1"),
                }
                for name in ("current", "title_first", "title_boost")
            },
            "malformed_in_top5": {
                name: sum(c["rankings"][name]["vs_recorded"]["malformed"] for c in case_results)
                for name in ("current", "title_first", "title_boost")
            },
            "cases_top5_changed_title_first": sorted(
                c["case_id"] for c in case_results if c["top5_changed_title_first"]
            ),
            "cases_top5_changed_title_boost": sorted(
                c["case_id"] for c in case_results if c["top5_changed_title_boost"]
            ),
        }
        if expanded is not None:
            summary["paired_vs_expanded"] = paired_block("vs_expanded")
            summary["unjudged_in_top5_vs_expanded"] = {
                name: sum(c["rankings"][name]["vs_expanded"]["unjudged"] for c in case_results)
                for name in ("current", "title_first", "title_boost")
            }
            summary["metrics_vs_expanded"] = {
                name: {
                    "recall_at_5_g1": aggregate(name, "vs_expanded", "recall1"),
                    "mrr_at_5_g1": aggregate(name, "vs_expanded", "rr1"),
                    "recall_at_5_g2": aggregate(name, "vs_expanded", "recall2"),
                    "mrr_at_5_g2": aggregate(name, "vs_expanded", "rr2"),
                }
                for name in ("current", "title_first", "title_boost")
            }
        report = {
            "harness": "h6-ranking-compare-v1",
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "status": "AI-prepared experiment output; scoring against recorded "
            "labels only unless --extra-judgments is given. Not production "
            "guidance; proposal inputs only.",
            "title_boost_weight": TITLE_BOOST_WEIGHT,
            "fetch_n": FETCH_N,
            "case_files": [_rel(p) for p in case_files],
            "case_files_sha256": {_rel(p): _sha256(Path(p)) for p in case_files},
            "labels_file": _rel(PHASE1 / "checkpoint1_corrections.json"),
            "labels_file_sha256": _sha256(PHASE1 / "checkpoint1_corrections.json"),
            "extra_judgments": args.extra_judgments,
            "extra_judgments_sha256": (
                _sha256(Path(args.extra_judgments)) if args.extra_judgments else None
            ),
            "summary": summary,
            "cases": case_results,
        }
    finally:
        engine.dispose()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    changed = summary["cases_top5_changed_title_first"]
    mal = summary["malformed_in_top5"]
    print(f"cases={len(case_results)} changed_title_first={len(changed)} malformed={mal}")
    print(f"changed: {changed}")
    for name in ("current", "title_first", "title_boost"):
        m = summary["metrics_vs_recorded"][name]
        print(
            f"{name}: recall_g1={m['recall_at_5_g1']['recall1']:.3f} "
            f"mrr_g1={m['mrr_at_5_g1']['rr1']:.3f} "
            f"recall_g2={m['recall_at_5_g2']['recall2']:.3f} "
            f"mrr_g2={m['mrr_at_5_g2']['rr2']:.3f}"
        )
    for name in ("current", "title_first", "title_boost"):
        m = summary["paired_vs_recorded"][name]
        print(
            f"paired {name}: n={m['recall_at_5_g1']['n_cases']} "
            f"no_judged_hit={m['recall_at_5_g1']['top5_without_judged_hit']} "
            f"recall_g1={m['recall_at_5_g1']['recall1']:.3f} "
            f"mrr_g1={m['mrr_at_5_g1']['rr1']:.3f} "
            f"recall_g2={m['recall_at_5_g2']['recall2']:.3f} "
            f"mrr_g2={m['mrr_at_5_g2']['rr2']:.3f}"
        )
    print(f"out={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
