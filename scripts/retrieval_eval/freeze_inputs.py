"""Freeze the baseline evaluation inputs (read-only; no DB writes).

Validates the case file, recomputes metrics over recorded labels with the
committed metric code, and records case/rubric hashes, corpus and search
fingerprints, generator commands, discovery methods, exposure status, and
label provenance. Defaults to ``phase2_inputs_freeze.json`` (pre-rebuild);
use ``--tag post-rebuild`` for the separately named post-rebuild
fingerprint. Never overwrites the pre-rebuild file with post-rebuild data.

Usage (from repo root):
    uv run python scripts/retrieval_eval/freeze_inputs.py [--tag post-rebuild]
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

from case_schema import check_case_file, load_case_file  # noqa: E402
from metrics import compute_metrics  # noqa: E402

PHASE1 = REPO / "evals" / "results" / "phase1"
PREBUILD_OUT = PHASE1 / "phase2_inputs_freeze.json"
POSTBUILD_OUT = PHASE1 / "phase2_inputs_post_rebuild.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(args: list[str]) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, cwd=REPO)
        return out.stdout.strip()
    except Exception:
        return "unknown"


def main() -> int:
    cases_path = REPO / "evals/cases/phase1_retrieval.json"
    data = load_case_file(str(cases_path))
    problems = check_case_file(data)
    if problems:
        print(json.dumps({"problems": problems}, indent=2))
        return 1
    corrections = json.loads((PHASE1 / "checkpoint1_corrections.json").read_text())
    labels: list[dict[str, Any]] = corrections.get("labels", [])
    aggregate_groups = {c.case_id: c.aggregate_group for c in data.cases if c.aggregate_group}
    metrics = compute_metrics(labels, aggregate_groups=aggregate_groups)
    calibration = json.loads((PHASE1 / "calibration_packet_v2.json").read_text())
    pools = json.loads((PHASE1 / "review_packet_v2.json").read_text())

    from sqlalchemy import create_engine, text

    from culinary_copilot.config import Settings
    from culinary_copilot.recipes.search import SEARCH_DOCUMENT_VERSION

    engine = create_engine(Settings().database_url.get_secret_value())
    try:
        with engine.connect() as conn:
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
                        "select dataset_id, revision, normalizer_version "
                        "from recipe_imports order by 1"
                    )
                ).all()
            ]
            search_versions = conn.execute(
                text("select search_document_version, count(*) from recipes group by 1 order by 1")
            ).all()
    finally:
        engine.dispose()

    by_reviewer: dict[str, int] = {}
    fallback = str(corrections.get("labels_reviewer_type", "unknown"))
    for label in labels:
        reviewer = str(label.get("reviewer_type", fallback))
        by_reviewer[reviewer] = by_reviewer.get(reviewer, 0) + 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", choices=["pre-rebuild", "post-rebuild"], default="pre-rebuild")
    tag = parser.parse_args().tag
    if tag == "post-rebuild" and PREBUILD_OUT.exists():
        out = POSTBUILD_OUT
    elif tag == "pre-rebuild":
        out = PREBUILD_OUT
    else:
        print("post-rebuild requires the pre-rebuild freeze to exist; refusing to overwrite")
        return 1
    freeze = {
        "freeze": f"phase2-inputs-{tag}",
        "status": (
            "AI-assisted review accepted by the project owner; "
            "not independently human-verified or culinarily validated."
        ),
        "cases": {
            "file": "evals/cases/phase1_retrieval.json",
            "sha256": _sha(cases_path),
            "version": data.version,
            "count": len(data.cases),
            "development": sum(1 for c in data.cases if c.split == "development"),
            "held_out": sum(1 for c in data.cases if c.split == "held_out"),
        },
        "rubric": {
            "file": "evals/rubric_v1.md",
            "sha256": _sha(REPO / "evals/rubric_v1.md"),
        },
        "labels": {
            "source": "evals/results/phase1/checkpoint1_corrections.json",
            "sha256": _sha(PHASE1 / "checkpoint1_corrections.json"),
            "count": len(labels),
            "by_reviewer_type": by_reviewer,
            "schema": corrections.get("label_schema"),
        },
        "metrics": metrics,
        "metrics_note": (
            "Topical Recall@5/MRR and joint suitability over RECORDED labels only; "
            "unjudged candidates excluded (never zero-graded); cases without "
            "judgments listed in no_relevant_label_cases (never verified no-match); "
            "shared intents counted once."
        ),
        "corpus_fingerprint": {"total": total, "by_dataset": by_dataset, "imports": imports},
        "search_fingerprint": {
            "renderer_version": SEARCH_DOCUMENT_VERSION,
            "search_document_versions": [[r[0], r[1]] for r in search_versions],
        },
        "code_revision": _git(["git", "rev-parse", "--short", "HEAD"]),
        "dirty_tree": _git(["git", "status", "--short"])[:2000],
        "commands": {
            "validate": "uv run python scripts/retrieval_eval/validate_cases.py",
            "assemble": "uv run python scripts/retrieval_eval/assemble_packet.py",
            "calibrate": "uv run python scripts/retrieval_eval/build_calibration_packet.py",
            "freeze": "uv run python scripts/retrieval_eval/freeze_inputs.py",
        },
        "discovery_methods": {
            "annotation_pool": pools["provenance"]["discovery_method"],
            "production": "deterministic mapping plus repository search at case limit",
        },
        "exposure": calibration["exposure"],
        "judgment_coverage": calibration["tallies"],
    }
    out.write_text(json.dumps(freeze, indent=2) + "\n", encoding="utf-8")
    print(
        f"cases={freeze['cases']['count']} labels={len(labels)} "
        f"judged_cases={metrics['cases_with_judgments']} "
        f"recall5={metrics['topical_recall_at_k']:.3f} corpus={total}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
