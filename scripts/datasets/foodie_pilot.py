"""Reproducible odunola/foodie pilot (Workstream 2B–2D).

Deterministic 150-record sample covering the full file (systematic stride,
not just the head). Writes ignored local artifacts under
``data/odunola-pilot/``; never touches the application database.

Usage (from repo root, after review)::

    uv run python scripts/datasets/foodie_pilot.py --size 150 --seed 42
    uv run python scripts/datasets/foodie_pilot.py --size 150 --seed 42 --csv PATH

Hand-selected edge cases (rows 1–4, shortest/longest) are evaluated
separately in the report so pilot quality rates stay interpretable.

Development tool: lives outside ``src/``; production modules never import
from ``scripts/``. Shared checksum/sampling helpers live in
``culinary_copilot.recipes.dataset_utils``.
"""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from huggingface_hub import hf_hub_download

from culinary_copilot.config import Settings
from culinary_copilot.recipes.adapters.base import foodie_source_id
from culinary_copilot.recipes.adapters.foodie import (
    FOODIE_ADAPTER_VERSION,
    FOODIE_DATASET,
    FOODIE_FILE,
    FOODIE_LICENSE,
    FOODIE_REVISION,
    FOODIE_SHA256,
    normalize_foodie_text,
)
from culinary_copilot.recipes.dataset_utils import sha256_file, systematic_sample

DEFAULT_SIZE = 150
DEFAULT_SEED = 42
EDGE_ROWS = [1, 2, 3, 4, 2761, 3083, 11893, 460, 663, 19566]


def run_pilot(csv_path: Path, output: Path, *, size: int, seed: int) -> dict[str, Any]:
    checksum = sha256_file(csv_path)
    if checksum != FOODIE_SHA256:
        raise ValueError(
            f"foodie checksum mismatch: got {checksum}, expected {FOODIE_SHA256}; "
            "review source and update the pin before piloting"
        )
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["texts"]:
            raise ValueError(f"unexpected foodie columns: {reader.fieldnames}")
        rows = list(reader)
    total = len(rows)
    selected = systematic_sample(total, size, seed)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    issue_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    amount_ok = unit_ok = qualitative = unknown_qty = 0
    ingredient_total = 0
    servings_known = durations_known = groups_seen = 0
    fingerprints: Counter[str] = Counter()
    for row_number in selected:
        texts = rows[row_number - 1]["texts"] or ""
        try:
            recipe = normalize_foodie_text(texts, row_number)
        except ValueError as exc:
            reason_counts[str(exc)] += 1
            rejected.append(
                {"row_number": row_number, "reason": str(exc), "raw": {"texts": texts[:2000]}}
            )
            continue
        accepted.append(recipe)
        fingerprints[recipe["content_hash"]] += 1
        for occurrence in recipe["ingredients"]:
            ingredient_total += 1
            if occurrence["amount"] is not None:
                amount_ok += 1
            elif occurrence["qualitative"]:
                qualitative += 1
            else:
                unknown_qty += 1
            if occurrence["unit"] is not None:
                unit_ok += 1
        if recipe["servings"] is not None:
            servings_known += 1
        if recipe["durations_reported"]:
            durations_known += 1
        if recipe["ingredient_groups"]:
            groups_seen += 1
        for item in recipe["quality_issues"]:
            issue_counts[item["code"]] += 1
    defective = sum(
        1
        for r in accepted
        if any(i["severity"] in ("warning", "error") for i in r["quality_issues"])
    )
    duplicates = sum(c - 1 for c in fingerprints.values() if c > 1)
    # Edge cases evaluated separately (not in pilot rates).
    edge_results: list[dict[str, Any]] = []
    for row_number in EDGE_ROWS:
        if row_number > total:
            continue
        texts = rows[row_number - 1]["texts"] or ""
        try:
            recipe = normalize_foodie_text(texts, row_number)
            edge_results.append(
                {
                    "row_number": row_number,
                    "source_id": foodie_source_id(row_number),
                    "title": recipe["title"],
                    "ingredients": len(recipe["ingredients"]),
                    "steps": len(recipe["instructions"]),
                    "status": "accepted",
                    "capabilities": recipe["capabilities"],
                }
            )
        except ValueError as exc:
            edge_results.append(
                {"row_number": row_number, "status": "rejected", "reason": str(exc)}
            )
    manifest = {
        "dataset_id": FOODIE_DATASET,
        "revision": FOODIE_REVISION,
        "file_path": FOODIE_FILE,
        "file_sha256": checksum,
        "dataset_url": f"https://huggingface.co/datasets/{FOODIE_DATASET}/tree/{FOODIE_REVISION}",
        "license_declared": FOODIE_LICENSE,
        "provenance_status": "pending",
        "adapter": "foodie",
        "adapter_version": FOODIE_ADAPTER_VERSION,
        "language": "en",
        "sampling": {
            "algorithm": "systematic-stride",
            "seed": seed,
            "size": size,
            "stride": total // size,
        },
        "total_rows": total,
        "selected_rows": selected,
        "selected_ids": [foodie_source_id(n) for n in selected],
        "edge_rows": EDGE_ROWS,
    }
    success_ex = accepted[0] if accepted else None
    failed_ex = rejected[0] if rejected else None
    report = {
        "manifest": manifest,
        "counts": {
            "candidate_rows": len(selected),
            "accepted_rows": len(accepted),
            "defective_rows": defective,
            "rejected_rows": len(rejected),
            **{f"rejected:{k}": v for k, v in reason_counts.items()},
        },
        "issues": dict(issue_counts),
        "ingredient_lines": ingredient_total,
        "amount_extraction_coverage": amount_ok / ingredient_total if ingredient_total else 0,
        "unit_extraction_coverage": unit_ok / ingredient_total if ingredient_total else 0,
        "qualitative_amounts": qualitative,
        "explicitly_unknown_quantities": unknown_qty,
        "section_parsing": {
            "ingredient_groups_seen": groups_seen,
            "servings_available": servings_known,
            "durations_available": durations_known,
            "provenance_complete": len(accepted),
        },
        "duplicate_content_candidates": duplicates,
        "representative_accepted": (
            {
                "source_id": success_ex["source_id"],
                "title": success_ex["title"],
                "ingredients": len(success_ex["ingredients"]),
                "steps": len(success_ex["instructions"]),
            }
            if success_ex
            else None
        ),
        "representative_rejected": failed_ex,
        "edge_cases": edge_results,
        "human_review_estimate": (
            "Label 30 records (~20%) at ~3 min each ≈ 1.5 h for extraction "
            "validation; full 150 at ~3 min ≈ 7.5 h. Automated coverage above "
            "is NOT accuracy — accuracy requires the manual labels."
        ),
        "note": "Coverage = fraction of lines with a parsed amount/unit. "
        "Accuracy requires manual comparison (see review-template.csv).",
    }
    output.mkdir(parents=True, exist_ok=True)
    with (output / "normalized.jsonl").open("w") as stream:
        for recipe in accepted:
            stream.write(json.dumps(recipe, ensure_ascii=False) + "\n")
    with (output / "quarantine.jsonl").open("w") as stream:
        for item in rejected:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    (output / "pilot-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "quality-report.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "REVIEW.md").write_text(_review_md(report))
    (output / "review-template.csv").write_text(_review_template(selected[:30]))
    return report


