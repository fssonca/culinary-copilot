"""Build the Phase 6 blind judging packet (ignored working artifact).

Reads the frozen blind raw run plus the handed-over judging template and
emits, without touching frozen code:
  - data/phase6/blind_packet.json: per-request pools (union of every
    configuration's top-5 from freeze.json, deduped, shuffled, mode and
    rank hidden) with exactly the template's required candidate evidence
    fields. No selection, highlighting or summarizing beyond the template.
  - data/phase6/blind_violations.json: script-computed mechanical
    violations per candidate, stored separately from the packet.
  - data/phase6/blind_run_provenance.json: all blind file hashes,
    converter hash, raw-output hash, command and spend.

The builder never prints result content (counts and hashes only), per the
Part D blinding rule.
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "retrieval_eval"))
sys.path.insert(0, str(REPO / "src"))

from phase6_eval import build_pool, derive_configs, mechanical_violations  # noqa: E402

RAW = REPO / "data" / "phase6" / "blind_raw.json"
TEMPLATE = REPO / "data" / "phase6" / "blind" / "judging_template_v3.json"
FREEZE = REPO / "evals" / "results" / "phase6" / "freeze.json"
PACKET = REPO / "data" / "phase6" / "blind_packet.json"
VIOLATIONS = REPO / "data" / "phase6" / "blind_violations.json"
PROVENANCE = REPO / "data" / "phase6" / "blind_run_provenance.json"
SHUFFLE_SEED = 20260926
CONFIG_KEYS = ("fulltext", "vector_a", "vector_c", "hybrid_a", "hybrid_b", "hybrid_c")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    freeze = json.loads(FREEZE.read_text(encoding="utf-8"))
    cutoff = freeze["configurations"]["vector_c"]["params"]["vector_distance_cutoff"]
    assert cutoff == freeze["configurations"]["hybrid_c"]["params"]["vector_distance_cutoff"]

    from sqlalchemy import create_engine

    from culinary_copilot.config import Settings
    from culinary_copilot.recipes.repository import get_recipe
    from culinary_copilot.retrieval.service import _excerpt_from_doc

    engine = create_engine(Settings().database_url.get_secret_value())
    rng = random.Random(SHUFFLE_SEED)
    try:
        packet_requests: list[dict[str, Any]] = []
        violations_all: list[dict[str, Any]] = []
        pool_sizes: dict[str, int] = {}
        for case in raw["cases"]:
            fulltext = [
                {"dataset_id": r["dataset_id"], "source_id": r["source_id"]}
                for r in case["fulltext_top20"]
            ]
            vector = [
                {
                    "dataset_id": r["dataset_id"],
                    "source_id": r["source_id"],
                    "distance": r["distance"],
                }
                for r in case["vector_top20"]
            ]
            derived = derive_configs(fulltext, vector, cutoff=cutoff)
            pool = build_pool([derived[key] for key in CONFIG_KEYS])
            pool_sizes[str(case["case_id"])] = len(pool)
            docs: dict[str, dict[str, Any] | None] = dict(case.get("documents", {}))
            # Fetch any pooled candidate whose full document is not on record.
            for dataset_id, source_id in pool:
                marker = f"{dataset_id}\x00{source_id}"
                if marker in docs:
                    continue
                try:
                    doc = get_recipe(engine, source_id, dataset_id=dataset_id)
                except ValueError:
                    doc = None
                if doc is None:
                    docs[marker] = None
                    continue
                durations = doc.get("durations_minutes")
                total = durations.get("TotalTime") if isinstance(durations, dict) else None
                ingredients = doc.get("ingredients") or []
                docs[marker] = {
                    "title": doc.get("title"),
                    "ingredients": [
                        item.get("canonical") or item.get("name")
                        for item in ingredients
                        if isinstance(item, dict)
                    ][:30],
                    "total_minutes_reported": total,
                    "excerpt": _excerpt_from_doc(doc),
                }
            candidates: list[dict[str, Any]] = []
            ceiling_raw: Any = case.get("time_minutes")
            ceiling = float(ceiling_raw) if ceiling_raw is not None else None
            scope = case.get("dataset_scope")
            scope_value = None if scope in (None, "combined") else str(scope)
            for dataset_id, source_id in pool:
                stored = docs.get(f"{dataset_id}\x00{source_id}")
                if stored is None:
                    candidates.append(
                        {
                            "dataset_id": dataset_id,
                            "source_id": source_id,
                            "document_available": False,
                        }
                    )
                    total_reported = None
                else:
                    total_reported = stored["total_minutes_reported"]
                    candidates.append(
                        {
                            "title": stored["title"],
                            "ingredients": stored["ingredients"],
                            "reported_total_time": total_reported,
                            "dataset_id": dataset_id,
                            "source_id": source_id,
                            "fixed_evidence_excerpt": stored["excerpt"],
                        }
                    )
                violations_all.append(
                    {
                        "case_id": case["case_id"],
                        "dataset_id": dataset_id,
                        "source_id": source_id,
                        "time_violation": "time"
                        in mechanical_violations(
                            total_minutes=total_reported,
                            ceiling=ceiling,
                            dataset_id=str(dataset_id),
                            scope=scope_value,
                        ),
                        "dataset_violation": "dataset"
                        in mechanical_violations(
                            total_minutes=total_reported,
                            ceiling=ceiling,
                            dataset_id=str(dataset_id),
                            scope=scope_value,
                        ),
                    }
                )
            rng.shuffle(candidates)
            request_fields = {
                "case_id": case["case_id"],
                "query_text": case["query_text"],
                "dataset_scope": case.get("dataset_scope"),
            }
            packet_requests.append({**request_fields, "candidates": candidates})
    finally:
        engine.dispose()
    packet = {
        "packet": "phase6-blind-packet-v1",
        "template_version": template["version"],
        "template_sha256": _sha(TEMPLATE),
        "cutoff": cutoff,
        "shuffle_seed": SHUFFLE_SEED,
        "blinding": "pooled union of every configuration top-5; shuffled; mode and rank hidden",
        "requests": packet_requests,
        "judgments": [],
    }
    PACKET.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    VIOLATIONS.write_text(json.dumps(violations_all, indent=2) + "\n", encoding="utf-8")
    provenance = {
        "run": "phase6-blind-raw",
        "command": (
            "uv run python scripts/retrieval_eval/run_phase6.py "
            "--cases data/phase6/blind/blind_cases_phase6_format.json "
            "--split blind_confirmation --live --price-verified "
            "--ceiling-usd 0.05 --out data/phase6/blind_raw.json"
        ),
        "runner_changed": False,
        "blind_requests_sha256": "5a613b5d5b17585a4ee560b23d882bae96103fbf65b7f59ed2d39db2f3c6e71f",
        "blind_phase6_format_sha256": _sha(
            REPO / "data/phase6/blind/blind_cases_phase6_format.json"
        ),
        "judging_template_sha256": _sha(TEMPLATE),
        "converter_sha256": _sha(REPO / "data/phase6/blind/convert_blind_requests.py"),
        "raw_output_sha256": _sha(RAW),
        "packet_sha256": _sha(PACKET),
        "violations_sha256": _sha(VIOLATIONS),
        "spend": raw["spend"],
        "pool_sizes": pool_sizes,
    }
    PROVENANCE.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(f"requests={len(packet_requests)} pool_total={sum(pool_sizes.values())}")
    print(f"raw={provenance['raw_output_sha256'][:16]} packet={provenance['packet_sha256'][:16]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
