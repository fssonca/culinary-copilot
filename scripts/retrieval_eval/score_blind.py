"""Score the Phase 6 blind confirmation set (ignored working artifact).

Inputs (all hashes recorded in blind_run_provenance.json before mapping):
  - data/phase6/blind_derived.json (config tops; never raw derived_top5)
  - data/phase6/blind/blind_judgments_v1.json (judge of record: owner)
  - data/phase6/blind_violations.json (mechanical; judge notes never override)
  - expected behaviour per blind_key_final_v3.md section 5 (key wins).

No re-grading, no blind-file edits, no frozen-code changes. Writes
data/phase6/blind_scores.json and prints the section 7 decision with
per-case tables.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "retrieval_eval"))
sys.path.insert(0, str(REPO / "src"))

DERIVED = REPO / "data" / "phase6" / "blind_derived.json"
JUDGMENTS = REPO / "data" / "phase6" / "blind" / "blind_judgments_v1.json"
VIOLATIONS = REPO / "data" / "phase6" / "blind_violations.json"
PACKET = REPO / "data" / "phase6" / "blind_packet.json"
CASES = REPO / "data" / "phase6" / "blind" / "blind_cases_phase6_format.json"
OUT = REPO / "data" / "phase6" / "blind_scores.json"

CONFIGS = ("fulltext", "vector_a", "vector_c", "hybrid_a", "hybrid_b", "hybrid_c")

# Expected behaviour per blind_key_final_v3.md section 5 (key wins).
EXPECTED: dict[str, str] = {
    "BLIND-01": "should_retrieve",
    "BLIND-02": "should_retrieve",
    "BLIND-03": "should_abstain",
    "BLIND-04": "should_retrieve",
    "BLIND-05": "should_retrieve",
    "BLIND-06": "should_abstain",
    "BLIND-07": "should_retrieve",
    "BLIND-08": "should_retrieve",
    "BLIND-09": "should_retrieve",
    "BLIND-10": "should_retrieve",
    "BLIND-11": "should_retrieve",
    "BLIND-12": "should_retrieve",
    "BLIND-13": "should_retrieve",
    "BLIND-14": "should_retrieve",
    "BLIND-15": "corpus_dependent",
    "BLIND-16": "should_retrieve",
    "BLIND-17": "should_retrieve",
    "BLIND-18": "should_retrieve",
    "BLIND-19": "should_retrieve",
    "BLIND-20": "should_abstain",
}


def gain_of(grade: int, violated: bool) -> int:
    """Key section 4 nDCG gain: topic grade, or 0 with any violation."""
    return 0 if violated else grade


def dcg_at_5(gains: list[int]) -> float:
    """DCG over ranks 1..5; unfilled ranks contribute 0."""
    total = 0.0
    for rank, gain in enumerate(gains[:5], 1):
        total += gain / math.log2(rank + 1)
    return total


def ndcg_at_5(mode_gains: list[int], pool_gains: list[int]) -> float:
    """nDCG with the pooled ideal; 0 when IDCG is 0."""
    ideal = sorted(pool_gains, reverse=True)[:5]
    idcg = dcg_at_5(ideal)
    if idcg == 0:
        return 0.0
    return dcg_at_5(mode_gains) / idcg


def hit_and_mrr(qualifying_ranks: list[int]) -> tuple[bool, float]:
    """Hit plus MRR from the ranks of qualifying results in the top 5."""
    if not qualifying_ranks:
        return False, 0.0
    return True, 1.0 / min(qualifying_ranks)


def net_wins(candidate_hits: dict[str, bool], ft_hits: dict[str, bool]) -> int:
    """Net HitRate case wins vs full-text over the same scored cases."""
    wins = sum(1 for case, hit in candidate_hits.items() if hit and not ft_hits[case])
    losses = sum(1 for case, hit in candidate_hits.items() if ft_hits[case] and not hit)
    return wins - losses


def apply_decision(summary: dict[str, dict[str, float]]) -> dict[str, object]:
    """Frozen key section 7 rule, applied mechanically to (c) candidates."""
    verdicts: dict[str, dict[str, object]] = {}
    for name in ("vector_c", "hybrid_c"):
        cand = summary[name]
        full = summary["fulltext"]
        checks = {
            "net_wins_ge_2": cand["net_wins"] >= 2,
            "mrr_ge": cand["mrr"] >= full["mrr"],
            "ndcg_ge": cand["ndcg"] >= full["ndcg"],
            "zero_violations": cand["violations_total"] == 0,
            "abst_le_ft": cand["abstain_results_total"] <= full["abstain_results_total"],
        }
        verdicts[name] = {"passes": all(checks.values()), "checks": checks}
    passing = [n for n, v in verdicts.items() if v["passes"]]
    winner = "fulltext"
    if len(passing) == 1:
        winner = passing[0]
    elif len(passing) == 2:
        # Key section 7.4 order: HitRate, MRR, nDCG, CleanAbstention, -FP/query.
        keyed = {
            n: (
                summary[n]["hitrate"],
                summary[n]["mrr"],
                summary[n]["ndcg"],
                summary[n]["clean_abstention"],
                -summary[n]["fp_per_query"],
            )
            for n in passing
        }
        if keyed[passing[0]] == keyed[passing[1]]:
            winner = "fulltext"
        else:
            winner = max(passing, key=lambda n: keyed[n])
    return {"verdicts": verdicts, "winner": winner}


def main() -> int:
    derived = json.loads(DERIVED.read_text(encoding="utf-8"))
    judgments = json.loads(JUDGMENTS.read_text(encoding="utf-8"))
    violations = json.loads(VIOLATIONS.read_text(encoding="utf-8"))
    packet = json.loads(PACKET.read_text(encoding="utf-8"))
    cases = {c["case_id"]: c for c in json.loads(CASES.read_text(encoding="utf-8"))["cases"]}

    grades = {
        (j["case_id"], j["dataset_id"], j["source_id"]): int(j["topic_relevance"])
        for j in judgments["judgments"]
    }
    viols = {
        (v["case_id"], v["dataset_id"], v["source_id"]): (
            bool(v["time_violation"]),
            bool(v["dataset_violation"]),
        )
        for v in violations
    }
    pool_ids = {
        str(r["case_id"]): [(c["dataset_id"], c["source_id"]) for c in r["candidates"]]
        for r in packet["requests"]
    }

    def qualifying(case_id: str, dataset_id: str, source_id: str) -> bool:
        key = (case_id, dataset_id, source_id)
        return grades.get(key) == 2 and not any(viols.get(key, (False, False)))

    scored_ids: list[str] = []
    abstain_ids: list[str] = []
    for case_id, behavior in EXPECTED.items():
        if behavior == "should_retrieve":
            scored_ids.append(case_id)
        elif behavior == "should_abstain":
            abstain_ids.append(case_id)
        else:
            pool = pool_ids.get(case_id, [])
            if any(qualifying(case_id, ds, src) for ds, src in pool):
                scored_ids.append(case_id)
            else:
                abstain_ids.append(case_id)

    derived_by_case = {c["case_id"]: c for c in derived["cases"]}
    per_case: dict[str, dict[str, dict[str, Any]]] = {}
    summary: dict[str, dict[str, float]] = {}
    for name in CONFIGS:
        hits: dict[str, bool] = {}
        mrrs: dict[str, float] = {}
        recalls: dict[str, float] = {}
        ndcgs: dict[str, float] = {}
        for case_id in scored_ids:
            top = derived_by_case[case_id]["configs"][name]
            q_ranks = [
                rank for rank, (ds, src) in enumerate(top, 1) if qualifying(case_id, ds, src)
            ]
            hit, mrr = hit_and_mrr(q_ranks)
            hits[case_id] = hit
            mrrs[case_id] = mrr
            pool = pool_ids.get(case_id, [])
            pool_qual = sum(1 for ds, src in pool if qualifying(case_id, ds, src))
            mode_qual = sum(1 for ds, src in top if qualifying(case_id, ds, src))
            recalls[case_id] = (mode_qual / pool_qual) if pool_qual else 0.0
            mode_gains = [
                gain_of(
                    grades.get((case_id, ds, src), 0),
                    any(viols.get((case_id, ds, src), (False, False))),
                )
                for ds, src in top
            ]
            pool_gains = [
                gain_of(
                    grades.get((case_id, ds, src), 0),
                    any(viols.get((case_id, ds, src), (False, False))),
                )
                for ds, src in pool_ids.get(case_id, [])
            ]
            ndcgs[case_id] = ndcg_at_5(mode_gains, pool_gains)
            per_case.setdefault(case_id, {})[name] = {
                "hit": hit,
                "mrr": round(mrr, 4),
                "recall": round(recalls[case_id], 4),
                "ndcg": round(ndcgs[case_id], 4),
            }
        summary[name] = {
            "hitrate": sum(hits.values()) / len(scored_ids),
            "mrr": sum(mrrs.values()) / len(scored_ids),
            "recall": sum(recalls.values()) / len(scored_ids),
            "ndcg": sum(ndcgs.values()) / len(scored_ids),
        }
    ft_hits = {c: bool(per_case[c]["fulltext"]["hit"]) for c in scored_ids}
    for name in CONFIGS:
        cand_hits = {c: bool(per_case[c][name]["hit"]) for c in scored_ids}
        summary[name]["net_wins"] = float(net_wins(cand_hits, ft_hits))

    for name in CONFIGS:
        returned = 0
        clean = 0
        grade1 = 0
        time_v = 0
        ds_v = 0
        for case_id in abstain_ids:
            top = derived_by_case[case_id]["configs"][name]
            returned += len(top)
            if not top:
                clean += 1
            grade1 += sum(1 for ds, src in top if grades.get((case_id, ds, src), 0) == 1)
        for case in derived["cases"]:
            for ds, src in case["configs"][name]:
                t, d = viols.get((case["case_id"], ds, src), (False, False))
                time_v += 1 if t else 0
                ds_v += 1 if d else 0
        summary[name].update(
            {
                "clean_abstention": clean / len(abstain_ids),
                "fp_per_query": returned / len(abstain_ids),
                "nearmiss_per_query": grade1 / len(abstain_ids),
                "abstain_results_total": float(returned),
                "violations_total": float(time_v + ds_v),
                "time_violations": float(time_v),
                "dataset_violations": float(ds_v),
            }
        )

    decision = apply_decision(summary)
    kinds = {c["case_id"]: c.get("kind", "?") for c in cases.values()}
    slices: dict[str, dict[str, dict[str, float]]] = {}
    for slice_name in ("integration", "retrieval_only"):
        members = [c for c in scored_ids if kinds.get(c) == slice_name]
        table: dict[str, dict[str, float]] = {}
        for name in CONFIGS:
            table[name] = {
                "hitrate": sum(bool(per_case[c][name]["hit"]) for c in members) / len(members),
                "mrr": sum(float(per_case[c][name]["mrr"]) for c in members) / len(members),
                "ndcg": sum(float(per_case[c][name]["ndcg"]) for c in members) / len(members),
            }
        slices[slice_name] = table
    report = {
        "scores": "phase6-blind-scores-v1",
        "scored_case_ids": sorted(scored_ids),
        "abstention_case_ids": sorted(abstain_ids),
        "per_case": per_case,
        "summary": summary,
        "slices": slices,
        "decision": decision,
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"scored={len(scored_ids)} abstention={len(abstain_ids)}")
    for name in CONFIGS:
        s = summary[name]
        print(
            f"{name}: hit={s['hitrate']:.3f} mrr={s['mrr']:.3f} "
            f"recall(pooled)={s['recall']:.3f} ndcg={s['ndcg']:.3f} "
            f"netwins={s['net_wins']:.0f} viol={s['violations_total']:.0f} "
            f"(t={s['time_violations']:.0f},d={s['dataset_violations']:.0f}) "
            f"clean={s['clean_abstention']:.3f} fp/q={s['fp_per_query']:.2f} "
            f"near/q={s['nearmiss_per_query']:.2f} abstTot={s['abstain_results_total']:.0f}"
        )
    print(f"decision={decision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
