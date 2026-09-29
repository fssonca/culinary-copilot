#!/usr/bin/env python3
"""Re-score SAVED technique retrieval rankings under new labels.

Reads a saved rankings file (``baseline_fulltext.json`` or
``vector_run.json``) plus the current frozen cases, and recomputes
document-level HitRate@5 and MRR over the answerable cases only.
Coverage-gap cases are excluded from the denominators and reported
separately. No retrieval runs, no embedding calls: only rankings
already on disk are used. If a saved ranking lacks what the rescore
needs (retrieved doc lists), it stops with an error instead of
re-running retrieval.

Output is labeled ``"rescore"`` and written next to the input; the
original result files are never modified.
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
sys.path.insert(0, str(REPO_ROOT / "scripts" / "retrieval_eval"))

from metrics import reciprocal_rank  # noqa: E402


def _verify_freeze(payload: dict[str, Any]) -> None:
    body = {k: v for k, v in payload.items() if k != "freeze_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    if digest != payload.get("freeze_sha256"):
        raise ValueError("cases freeze hash mismatch")


def _score(relevant: list[str], retrieved: list[str]) -> tuple[bool, float | None]:
    relevant_pairs = [(d, d) for d in relevant]
    retrieved_pairs = [(d, d) for d in retrieved]
    return (
        any(pair in retrieved_pairs for pair in relevant_pairs),
        reciprocal_rank(relevant_pairs, retrieved_pairs, k=5),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rankings", required=True)
    parser.add_argument(
        "--cases",
        default=str(REPO_ROOT / "evals" / "technique_retrieval" / "cases.json"),
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    _verify_freeze(payload)
    saved = json.loads(Path(args.rankings).read_text(encoding="utf-8"))
    mode = saved.get("mode", "fulltext")
    by_case = {row["case_id"]: row for row in saved.get("rows", [])}
    rows: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for case in payload["cases"]:
        case_id = case["case_id"]
        if case.get("expected") == "coverage_gap":
            saved_row = by_case.get(case_id)
            if saved_row is None:
                print(f"error: saved rankings lack case {case_id}", file=sys.stderr)
                return 2
            gap: dict[str, Any] = {
                "case_id": case_id,
                "expected": "coverage_gap",
                "reason": case.get("coverage_gap_reason", ""),
            }
            if mode == "vector":
                for key in ("cutoff_0_66", "no_cutoff"):
                    branch = saved_row.get(key) or {}
                    if "retrieved_docs" not in branch:
                        print(
                            f"error: saved vector ranking for {case_id} lacks "
                            f"{key}.retrieved_docs; not re-running retrieval",
                            file=sys.stderr,
                        )
                        return 2
                    gap[f"retrieved_docs_{key}"] = list(branch["retrieved_docs"])
                gap["note"] = (
                    "per-retrieved-doc distances are not in the saved "
                    "rankings (only relevant-doc distances were stored); "
                    "not re-runnable without embedding calls"
                )
            else:
                if "retrieved_docs" not in saved_row:
                    print(
                        f"error: saved full-text ranking for {case_id} lacks "
                        "retrieved_docs; not re-running retrieval",
                        file=sys.stderr,
                    )
                    return 2
                gap["retrieved_docs"] = list(saved_row["retrieved_docs"])
            gaps.append(gap)
            continue
        saved_row = by_case.get(case_id)
        if saved_row is None:
            print(f"error: saved rankings lack case {case_id}", file=sys.stderr)
            return 2
        relevant = list(case["relevant_docs"])
        if mode == "vector":
            row: dict[str, Any] = {"case_id": case_id, "relevant_docs": relevant}
            for key in ("cutoff_0_66", "no_cutoff"):
                branch = saved_row.get(key) or {}
                if "retrieved_docs" not in branch:
                    print(
                        f"error: saved vector ranking for {case_id} lacks "
                        f"{key}.retrieved_docs; not re-running retrieval",
                        file=sys.stderr,
                    )
                    return 2
                hit, rr = _score(relevant, list(branch["retrieved_docs"]))
                row[key] = {
                    "hit_at_5": hit,
                    "reciprocal_rank": rr,
                    "retrieved_docs": list(branch["retrieved_docs"]),
                }
            rows.append(row)
        else:
            if "retrieved_docs" not in saved_row:
                print(
                    f"error: saved full-text ranking for {case_id} lacks "
                    "retrieved_docs; not re-running retrieval",
                    file=sys.stderr,
                )
                return 2
            hit, rr = _score(relevant, list(saved_row["retrieved_docs"]))
            rows.append(
                {
                    "case_id": case_id,
                    "hit_at_5": hit,
                    "reciprocal_rank": rr,
                    "retrieved_docs": list(saved_row["retrieved_docs"]),
                    "relevant_docs": relevant,
                }
            )

    def _aggregate(key: str | None) -> dict[str, float]:
        if key is None:
            hits = [r for r in rows]
            hits_n = sum(1 for r in hits if r["hit_at_5"])
            rrs = [r["reciprocal_rank"] for r in hits if r["reciprocal_rank"] is not None]
        else:
            hits_n = sum(1 for r in rows if r[key]["hit_at_5"])
            rrs = [r[key]["reciprocal_rank"] for r in rows if r[key]["reciprocal_rank"] is not None]
        return {
            "hit_rate_at_5": hits_n / len(rows) if rows else 0.0,
            "mrr": sum(rrs) / len(rrs) if rrs else 0.0,
        }

    if mode == "vector":
        summary = {
            "kind": "rescore",
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": "vector",
            "rankings": str(args.rankings),
            "cases": len(rows),
            "cutoff_0_66": _aggregate("cutoff_0_66"),
            "no_cutoff": _aggregate("no_cutoff"),
            "cases_sha256": payload["freeze_sha256"],
            "coverage_gap_cases": gaps,
            "rows": rows,
        }
        print(
            f"rescore vector over {len(rows)} answerable cases; cutoff HitRate@5: "
            f"{summary['cutoff_0_66']['hit_rate_at_5']:.3f} MRR: "
            f"{summary['cutoff_0_66']['mrr']:.3f}; no-cutoff HitRate@5: "
            f"{summary['no_cutoff']['hit_rate_at_5']:.3f} MRR: "
            f"{summary['no_cutoff']['mrr']:.3f}"
        )
    else:
        agg = _aggregate(None)
        summary = {
            "kind": "rescore",
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": "fulltext",
            "rankings": str(args.rankings),
            "cases": len(rows),
            "hit_rate_at_5": agg["hit_rate_at_5"],
            "mrr": agg["mrr"],
            "cases_sha256": payload["freeze_sha256"],
            "coverage_gap_cases": gaps,
            "rows": rows,
        }
        print(
            f"rescore fulltext over {len(rows)} answerable cases; HitRate@5: "
            f"{agg['hit_rate_at_5']:.3f}; MRR: {agg['mrr']:.3f}"
        )
    Path(args.out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"rescore: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
