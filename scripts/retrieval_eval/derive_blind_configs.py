"""Re-derive blind configurations at the frozen cutoff (correction, no re-run).

The blind raw run omitted --cutoff, so its embedded derived_top5 used
cutoff=None. This script re-derives all six configurations from the
recorded raw arms with the frozen derive_configs, cutoff and rrf_k read
from freeze.json. No retrieval, no frozen-code changes, no packet change.
Writes data/phase6/blind_derived.json (counts and hashes printed only).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "retrieval_eval"))
sys.path.insert(0, str(REPO / "src"))

from phase6_eval import derive_configs  # noqa: E402

RAW = REPO / "data" / "phase6" / "blind_raw.json"
FREEZE = REPO / "evals" / "results" / "phase6" / "freeze.json"
PACKET = REPO / "data" / "phase6" / "blind_packet.json"
OUT = REPO / "data" / "phase6" / "blind_derived.json"
CONFIG_KEYS = ("fulltext", "vector_a", "vector_c", "hybrid_a", "hybrid_b", "hybrid_c")


def _ids(rows: list[dict[str, Any]]) -> list[list[str]]:
    return [[str(r.get("dataset_id")), str(r.get("source_id"))] for r in rows]


def main() -> int:
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    freeze = json.loads(FREEZE.read_text(encoding="utf-8"))
    cutoff = freeze["configurations"]["vector_c"]["params"]["vector_distance_cutoff"]
    assert cutoff == freeze["configurations"]["hybrid_c"]["params"]["vector_distance_cutoff"]
    rrf_k = freeze["configurations"]["hybrid_c"]["params"]["rrf_k"]
    assert rrf_k == 60
    packet = json.loads(PACKET.read_text(encoding="utf-8"))
    pool_by_case = {
        str(r["case_id"]): {(c["dataset_id"], c["source_id"]) for c in r["candidates"]}
        for r in packet["requests"]
    }
    derived_cases: list[dict[str, Any]] = []
    changed = 0
    vector_abstains = 0
    outside_pool: list[str] = []
    for case in raw["cases"]:
        case_id = str(case["case_id"])
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
        frozen = derive_configs(fulltext, vector, cutoff=cutoff, rrf_k=rrf_k)
        stored = case["derived_top5"]
        if _ids(frozen["vector_c"]) != [
            [r["dataset_id"], r["source_id"]] for r in stored["vector_c"]
        ] or _ids(frozen["hybrid_c"]) != [
            [r["dataset_id"], r["source_id"]] for r in stored["hybrid_c"]
        ]:
            changed += 1
        if frozen["vector_abstention_c"] is not None:
            vector_abstains += 1
        pool = pool_by_case[case_id]
        for key in ("vector_c", "hybrid_c"):
            for row in frozen[key]:
                if (str(row.get("dataset_id")), str(row.get("source_id"))) not in pool:
                    outside_pool.append(f"{case_id}:{key}")
        derived_cases.append(
            {
                "case_id": case_id,
                "cutoff": cutoff,
                "rrf_k": rrf_k,
                "configs": {key: _ids(frozen[key]) for key in CONFIG_KEYS},
                "vector_abstention_c": frozen["vector_abstention_c"],
                "hybrid_abstention_c": frozen["hybrid_abstention_c"],
            }
        )
    out = {
        "derived": "phase6-blind-derived-v1",
        "source_raw_sha256": hashlib.sha256(RAW.read_bytes()).hexdigest(),
        "cutoff": cutoff,
        "rrf_k": rrf_k,
        "cases": derived_cases,
    }
    OUT.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(OUT.read_bytes()).hexdigest()
    print(
        f"cases={len(derived_cases)} changed_c_top5={changed} vector_c_abstains={vector_abstains}"
    )
    print(f"outside_pool={outside_pool or 'none'} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
