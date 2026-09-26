"""Phase 6 development analysis: cutoff curve, selection, dev results.

Reads one raw run plus the frozen Phase 1 labels and the new Phase 6
development label layer, then applies the frozen cutoff selection rule
mechanically (see phase6_eval.select_cutoff). No tuning by any other
means. Outputs (ignored path): the full curve JSON. The freeze record is
written separately at STOP 2.

Usage (from repo root):
    uv run python scripts/retrieval_eval/analyze_phase6.py \\
        --raw data/phase6/dev_live_raw.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "retrieval_eval"))
sys.path.insert(0, str(REPO / "src"))

from phase6_eval import cutoff_grid, derive_configs, select_cutoff  # noqa: E402

FROZEN_LABELS = REPO / "evals" / "results" / "phase1" / "checkpoint1_corrections.json"
DEV_LAYER = REPO / "data" / "phase6" / "phase6_dev_labels_v1.json"


def _grade_map() -> dict[tuple[str, str, str], int]:
    grades: dict[tuple[str, str, str], int] = {}
    frozen = json.loads(FROZEN_LABELS.read_text(encoding="utf-8"))
    for label in frozen.get("labels", []):
        if label.get("topical_grade") is not None:
            grades[(str(label["case_id"]), str(label["dataset_id"]), str(label["source_id"]))] = (
                int(label["topical_grade"])
            )
    layer = json.loads(DEV_LAYER.read_text(encoding="utf-8"))
    for label in layer.get("labels", []):
        grades[(str(label["case_id"]), str(label["dataset_id"]), str(label["source_id"]))] = int(
            label["topical_grade"]
        )
    return grades


def _hit_mrr(
    top5: list[dict[str, Any]], case_id: str, grades: dict[tuple[str, str, str], int]
) -> tuple[bool, float]:
    for rank, row in enumerate(top5, 1):
        key = (case_id, str(row.get("dataset_id")), str(row.get("source_id")))
        if grades.get(key) == 2:
            return True, 1.0 / rank
    return False, 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--out", default="data/phase6/dev_curve.json")
    args = parser.parse_args()

    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    grades = _grade_map()
    grid = cutoff_grid()
    cases = raw["cases"]
    dev_scored = [c for c in cases if c.get("category") != "nonfood"]
    nonfood = [c for c in cases if c.get("category") == "nonfood"]
    hard = [c for c in cases if str(c.get("category", "")).startswith("hard_")]

    scored_ids: list[str] = []
    for case in dev_scored:
        case_id = str(case["case_id"])
        if case_id.startswith("P6D"):
            scored_ids.append(case_id)
        elif any(
            grades.get((case_id, str(r["dataset_id"]), str(r["source_id"]))) == 2
            for r in case["fulltext_top20"]
        ) or any(
            grades.get((case_id, str(r["dataset_id"]), str(r["source_id"]))) == 2
            for r in case["vector_top20"]
        ):
            scored_ids.append(case_id)
    scored = [c for c in dev_scored if str(c["case_id"]) in scored_ids]

    def rows(case: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        fulltext = [
            {"dataset_id": r["dataset_id"], "source_id": r["source_id"]}
            for r in case["fulltext_top20"]
        ]
        vector = [
            {"dataset_id": r["dataset_id"], "source_id": r["source_id"], "distance": r["distance"]}
            for r in case["vector_top20"]
        ]
        return fulltext, vector

    # Reference: grade-2 hard hits with no cutoff (what a cutoff can remove).
    ref_hard_hits: dict[str, list[str]] = {}
    for case in hard:
        fulltext, vector = rows(case)
        derived = derive_configs(fulltext, vector, cutoff=None)
        ref_hard_hits[str(case["case_id"])] = [
            f"{r['dataset_id']}\x00{r['source_id']}" for r in derived["hybrid_c"]
        ]

    curve: list[dict[str, Any]] = []
    for value in grid:
        abstentions = 0
        for case in nonfood:
            _, vector = rows(case)
            kept = [r for r in vector if float(r["distance"]) <= float(value)] if value else vector
            if not kept:
                abstentions += 1
        hits = 0
        mrrs: list[float] = []
        removed: dict[str, list[str]] = {}
        for case in scored:
            case_id = str(case["case_id"])
            fulltext, vector = rows(case)
            derived = derive_configs(fulltext, vector, cutoff=value)
            hit, mrr = _hit_mrr(derived["hybrid_c"], case_id, grades)
            hits += 1 if hit else 0
            mrrs.append(mrr)
            if case in hard:
                now = {f"{r['dataset_id']}\x00{r['source_id']}" for r in derived["hybrid_c"]}
                removed[case_id] = [i for i in ref_hard_hits[case_id] if i not in now]
        curve.append(
            {
                "cutoff": value,
                "nonfood_abstention": abstentions / len(nonfood) if nonfood else 0.0,
                "nonfood_abstentions": abstentions,
                "nonfood_total": len(nonfood),
                "hitrate": hits / len(scored) if scored else 0.0,
                "hits": hits,
                "scored_total": len(scored),
                "mrr": sum(mrrs) / len(mrrs) if mrrs else 0.0,
                "hard_removed_vs_none": removed,
            }
        )
    selection = select_cutoff(curve)
    chosen = selection["chosen"]["cutoff"]

    # Development results per configuration at the chosen cutoff + (a)/(b).
    configs = ["fulltext", "vector_a", "vector_c", "hybrid_a", "hybrid_b", "hybrid_c"]
    dev_table: dict[str, dict[str, Any]] = {}
    for name in configs:
        cutoff = chosen if name.endswith("_c") else None
        hits = 0
        config_mrrs: list[float] = []
        per_case: dict[str, dict[str, Any]] = {}
        for case in scored:
            case_id = str(case["case_id"])
            fulltext, vector = rows(case)
            derived = derive_configs(fulltext, vector, cutoff=cutoff)
            hit, mrr = _hit_mrr(derived[name], case_id, grades)
            hits += 1 if hit else 0
            config_mrrs.append(mrr)
            per_case[case_id] = {"hit": hit, "mrr": round(mrr, 4)}
        dev_table[name] = {
            "cutoff": cutoff,
            "hitrate_at_5": hits / len(scored) if scored else 0.0,
            "hits": hits,
            "scored_total": len(scored),
            "mrr_at_5": sum(config_mrrs) / len(config_mrrs) if config_mrrs else 0.0,
            "per_case": per_case,
        }
    out = {
        "analysis": "phase6-dev-cutoff-curve-v1",
        "raw": args.raw,
        "scored_case_ids": sorted(scored_ids),
        "curve": curve,
        "selection": selection,
        "dev_table": dev_table,
    }
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"scored={len(scored_ids)} nonfood={len(nonfood)}")
    print(f"chosen cutoff={chosen} constraint_met={selection['constraint_met']}")
    for entry in curve:
        print(
            f"cutoff={entry['cutoff']} abst={entry['nonfood_abstention']:.2f} "
            f"hit={entry['hitrate']:.3f} mrr={entry['mrr']:.3f}"
        )
    print(f"dev_table hitrates: { {k: round(v['hitrate_at_5'], 3) for k, v in dev_table.items()} }")
    print(f"out={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