def _review_md(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "# odunola/foodie pilot review (automated — needs human labels)",
        "",
        f"Sample: {counts['candidate_rows']} accepted {counts['accepted_rows']} "
        f"defective {counts['defective_rows']} rejected {counts['rejected_rows']}.",
        f"Amount coverage: {report['amount_extraction_coverage']:.2f}; "
        f"unit coverage: {report['unit_extraction_coverage']:.2f} "
        "(coverage ≠ accuracy).",
        f"Servings available: {report['section_parsing']['servings_available']}; "
        f"durations: {report['section_parsing']['durations_available']}; "
        f"duplicate candidates: {report['duplicate_content_candidates']}.",
        f"Top issues: {report['issues']}.",
        "",
        "## Representative accepted",
        f"{report['representative_accepted']}",
        "",
        "## Representative rejected",
        f"{report['representative_rejected']}",
        "",
        "## Edge cases (separate from pilot rates)",
    ]
    for edge in report["edge_cases"]:
        lines.append(f"- {edge}")
    lines += [
        "",
        "## " + report["human_review_estimate"],
        "",
        "Label 30 rows in review-template.csv: for each ingredient line mark "
        "amount_ok/unit_ok/name_ok, and per recipe mark sections_ok. Automated "
        "checks above do not count as validation.",
    ]
    return "\n".join(lines) + "\n"


def _review_template(sample: list[int]) -> str:
    header = (
        "row_number,source_id,title,ingredient_original,"
        "amount_ok,unit_ok,name_ok,sections_ok,notes\n"
    )
    rows = "".join(f"{n},{foodie_source_id(n)},,,,,,,\n" for n in sample)
    return header + rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=Path("data/odunola-pilot"))
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    if not 100 <= args.size <= 200:
        parser.error("pilot size must be between 100 and 200")
    settings = Settings()
    path = args.csv or Path(
        hf_hub_download(
            FOODIE_DATASET,
            FOODIE_FILE,
            repo_type="dataset",
            revision=FOODIE_REVISION,
            token=settings.hf_token.get_secret_value() or False,
            cache_dir=str(Path(settings.hf_home) / "hub"),
        )
    )
    report = run_pilot(path, args.output, size=args.size, seed=args.seed)
    print(json.dumps(report["counts"], indent=2))


if __name__ == "__main__":
    main()
