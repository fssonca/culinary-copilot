"""Corrected baseline scoring from saved outputs (no DB, no tuning, no label edits).

Eligibility comes from the frozen reference judgments, independently of
retrieval output: every case with at least one labeled relevant recipe
(grade >= 1) is scored, and a retrieval miss stays in the denominator.
Reads the saved post-rebuild retrieval outputs plus the frozen cases and
labels; writes ``baseline_fulltext_corrected.json``.

Usage (from repo root):
    uv run python scripts/retrieval_eval/rescore_baseline.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "retrieval_eval"))

from case_schema import load_case_file  # noqa: E402
from metrics import Pair, joint_verdict, recall_at_k, reciprocal_rank  # noqa: E402

PHASE1 = REPO / "evals" / "results" / "phase1"
OUT = PHASE1 / "baseline_fulltext_corrected.json"
# Frozen declared cutoff (corrections metrics_intended): grade 2 is the
# primary baseline; grade >= 1 is a sensitivity analysis. No prior decision
# changed the threshold.
PRIMARY_CUTOFF = 2
SENSITIVITY_CUTOFF = 1


def main() -> int:
    data = load_case_file(str(REPO / "evals/cases/phase1_retrieval.json"))
    saved = json.loads((PHASE1 / "baseline_fulltext_post_rebuild.json").read_text())
    corrections = json.loads((PHASE1 / "checkpoint1_corrections.json").read_text())
    labels: list[dict[str, Any]] = corrections.get("labels", [])
    by_case: dict[str, Any] = {c.case_id: c for c in data.cases}
    retrieved: dict[str, list[Pair]] = {}
    for entry in saved["cases"]:
        retrieved[entry["case_id"]] = [
            (str(h["dataset_id"]), str(h["source_id"])) for h in entry["hits"][:5]
        ]

    def score_cutoff(cutoff: int) -> dict[str, Any]:
        relevant: dict[str, list[Pair]] = {}
        negative_only: list[str] = []
        insufficient: list[str] = []
        for case in data.cases:
            grades = [
                label.get("topical_grade") for label in labels if label["case_id"] == case.case_id
            ]
            judged = [g for g in grades if g is not None]
            if any(g >= cutoff for g in judged):
                relevant[case.case_id] = [
                    (str(label["dataset_id"]), str(label["source_id"]))
                    for label in labels
                    if label["case_id"] == case.case_id
                    and (label.get("topical_grade") or 0) >= cutoff
                ]
            elif judged:
                negative_only.append(case.case_id)
            else:
                insufficient.append(case.case_id)

        # Shared intents score once: union relevant sets and retrieved sets.
        units: dict[str, list[str]] = {}
        for case_id in relevant:
            group = by_case[case_id].aggregate_group
            units.setdefault(f"group:{group}" if group else f"case:{case_id}", []).append(case_id)

        rows: list[dict[str, Any]] = []
        for unit, members in sorted(units.items()):
            rel: list[Pair] = []
            for member in members:
                rel.extend(relevant[member])
            rel = list(dict.fromkeys(rel))
            ret: list[Pair] = []
            for member in members:
                ret.extend(retrieved.get(member, []))
            ret = list(dict.fromkeys(ret))
            first = members[0]
            rows.append(
                {
                    "unit": unit,
                    "cases": members,
                    "split": by_case[first].split,
                    "category": by_case[first].category,
                    "relevant_labeled": len(rel),
                    "retrieved_relevant": sum(1 for p in rel if p in ret[:5]),
                    "recall_at_5": recall_at_k(rel, ret),
                    "mrr_at_5": reciprocal_rank(rel, ret),
                }
            )
        scored = [r for r in rows if r["recall_at_5"] is not None]

        # Joint suitability, same independence principle. The recipe-level
        # profile counts frozen labels (one per case-context judgment, so the
        # same recipe relevant to two cases counts twice). Retrieval joint
        # recall is case-aware: a suitable-relevant (case, recipe) pair
        # counts only when retrieved in that case's top-5.
        profile = {"suitable": 0, "unknown": 0, "unsuitable": 0, "unjudged": 0}
        suitable_labeled = 0
        suitable_retrieved = 0
        for label in labels:
            if (label.get("topical_grade") or 0) >= cutoff:
                verdict = joint_verdict(label)
                profile[verdict] = profile.get(verdict, 0) + 1
                if verdict == "suitable":
                    suitable_labeled += 1
                    pair = (str(label["dataset_id"]), str(label["source_id"]))
                    if pair in retrieved.get(str(label["case_id"]), [])[:5]:
                        suitable_retrieved += 1
        joint = {
            "relevant_labels": sum(profile.values()),
            **profile,
            "suitable_relevant_retrieved_in_top5": suitable_retrieved,
            "suitable_relevant_labeled": suitable_labeled,
        }

        def mean(key: str) -> float | None:
            vals = [r[key] for r in scored if r[key] is not None]
            return sum(vals) / len(vals) if vals else None

        def split(name: str) -> dict[str, Any]:
            members = [r for r in scored if r["split"] == name]
            return {
                "units_scored": len(members),
                "recall_at_5": sum(r["recall_at_5"] for r in members) / len(members)
                if members
                else None,
                "note": "held-out numbers must not drive iterative changes",
            }

        return {
            "cutoff": f"topical_grade >= {cutoff}",
            "relevant_units": len(scored),
            "judged_only_negative": sorted(negative_only),
            "insufficient_judgments": sorted(insufficient),
            "recall_at_5": mean("recall_at_5"),
            "mrr_at_5": mean("mrr_at_5"),
            "denominator": f"{len(scored)} relevant-labeled units",
            "joint": joint,
            "joint_denominators": {
                "recipe_profile": (
                    f"{sum(profile.values())} relevant case-context labels "
                    "(retrieval-independent; one per case/recipe judgment)"
                ),
                "retrieval_joint_recall": (
                    f"{suitable_retrieved}/{suitable_labeled} suitable-relevant "
                    "case/recipe pairs retrieved in their case top-5"
                ),
            },
            "by_split": {name: split(name) for name in ("development", "held_out")},
            "per_case_table": [
                {
                    "unit": r["unit"],
                    "cases": r["cases"],
                    "split": r["split"],
                    "category": r["category"],
                    "numerator": r["retrieved_relevant"],
                    "denominator": r["relevant_labeled"],
                    "recall_at_5": r["recall_at_5"],
                    "mrr_at_5": r["mrr_at_5"],
                }
                for r in rows
            ],
        }

    strict = score_cutoff(PRIMARY_CUTOFF)
    broad = score_cutoff(SENSITIVITY_CUTOFF)

    report = {
        "baseline": "official-fulltext-post-rebuild-corrected",
        "supersedes": "baseline_fulltext_post_rebuild (selection-biased scoring)",
        "scoring_defect": (
            "The superseded report scored only cases with a judged production top-5 hit "
            "(23/52), dropping eligible cases with zero relevant retrieved. Corrected "
            "eligibility comes from frozen reference judgments; misses score 0 and stay "
            "in the denominator."
        ),
        "threshold_history": (
            "Frozen declaration (checkpoint1_corrections.json metrics_intended): topical "
            "Recall@5/MRR at topical_grade 2, joint as grade-2 AND suitable. No prior "
            "decision changed it: the corrected report's first issue used grade >= 1 "
            "without authorization. Grade 2 is therefore the primary baseline; grade >= 1 "
            "is a sensitivity analysis."
        ),
        "id_matching": "exact (dataset_id, source_id) pairs; no cross-dataset source_id matching",
        "no_match_note": "No corpus absence was independently established. Expected-empty "
        "intent cases (expect_hits=false: DEV-21, DEV-22, DEV-23, HELD-10, HELD-11, "
        "HELD-20) are listed, not verified; all sit in insufficient_judgments.",
        "primary": {"threshold": "strict relevance (grade 2, frozen declared cutoff)", **strict},
        "sensitivity": {"threshold": "broad relevance (grade >= 1)", **broad},
        "not_applicable_note": (
            "not_applicable constraints never fail a case: with no applicable constraint "
            "the joint verdict falls back to topical acceptability, so recorded "
            "suitable/not_applicable both resolve suitable while unknown stays unknown."
        ),
        "saved_outputs_reused": {
            "retrieval": "baseline_fulltext_post_rebuild.json (frozen post-rebuild top-5s)",
            "latency_violations_coverage": "unchanged from the saved outputs; not recomputed here",
        },
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"strict units={strict['relevant_units']} recall5={strict['recall_at_5']:.3f} "
        f"mrr5={strict['mrr_at_5']:.3f} | broad units={broad['relevant_units']} "
        f"recall5={broad['recall_at_5']:.3f} mrr5={broad['mrr_at_5']:.3f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
