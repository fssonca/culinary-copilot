"""Deterministically assemble the Phase 1 v2 review packet.

Recorded AI judgments are joined in from the corrections file, never
regenerated: rerunning this script preserves judgments and provenance.
Candidate discovery reruns the current repository code read-only against
the application database; new or unmatched candidates are marked
``unlabelled``, never graded.

Usage (from repo root):
    uv run python scripts/retrieval_eval/assemble_packet.py \\
        --cases evals/cases/phase1_retrieval.json \\
        --corrections evals/results/phase1/checkpoint1_corrections.json \\
        --out evals/results/phase1/review_packet_v2.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "retrieval_eval"))
sys.path.insert(0, str(REPO / "src"))

from case_schema import Phase1Case, load_case_file  # noqa: E402

DISCOVERY_METHOD = (
    "fixed-code repository search top-15 (dish-eligibility plus pantry "
    "ranking boost, or pantry-overlap matching) plus a read-only title "
    "ILIKE scan top-10 for dish/query heads; merged, deduped, capped at 20"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _revision() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=REPO
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _fingerprint(engine: Any) -> dict[str, Any]:
    from sqlalchemy import text

    with engine.connect() as conn:
        total = conn.execute(text("select count(*) from recipes")).scalar_one()
        by_dataset = [
            [row[0], row[1]]
            for row in conn.execute(
                text("select dataset_id, count(*) from recipes group by 1 order by 1")
            ).all()
        ]
        imports = [
            [row[0], row[1], row[2]]
            for row in conn.execute(
                text(
                    "select dataset_id, revision, normalizer_version "
                    "from recipe_imports order by dataset_id"
                )
            ).all()
        ]
    return {"total": total, "by_dataset": by_dataset, "imports": imports}


def _discover(engine: Any, case: Phase1Case) -> list[dict[str, Any]]:
    from sqlalchemy import text

    from culinary_copilot.recipes.repository import (
        get_recipe,
        search_all,
        search_recipes,
    )
    from culinary_copilot.retrieval.query import (
        MATCH_DISH,
        map_request_to_query,
    )
    from culinary_copilot.services.answers import init_state

    pool: dict[tuple[str, str], dict[str, Any]] = {}

    def add(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            key = (str(row["dataset_id"]), str(row["source_id"]))
            pool.setdefault(key, row)

    if case.kind == "integration":
        request: dict[str, Any] = {"ingredients": list(case.ingredients)}
        if case.time_minutes is not None:
            request["time_minutes"] = case.time_minutes
        if case.dietary_constraints:
            request["dietary_constraints"] = list(case.dietary_constraints)
        if case.equipment:
            request["equipment"] = list(case.equipment)
        state = init_state(request_id=f"asm-{case.case_id}", request=request, dish=case.dish)
        query = map_request_to_query(state, dataset_id=case.dataset_id)
        search = search_recipes if case.dataset_id else search_all
        kwargs: dict[str, Any] = {"dataset_id": case.dataset_id} if case.dataset_id else {}
        if query.match_mode == MATCH_DISH:
            add(
                search(
                    engine,
                    query.query_text or "recipe",
                    max_minutes=query.max_minutes,
                    limit=15,
                    rank_pantry_terms=query.rank_pantry_terms,
                    **kwargs,
                )
            )
        else:
            add(
                search(
                    engine,
                    query.query_text or "recipe",
                    max_minutes=query.max_minutes,
                    limit=15,
                    match_any_ingredients=query.match_any_ingredients,
                    **kwargs,
                )
            )
        head = case.dish or ""
    else:
        head = case.query_text or ""
        search = search_recipes if case.dataset_id else search_all
        slice_kwargs: dict[str, Any] = {"dataset_id": case.dataset_id} if case.dataset_id else {}
        add(
            search(
                engine,
                head,
                max_minutes=case.time_minutes,
                limit=15,
                **slice_kwargs,
            )
        )
    tokens = [t for t in head.split() if len(t) > 2][:3]
    if tokens:
        like = "%" + "%".join(tokens) + "%"
        params: dict[str, Any] = {"like": like, "limit": 10}
        dataset_filter = ""
        if case.dataset_id:
            dataset_filter = "AND dataset_id=:dataset"
            params["dataset"] = case.dataset_id
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT dataset_id, source_id, title, total_minutes, servings, "
                    "NULL AS score FROM recipes "
                    f"WHERE title ILIKE :like {dataset_filter} "
                    "ORDER BY dataset_id, source_id LIMIT :limit"
                ),
                params,
            ).mappings()
            add([dict(r) for r in rows])
    candidates: list[dict[str, Any]] = []
    for rank, ((dataset_id, source_id), row) in enumerate(list(pool.items())[:20], 1):
        try:
            doc = get_recipe(engine, source_id, dataset_id=dataset_id)
        except ValueError:
            doc = None
        excerpt = ""
        if doc is not None:
            desc = doc.get("description")
            if isinstance(desc, str) and desc.strip():
                excerpt = " ".join(desc.split())[:220]
            steps = doc.get("instructions") or []
            bits = [excerpt] if excerpt else []
            for step in steps[:2]:
                if isinstance(step, str) and step.strip():
                    bits.append(" ".join(step.split())[:220])
            excerpt = " | ".join(bits)[:500]
        durations = doc.get("durations_minutes") if isinstance(doc, dict) else None
        candidates.append(
            {
                "dataset_id": dataset_id,
                "source_id": source_id,
                "title": (doc or {}).get("title") or row.get("title"),
                "pool_rank": rank,
                "total_minutes_reported": (durations or {}).get("TotalTime"),
                "servings": (doc or {}).get("servings"),
                "excerpt": excerpt,
            }
        )
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="evals/cases/phase1_retrieval.json")
    parser.add_argument(
        "--corrections", default="evals/results/phase1/checkpoint1_corrections.json"
    )
    parser.add_argument("--out", default="evals/results/phase1/review_packet_v2.json")
    args = parser.parse_args()

    cases_path = REPO / args.cases
    corrections_path = REPO / args.corrections
    data = load_case_file(str(cases_path))
    corrections = json.loads(corrections_path.read_text(encoding="utf-8"))
    recorded = {
        (label["case_id"], label["dataset_id"], label["source_id"]): label
        for label in corrections.get("labels", [])
    }

    from sqlalchemy import create_engine

    from culinary_copilot.config import Settings

    engine = create_engine(Settings().database_url.get_secret_value())
    try:
        fingerprint = _fingerprint(engine)
        entries: list[dict[str, Any]] = []
        for case in data.cases:
            candidates = _discover(engine, case)
            for candidate in candidates:
                key = (case.case_id, candidate["dataset_id"], candidate["source_id"])
                label = recorded.get(key)
                if label is None:
                    candidate["label_status"] = "unlabelled"
                else:
                    candidate["label_status"] = "ai_proposed"
                    candidate["recorded"] = {
                        "topical_grade": label.get("topical_grade"),
                        "constraint_checks": label.get("constraint_checks"),
                        "suitability": label.get("suitability"),
                        "rationale": label.get("rationale"),
                        "reviewer_type": label.get("reviewer_type"),
                    }
            entries.append(
                {
                    "case_id": case.case_id,
                    "split": case.split,
                    "kind": case.kind,
                    "category": case.category,
                    "dish": case.dish,
                    "ingredients": list(case.ingredients),
                    "dietary_constraints": list(case.dietary_constraints),
                    "equipment": list(case.equipment),
                    "time_minutes": case.time_minutes,
                    "query_text": case.query_text,
                    "dataset_scope": case.dataset_id or "combined",
                    "limit": case.limit,
                    "expect_hits": case.expect_hits,
                    "relevance_expectation": case.relevance_expectation,
                    "behavioral": list(case.behavioral),
                    "aggregate_group": case.aggregate_group,
                    "notes": case.notes,
                    "pool_size": len(candidates),
                    "candidates": candidates,
                }
            )
    finally:
        engine.dispose()

    packet = {
        "packet_version": "phase1-v2",
        "label_schema": corrections.get("label_schema"),
        "label_status": "Recorded grades are AI-proposed until human-reviewed. "
        "Unlabelled candidates have no grade. No definitive score is published.",
        "provenance": {
            "cases_file": args.cases,
            "cases_sha256": _sha(cases_path),
            "cases_version": data.version,
            "corrections_file": args.corrections,
            "corrections_sha256": _sha(corrections_path),
            "corrections_version": corrections.get("corrections_version"),
            "corpus_fingerprint": fingerprint,
            "code_revision": _revision(),
            "command": "uv run python scripts/retrieval_eval/assemble_packet.py "
            f"--cases {args.cases} --corrections {args.corrections} --out {args.out}",
            "discovery_method": DISCOVERY_METHOD,
        },
        "cases": entries,
    }
    out_path = REPO / args.out
    out_path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    labelled = sum(1 for e in entries for c in e["candidates"] if c["label_status"] != "unlabelled")
    total = sum(len(e["candidates"]) for e in entries)
    print(f"cases={len(entries)} candidates={total} recorded_labels_joined={labelled}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
